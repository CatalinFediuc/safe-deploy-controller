import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from conftest import ENV
from deployctl import __version__
from deployctl.cli import main
from deployctl.clock import SystemClock
from deployctl.errors import InvariantError
from deployctl.models import EnvironmentState, EnvStatus, ExitCode
from deployctl.state import StateStore

EXAMPLES = Path(__file__).parents[2] / "examples"
MANIFESTS = EXAMPLES / "manifests"
ARTIFACTS = EXAMPLES / "artifacts"
HEALTHY = EXAMPLES / "health" / "default.json"
OUTAGE = EXAMPLES / "health" / "dependency-outage.json"
RECOVER = ("--service", "orders-api", "--env", "staging", "--reason", "outage over")
NAMES = ("--service", "orders-api", "--env", "staging")

Run = Callable[..., int]


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SystemClock, "sleep", lambda self, seconds: None)


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def deploy(state: Path) -> Run:
    def run(manifest: str, *flags: str, profile: Path = HEALTHY, output: str = "text") -> int:
        return main(
            [
                "deploy",
                "--manifest",
                str(MANIFESTS / f"{manifest}.json"),
                "--artifact-dir",
                str(ARTIFACTS),
                "--health-profile",
                str(profile),
                "--state-dir",
                str(state),
                "--output",
                output,
                *flags,
            ]
        )

    return run


@pytest.fixture
def command(state: Path) -> Run:
    def run(name: str, *args: str) -> int:
        return main([name, "--state-dir", str(state), *args])

    return run


@pytest.fixture
def recover(command: Run, monkeypatch: pytest.MonkeyPatch) -> Run:
    monkeypatch.setenv("USER", "oncall")

    def run(*extra: str, profile: Path = HEALTHY) -> int:
        return command("recover", *RECOVER, "--health-profile", str(profile), *extra)

    return run


def usage_error(argv: list[str]) -> Any:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_a_subcommand_is_required() -> None:
    assert usage_error([]) == ExitCode.USAGE


def test_deploy_needs_a_health_profile() -> None:
    assert usage_error(["deploy", "--manifest", "m", "--artifact-dir", "a"]) == ExitCode.USAGE


@pytest.mark.parametrize(
    "argv",
    [
        ["status", *NAMES[:1], "Not_Valid", "--env", "staging"],
        ["status", "--service", "orders-api", "--env", "qa"],
        ["recover", "--service", "x", "--env", "staging", "--reason", "r", "--health-profile", "p"],
        ["recover", *NAMES, "--reason", "  ", "--health-profile", "p"],
        ["history", *NAMES, "--limit", "0"],
        ["history", *NAMES, "--limit", "ten"],
        ["validate", "--manifest", "m", "--artifact-dir", "a", "--state-dir", "s"],
        [
            "deploy",
            "--manifest",
            "m",
            "--artifact-dir",
            "a",
            "--health-profile",
            "p",
            "--actor",
            "x",
        ],
    ],
    ids=[
        "bad-service",
        "bad-env",
        "short-service",
        "blank-reason",
        "zero-limit",
        "word-limit",
        "validate-has-no-state",
        "deploy-has-no-actor",
    ],
)
def test_usage_errors_exit_2_and_leave_stdout_empty(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert usage_error(argv) == ExitCode.USAGE
    assert capsys.readouterr().out == ""


def test_recover_needs_an_actor_when_user_is_unset(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("USER")
    argv = ["recover", *RECOVER, "--health-profile", str(HEALTHY), "--state-dir", str(state)]
    assert usage_error(argv) == ExitCode.USAGE
    assert "--actor" in capsys.readouterr().err


def test_validate_good_manifest_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code = main(
        [
            "validate",
            "--manifest",
            str(MANIFESTS / "orders-api-1.4.2.json"),
            "--artifact-dir",
            str(ARTIFACTS),
            "--output",
            "json",
        ]
    )

    assert code == ExitCode.OK
    document = json.loads(capsys.readouterr().out)
    assert document["outcome"] == "VALID"
    assert document["to_version"] == "1.4.2"
    assert document["final_state"] is None
    assert list(tmp_path.iterdir()) == []


def test_validate_bad_manifest_exits_3_and_lists_every_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "validate",
            "--manifest",
            str(MANIFESTS / "invalid-schema.json"),
            "--artifact-dir",
            str(ARTIFACTS),
        ]
    )

    assert code == ExitCode.REJECTED
    out = capsys.readouterr().out
    assert "OUTCOME REJECTED" in out
    assert "version" in out
    assert "environment" in out


