import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeClock, make_manifest
from deployctl.audit import AuditLog, NullAudit, Recorder
from deployctl.errors import InvariantError
from deployctl.models import (
    EnvironmentState,
    EnvStatus,
    ExitCode,
    FinalState,
    HealthCheckSpec,
    Outcome,
    RollbackStatus,
    Stage,
    StepResult,
)

EVENT_KEYS = [
    "seq",
    "ts",
    "run_id",
    "deployment_key",
    "service",
    "environment",
    "stage",
    "result",
    "from_version",
    "to_version",
    "change_id",
    "actor",
    "message",
]


def events(audit_log: AuditLog) -> list[dict[str, Any]]:
    lines = audit_log.path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


@pytest.fixture
def recorder(audit_log: AuditLog, clock: FakeClock) -> Recorder:
    return Recorder(audit_log, clock, run_id="r-0007")


def test_append_creates_the_state_dir(audit_log: AuditLog) -> None:
    audit_log.append({"seq": 1})
    assert events(audit_log) == [{"seq": 1}]


def test_append_keeps_existing_events(audit_log: AuditLog, state_dir: Path) -> None:
    audit_log.append({"seq": 1})
    AuditLog(state_dir).append({"seq": 2})
    assert events(audit_log) == [{"seq": 1}, {"seq": 2}]


def test_multiline_message_stays_on_one_line(audit_log: AuditLog) -> None:
    audit_log.append({"message": "first\nsecond"})
    audit_log.append({"message": "third"})
    assert [event["message"] for event in events(audit_log)] == ["first\nsecond", "third"]