def test_interrupted_validate_exits_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(*_: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("deployctl.controller.load_manifest", interrupted)
    code = main(["validate", "--manifest", "m", "--artifact-dir", "a"])

    assert code == ExitCode.INTERRUPTED
    assert "OUTCOME INTERRUPTED" in capsys.readouterr().out


def test_t16_json_contract(deploy: Run, capsys: pytest.CaptureFixture[str]) -> None:
    deploy("orders-api-1.4.2")
    capsys.readouterr()

    code = deploy("orders-api-1.5.0", output="json")

    assert code == ExitCode.ROLLED_BACK
    document = json.loads(capsys.readouterr().out)  # stdout is JSON and nothing else
    assert document["outcome"] == "ROLLED_BACK"
    assert document["rollback_status"] == "SUCCEEDED"
    assert document["exit_code"] == code
    assert document["actor"] == "catalin"
    assert document["final_state"] == {"status": "STABLE", "active_version": "1.4.2"}
    assert all({"seq", "ts", "stage", "result", "message"} <= set(s) for s in document["steps"])


def test_the_demo_sequence_ends_with_the_documented_exit_codes(
    deploy: Run, recover: Run, command: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    assert deploy("orders-api-1.4.2") == ExitCode.OK
    assert deploy("orders-api-1.4.2") == ExitCode.OK
    assert deploy("orders-api-1.5.0") == ExitCode.ROLLED_BACK
    assert deploy("orders-api-1.5.0") == ExitCode.BLOCKED
    assert deploy("orders-api-1.5.1") == ExitCode.OK
    assert deploy("invalid-schema") == ExitCode.REJECTED
    assert deploy("checksum-mismatch") == ExitCode.REJECTED
    assert deploy("path-traversal") == ExitCode.REJECTED
    assert deploy("orders-api-1.6.0", profile=OUTAGE) == ExitCode.NEEDS_ATTENTION
    assert deploy("orders-api-1.5.1") == ExitCode.BLOCKED
    assert recover() == ExitCode.OK
    capsys.readouterr()

    assert command("status", *NAMES) == ExitCode.OK
    assert "STABLE" in capsys.readouterr().out
    assert command("history", *NAMES, "--limit", "15", "--output", "json") == ExitCode.OK
    runs = json.loads(capsys.readouterr().out)["runs"]
    assert [run["outcome"] for run in runs] == [
        "RECOVERED",
        "BLOCKED",
        "ROLLBACK_FAILED",
        "REJECTED",  # path traversal
        "REJECTED",  # checksum mismatch; invalid-schema declares `qa`, so it is listed there
        "SUCCEEDED",
        "BLOCKED",
        "ROLLED_BACK",
        "NO_OP",
        "SUCCEEDED",
    ]


def test_dry_run_and_overrides_reach_the_controller(
    deploy: Run, state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deploy("orders-api-1.5.1")
    before = (state / "state.json").read_bytes()

    assert deploy("orders-api-1.4.2", "--dry-run", "--allow-downgrade") == ExitCode.OK
    assert (state / "state.json").read_bytes() == before
    assert deploy("orders-api-1.4.2") == ExitCode.BLOCKED
    assert deploy("orders-api-1.4.2", "--allow-downgrade") == ExitCode.OK

    assert deploy("orders-api-1.5.0", "--allow-downgrade") == ExitCode.ROLLED_BACK
    assert deploy("orders-api-1.5.0") == ExitCode.BLOCKED
    assert deploy("orders-api-1.5.0", "--allow-known-bad") == ExitCode.ROLLED_BACK


def test_a_bad_health_profile_exits_2_before_anything_is_touched(
    deploy: Run, state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text('{"orders-api": {"1.6.O": []}}', encoding="utf-8")

    assert deploy("orders-api-1.4.2", profile=profile, output="json") == ExitCode.USAGE

    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert json.loads(captured.out)["exit_code"] == ExitCode.USAGE
    assert not state.exists()


def test_a_missing_health_profile_exits_2_for_recover(recover: Run, tmp_path: Path) -> None:
    assert recover(profile=tmp_path / "absent.json") == ExitCode.USAGE


def test_recover_uses_user_for_the_actor_unless_told_otherwise(
    deploy: Run, recover: Run, state: Path
) -> None:
    deploy("orders-api-1.4.2")
    (state / "environment.json").unlink()
    recover()
    (state / "environment.json").unlink()
    recover("--actor", "someone-else")

    lines = (state / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    actors = [json.loads(line)["actor"] for line in lines if '"stage": "recover"' in line]
    assert actors == ["oncall", "someone-else"]


def test_recover_break_lock_reaches_the_controller(deploy: Run, recover: Run, state: Path) -> None:
    deploy("orders-api-1.4.2")
    (state / "deployctl.lock").write_text("{}", encoding="utf-8")

    assert recover() == ExitCode.BLOCKED
    assert recover("--break-lock") == ExitCode.OK
    assert not (state / "deployctl.lock").exists()


def test_recover_refuses_an_environment_with_no_record(
    recover: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    assert recover() == ExitCode.BLOCKED
    assert "no record of orders-api/staging" in capsys.readouterr().out


def test_status_json(deploy: Run, command: Run, capsys: pytest.CaptureFixture[str]) -> None:
    deploy("orders-api-1.4.2")
    capsys.readouterr()

    assert command("status", *NAMES, "--output", "json") == ExitCode.OK

    document = json.loads(capsys.readouterr().out)
    assert document["status"] == "STABLE"
    assert document["running_version"] == "1.4.2"
    assert document["active_version"] == "1.4.2"
    assert document["drift"] is False


def test_status_reports_drift_and_still_exits_0(
    deploy: Run, command: Run, state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deploy("orders-api-1.4.2")
    (state / "environment.json").unlink()
    capsys.readouterr()

    assert command("status", *NAMES, "--output", "json") == ExitCode.OK
    assert json.loads(capsys.readouterr().out)["drift"] is True


def test_status_of_needs_attention_exits_0(deploy: Run, command: Run) -> None:
    deploy("orders-api-1.4.2")
    deploy("orders-api-1.6.0", profile=OUTAGE)
    assert command("status", *NAMES) == ExitCode.OK


def test_status_with_no_record_exits_6(command: Run, capsys: pytest.CaptureFixture[str]) -> None:
    assert command("status", *NAMES, "--output", "json") == ExitCode.BLOCKED

    captured = capsys.readouterr()
    assert "no record of orders-api/staging" in captured.err
    document = json.loads(captured.out)
    assert document["exit_code"] == ExitCode.BLOCKED
    assert "no record of orders-api/staging" in document["error"]


def test_history_can_find_a_rejected_run_under_an_invalid_name(
    deploy: Run, command: Run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"service": "orders-api", "environment": "qa"}', encoding="utf-8")
    assert (
        command(
            "deploy",
            "--manifest",
            str(manifest),
            "--artifact-dir",
            str(ARTIFACTS),
            "--health-profile",
            str(HEALTHY),
        )
        == ExitCode.REJECTED
    )
    capsys.readouterr()

    assert command("history", "--service", "orders-api", "--env", "qa") == ExitCode.OK
    assert "REJECTED" in capsys.readouterr().out


def test_history_warns_about_unreadable_lines_and_reports_them(
    deploy: Run, command: Run, state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deploy("orders-api-1.4.2")
    with (state / "audit.jsonl").open("a", encoding="utf-8") as stream:
        stream.write('{"run_id": "r-1", "mess')
    deploy("orders-api-1.4.2")
    capsys.readouterr()

    assert command("history", *NAMES, "--output", "json") == ExitCode.OK

    captured = capsys.readouterr()
    assert "unreadable audit lines skipped: 1" in captured.err
    document = json.loads(captured.out)
    assert document["skipped_lines"] == 1
    assert [run["outcome"] for run in document["runs"]] == ["NO_OP", "SUCCEEDED"]


def test_history_of_an_unknown_environment_exits_0(
    command: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    assert command("history", *NAMES) == ExitCode.OK
    assert "no recorded runs for orders-api/staging" in capsys.readouterr().out


def test_state_dir_defaults_to_the_environment_variable(
    deploy: Run, state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deploy("orders-api-1.4.2")
    monkeypatch.setenv("DEPLOYCTL_STATE_DIR", str(state))
    capsys.readouterr()

    assert main(["status", *NAMES]) == ExitCode.OK


def test_state_dir_falls_back_to_a_local_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEPLOYCTL_STATE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    main(
        [
            "deploy",
            "--manifest",
            str(MANIFESTS / "orders-api-1.4.2.json"),
            "--artifact-dir",
            str(ARTIFACTS),
            "--health-profile",
            str(HEALTHY),
        ]
    )
    assert (tmp_path / ".deployctl" / "state.json").exists()


def test_run_ids_sort_by_time_and_are_unique(
    deploy: Run, state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deploy("orders-api-1.4.2")
    deploy("orders-api-1.4.2")

    lines = (state / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    run_ids = {json.loads(line)["run_id"] for line in lines}
    assert len(run_ids) == 2
    assert all(re.fullmatch(r"r-\d{8}T\d{6}-[0-9a-f]{6}", run_id) for run_id in run_ids)


def test_corrupt_state_exits_1_with_a_json_error(
    deploy: Run, state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state.mkdir()
    (state / "state.json").write_text("{oops", encoding="utf-8")

    assert deploy("orders-api-1.4.2", output="json") == ExitCode.INTERNAL_ERROR

    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    document = json.loads(captured.out)
    assert set(document) == {"error", "exit_code"}
    assert document["exit_code"] == ExitCode.INTERNAL_ERROR
    assert "state.json" in document["error"]


def test_corrupt_state_exits_1_for_status(command: Run, state: Path) -> None:
    state.mkdir()
    (state / "state.json").write_text("{oops", encoding="utf-8")
    assert command("status", *NAMES) == ExitCode.INTERNAL_ERROR


def test_a_stopped_run_exits_1_with_the_recover_instruction(
    deploy: Run, state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deploy("orders-api-1.4.2")
    capsys.readouterr()
    real_save = StateStore.save

    def save_intent_only(store: StateStore, entry: EnvironmentState) -> None:
        if entry.status is not EnvStatus.DEPLOYING:
            raise OSError("disk full")
        real_save(store, entry)

    monkeypatch.setattr(StateStore, "save", save_intent_only)

    assert deploy("orders-api-1.6.0") == ExitCode.INTERNAL_ERROR
    error = capsys.readouterr().err
    assert error.startswith("error: orders-api/staging: disk full")  # not reported as a surprise
    assert "run `deployctl recover`" in error
    monkeypatch.undo()
    entry = json.loads((state / "state.json").read_text(encoding="utf-8"))["environments"][ENV]
    assert entry["status"] == "DEPLOYING"


def test_a_bug_exits_1(
    deploy: Run, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def bug(*_: object) -> None:
        raise InvariantError("illegal status transition")

    monkeypatch.setattr("deployctl.controller.check_transition", bug)

    assert deploy("orders-api-1.4.2") == ExitCode.INTERNAL_ERROR
    assert "illegal status transition" in capsys.readouterr().err


def test_an_unexpected_error_exits_1_and_points_at_status(
    command: Run, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(*_: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr("deployctl.audit.AuditLog.history", broken)

    assert command("history", *NAMES) == ExitCode.INTERNAL_ERROR
    error = capsys.readouterr().err
    assert "OSError: permission denied" in error
    assert "deployctl status" in error


def test_an_interrupt_outside_a_run_exits_130(
    command: Run, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(*_: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("deployctl.audit.AuditLog.history", interrupted)

    assert command("history", *NAMES, "--output", "json") == ExitCode.INTERRUPTED
    assert json.loads(capsys.readouterr().out)["exit_code"] == ExitCode.INTERRUPTED


def test_sigterm_during_a_run_stops_it_in_process(
    deploy: Run, state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        SystemClock, "sleep", lambda self, seconds: os.kill(os.getpid(), signal.SIGTERM)
    )

    assert deploy("orders-api-1.4.2") == ExitCode.INTERRUPTED

    assert "OUTCOME INTERRUPTED" in capsys.readouterr().out
    assert not (state / "deployctl.lock").exists()


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP], ids=str)
def test_stop_signal_handlers_are_put_back(command: Run, number: signal.Signals) -> None:
    def embedding_application(*_: object) -> None:
        raise AssertionError("not expected to run")

    original = signal.signal(number, embedding_application)
    try:
        command("history", *NAMES)
        assert signal.getsignal(number) is embedding_application
    finally:
        signal.signal(number, original)


def _slow_manifest(tmp_path: Path) -> Path:
    document = json.loads((MANIFESTS / "orders-api-1.4.2.json").read_text(encoding="utf-8"))
    document["health_check"]["interval_seconds"] = 30
    path = tmp_path / "slow.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.mark.parametrize("stop", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP], ids=str)
def test_a_stop_signal_releases_the_lock_and_leaves_explicit_state(
    tmp_path: Path, state: Path, stop: signal.Signals
) -> None:
    process = subprocess.Popen(  # noqa: S603 - our own interpreter and arguments
        [
            sys.executable,
            "-m",
            "deployctl",
            "deploy",
            "--manifest",
            str(_slow_manifest(tmp_path)),
            "--artifact-dir",
            str(ARTIFACTS),
            "--health-profile",
            str(HEALTHY),
            "--state-dir",
            str(state),
            "--output",
            "json",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 20
    audit = state / "audit.jsonl"
    while '"stage": "deploy"' not in (audit.read_text() if audit.exists() else ""):
        assert time.monotonic() < deadline, "the deploy never reached its verify wait"
        time.sleep(0.02)
    time.sleep(0.1)  # into the 30s wait between probes

    process.send_signal(stop)
    out, _ = process.communicate(timeout=20)

    assert process.returncode == ExitCode.INTERRUPTED
    assert json.loads(out)["outcome"] == "INTERRUPTED"
    assert not (state / "deployctl.lock").exists()
    ledger = json.loads((state / "state.json").read_text(encoding="utf-8"))
    entry = ledger["environments"][ENV]
    assert entry["status"] == "VERIFYING"
    assert entry["in_flight"] is not None