def test_every_append_is_fsynced(audit_log: AuditLog, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[int] = []
    monkeypatch.setattr(os, "fsync", synced.append)
    audit_log.append({"seq": 1})
    audit_log.append({"seq": 2})
    assert len(synced) == 2


def test_steps_are_numbered_in_order(
    recorder: Recorder, audit_log: AuditLog, clock: FakeClock
) -> None:
    recorder.step(Stage.VALIDATE, StepResult.PASS, "manifest valid")
    clock.advance(0.002)
    recorder.step(Stage.PREFLIGHT, StepResult.PASS, "lock acquired")
    clock.advance(0.006)
    recorder.step(Stage.DEPLOY, StepResult.FAIL, "apply failed")

    logged = events(audit_log)
    assert [event["seq"] for event in logged] == [1, 2, 3]
    assert [event["stage"] for event in logged] == ["validate", "preflight", "deploy"]
    assert [event["ts"] for event in logged] == [
        "2026-11-01T10:15:01.000Z",
        "2026-11-01T10:15:01.002Z",
        "2026-11-01T10:15:01.008Z",
    ]


def test_each_run_numbers_its_own_steps(
    audit_log: AuditLog, clock: FakeClock, run_ids: Callable[[], str]
) -> None:
    for _ in range(2):
        Recorder(audit_log, clock, run_ids()).step(Stage.VALIDATE, StepResult.PASS, "ok")
    assert [(event["run_id"], event["seq"]) for event in events(audit_log)] == [
        ("r-0001", 1),
        ("r-0002", 1),
    ]


def test_event_before_bind_has_null_identity(recorder: Recorder, audit_log: AuditLog) -> None:
    recorder.step(Stage.VALIDATE, StepResult.FAIL, "manifest: invalid JSON")
    event = events(audit_log)[0]
    assert list(event) == EVENT_KEYS
    assert event["run_id"] == "r-0007"
    assert event["result"] == "FAIL"
    assert event["message"] == "manifest: invalid JSON"
    assert all(
        event[key] is None
        for key in (
            "deployment_key",
            "service",
            "environment",
            "from_version",
            "to_version",
            "change_id",
            "actor",
        )
    )


def test_event_after_bind_matches_the_audit_contract(
    recorder: Recorder, audit_log: AuditLog
) -> None:
    manifest = make_manifest()
    recorder.bind(manifest)
    recorder.bind_from_version("1.4.2")
    recorder.step(Stage.VERIFY, StepResult.FAIL, "0/3 consecutive healthy")
    assert events(audit_log)[0] == {
        "seq": 1,
        "ts": "2026-11-01T10:15:01.000Z",
        "run_id": "r-0007",
        "deployment_key": manifest.deployment_key,
        "service": "orders-api",
        "environment": "staging",
        "stage": "verify",
        "result": "FAIL",
        "from_version": "1.4.2",
        "to_version": "1.5.0",
        "change_id": "CHG-1042",
        "actor": "catalin",
        "message": "0/3 consecutive healthy",
    }


def test_step_failing_to_reach_the_audit_log_is_not_reported(
    recorder: Recorder, audit_log: AuditLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_: dict[str, object]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(audit_log, "append", fail)
    with pytest.raises(OSError, match="disk full"):
        recorder.step(Stage.VALIDATE, StepResult.PASS, "ok")
    monkeypatch.undo()
    assert recorder.finish(Outcome.REJECTED).steps == []


def test_in_flight_carries_the_run_identity(recorder: Recorder, clock: FakeClock) -> None:
    manifest = make_manifest(health_check=HealthCheckSpec(attempts=7, required_consecutive=2))
    recorder.bind(manifest)
    recorder.bind_from_version("1.4.2")
    clock.advance(5)

    in_flight = recorder.in_flight(Stage.DEPLOY)

    assert in_flight.run_id == "r-0007"
    assert in_flight.deployment_key == manifest.deployment_key
    assert (in_flight.from_version, in_flight.to_version) == ("1.4.2", "1.5.0")
    assert in_flight.stage is Stage.DEPLOY
    assert in_flight.started_at == "2026-11-01T10:15:06.000Z"
    assert in_flight.health_check == manifest.health_check


def test_in_flight_of_a_first_deploy_has_no_from_version(recorder: Recorder) -> None:
    recorder.bind(make_manifest())
    assert recorder.in_flight(Stage.DEPLOY).from_version is None


def test_partial_identity_is_audited_and_reported(recorder: Recorder, audit_log: AuditLog) -> None:
    recorder.bind_environment("orders-api", "qa")
    recorder.step(Stage.VALIDATE, StepResult.FAIL, "bad manifest")
    event = events(audit_log)[0]
    assert (event["service"], event["environment"]) == ("orders-api", "qa")
    assert (event["actor"], event["to_version"]) == (None, None)
    result = recorder.finish(Outcome.REJECTED)
    assert (result.service, result.environment) == ("orders-api", "qa")


def test_recovery_identity_comes_from_the_ledger(recorder: Recorder, audit_log: AuditLog) -> None:
    spec = HealthCheckSpec(attempts=7, required_consecutive=2)
    recorder.bind_environment("orders-api", "staging", "oncall")
    recorder.bind_recovery("3f9a1c2b7e10", "1.5.0", "1.4.2", spec)
    recorder.step(Stage.RECOVER, StepResult.PASS, "restoring")

    event = events(audit_log)[0]
    assert event["actor"] == "oncall"
    assert event["deployment_key"] == "3f9a1c2b7e10"
    assert (event["from_version"], event["to_version"]) == ("1.5.0", "1.4.2")
    assert recorder.in_flight(Stage.ROLLBACK).health_check == spec


def test_recovery_with_nothing_to_restore_has_no_target_version(recorder: Recorder) -> None:
    recorder.bind_environment("orders-api", "staging", "oncall")
    recorder.bind_recovery(None, "9.9.9", None, HealthCheckSpec())
    in_flight = recorder.in_flight(Stage.ROLLBACK)
    assert (in_flight.deployment_key, in_flight.to_version) == (None, None)


def test_in_flight_before_bind_is_a_bug(recorder: Recorder) -> None:
    with pytest.raises(InvariantError):
        recorder.in_flight(Stage.DEPLOY)


def test_finish_of_a_rejected_run_has_no_identity(recorder: Recorder) -> None:
    recorder.step(Stage.VALIDATE, StepResult.FAIL, "bad manifest")
    result = recorder.finish(Outcome.REJECTED)
    assert result.run_id == "r-0007"
    assert result.exit_code is ExitCode.REJECTED
    assert result.rollback_status is RollbackStatus.NOT_NEEDED
    assert result.service is None
    assert result.final_state is None
    assert [step.stage for step in result.steps] == [Stage.VALIDATE]


def test_finish_reports_the_run_and_its_final_state(
    recorder: Recorder, audit_log: AuditLog
) -> None:
    manifest = make_manifest()
    recorder.bind(manifest)
    recorder.bind_from_version("1.4.2")
    recorder.step(Stage.VERIFY, StepResult.FAIL, "unhealthy")
    state = EnvironmentState(
        service="orders-api",
        environment="staging",
        status=EnvStatus.STABLE,
        active_version="1.4.2",
    )

    result = recorder.finish(
        Outcome.ROLLED_BACK, RollbackStatus.SUCCEEDED, state, next_action="investigate"
    )

    assert result.outcome is Outcome.ROLLED_BACK
    assert result.rollback_status is RollbackStatus.SUCCEEDED
    assert result.deployment_key == manifest.deployment_key
    assert (result.service, result.environment) == ("orders-api", "staging")
    assert result.change_id == "CHG-1042"
    assert (result.from_version, result.to_version) == ("1.4.2", "1.5.0")
    assert result.final_state == FinalState(EnvStatus.STABLE, "1.4.2")
    assert result.next_action == "investigate"
    assert result.actor == "catalin"
    assert [(s.seq, s.ts) for s in result.steps] == [
        (event["seq"], event["ts"]) for event in events(audit_log)[:-1]
    ]


def test_finish_closes_the_run_in_the_audit_log(recorder: Recorder, audit_log: AuditLog) -> None:
    recorder.bind(make_manifest())
    recorder.step(Stage.VERIFY, StepResult.FAIL, "unhealthy")

    result = recorder.finish(
        Outcome.ROLLED_BACK, RollbackStatus.SUCCEEDED, next_action="investigate"
    )

    closing = events(audit_log)[-1]
    assert closing["seq"] == 2
    assert closing["stage"] == "finish"
    assert closing["result"] == "FAIL"
    assert closing["message"] == "investigate"
    assert closing["outcome"] == "ROLLED_BACK"
    assert closing["rollback_status"] == "SUCCEEDED"
    assert closing["exit_code"] == 4
    assert closing["run_id"] == result.run_id
    assert closing["service"] == "orders-api"
    assert [step.stage for step in result.steps] == [Stage.VERIFY]


def test_finish_with_no_next_action_names_the_outcome(
    recorder: Recorder, audit_log: AuditLog
) -> None:
    recorder.finish(Outcome.SUCCEEDED)

    closing = events(audit_log)[-1]
    assert (closing["result"], closing["message"]) == ("PASS", "SUCCEEDED")


def test_finished_result_does_not_change_with_later_steps(recorder: Recorder) -> None:
    recorder.step(Stage.VALIDATE, StepResult.PASS, "ok")
    result = recorder.finish(Outcome.NO_OP)
    recorder.step(Stage.PREFLIGHT, StepResult.PASS, "ok")
    assert len(result.steps) == 1


def test_null_audit_writes_nothing(clock: FakeClock, state_dir: Path) -> None:
    Recorder(NullAudit(), clock, run_id="r-0007").finish(Outcome.VALID)
    assert not state_dir.exists()


def test_append_after_a_torn_line_keeps_the_new_event_readable(audit_log: AuditLog) -> None:
    audit_log.append({"run_id": "r-1"})
    with audit_log.path.open("a", encoding="utf-8") as stream:
        stream.write('{"run_id": "r-1", "mess')

    audit_log.append({"run_id": "r-2"})

    lines = audit_log.path.read_text(encoding="utf-8").splitlines()
    assert lines == ['{"run_id": "r-1"}', '{"run_id": "r-1", "mess', '{"run_id": "r-2"}']


def _event(run_id: str, ts: str, environment: str = "staging", **extra: Any) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "ts": ts,
        "service": "orders-api",
        "environment": environment,
        "from_version": None,
        "to_version": "1.5.0",
        "change_id": "CHG-1042",
        "actor": "catalin",
        **extra,
    }


def test_history_groups_events_by_run_newest_first(audit_log: AuditLog) -> None:
    audit_log.append(_event("r-1", "t1"))
    audit_log.append(_event("r-1", "t2", outcome="SUCCEEDED", rollback_status="X", exit_code=0))
    audit_log.append(_event("r-2", "t3", to_version="1.6.0"))

    history = audit_log.history("orders-api", "staging", limit=10)

    assert [run["run_id"] for run in history.runs] == ["r-2", "r-1"]
    newest, oldest = history.runs
    assert (newest["started_at"], newest["outcome"], newest["exit_code"]) == ("t3", None, None)
    assert newest["to_version"] == "1.6.0"
    assert (oldest["started_at"], oldest["outcome"], oldest["exit_code"]) == ("t1", "SUCCEEDED", 0)
    assert oldest["rollback_status"] == "X"
    assert len(oldest["events"]) == 2
    assert history.skipped_lines == 0


def test_history_limit_counts_runs_not_events(audit_log: AuditLog) -> None:
    for run_id in ("r-1", "r-2", "r-3"):
        audit_log.append(_event(run_id, "t1"))
        audit_log.append(_event(run_id, "t2"))

    history = audit_log.history("orders-api", "staging", limit=2)

    assert [run["run_id"] for run in history.runs] == ["r-3", "r-2"]
    assert all(len(run["events"]) == 2 for run in history.runs)


def test_history_only_lists_the_asked_environment(audit_log: AuditLog) -> None:
    audit_log.append(_event("r-1", "t1"))
    audit_log.append(_event("r-2", "t2", environment="prod"))

    assert [r["run_id"] for r in audit_log.history("orders-api", "prod", 10).runs] == ["r-2"]
    assert audit_log.history("other-api", "prod", 10).runs == []


@pytest.mark.parametrize(
    "line",
    ['{"run_id": "r-1", "mess', "17", '{"seq": 1}', '{"run_id": 7}', "not json"],
    ids=["torn", "fragment", "no-run-id", "bad-run-id", "garbage"],
)
def test_history_counts_lines_it_cannot_read(audit_log: AuditLog, line: str) -> None:
    audit_log.append(_event("r-1", "t1"))
    with audit_log.path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
    audit_log.append(_event("r-2", "t2"))

    history = audit_log.history("orders-api", "staging", 10)

    assert [run["run_id"] for run in history.runs] == ["r-2", "r-1"]
    assert history.skipped_lines == 1


def test_history_survives_bytes_that_are_not_text(audit_log: AuditLog) -> None:
    audit_log.append(_event("r-1", "t1"))
    with audit_log.path.open("ab") as stream:
        stream.write(b"\xff\xfe\n")

    assert audit_log.history("orders-api", "staging", 10).skipped_lines == 1


def test_history_with_no_audit_file_is_empty(audit_log: AuditLog) -> None:
    history = audit_log.history("orders-api", "staging", 10)
    assert (history.runs, history.skipped_lines) == ([], 0)
