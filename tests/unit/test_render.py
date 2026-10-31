from deployctl.audit import AuditHistory
from deployctl.models import (
    DeploymentResult,
    EnvironmentState,
    EnvStatus,
    FinalState,
    HealthCheckSpec,
    InFlight,
    Outcome,
    RollbackStatus,
    Stage,
    StepRecord,
    StepResult,
)
from deployctl.render import (
    history_document,
    history_text,
    result_document,
    result_text,
    status_document,
    status_text,
)


def _step(seq: int, ms: str, stage: Stage, result: StepResult, message: str) -> StepRecord:
    return StepRecord(seq, f"2026-11-01T10:15:01.{ms}Z", stage, result, message)


def rolled_back() -> DeploymentResult:
    return DeploymentResult(
        run_id="r-0007",
        outcome=Outcome.ROLLED_BACK,
        rollback_status=RollbackStatus.SUCCEEDED,
        deployment_key="3f9a1c2b7e10",
        service="orders-api",
        environment="staging",
        change_id="CHG-1042",
        actor="catalin",
        from_version="1.4.2",
        to_version="1.5.0",
        final_state=FinalState(EnvStatus.STABLE, "1.4.2"),
        next_action="investigate",
        steps=[
            _step(1, "002", Stage.VALIDATE, StepResult.PASS, "manifest valid; sha256 verified"),
            _step(2, "020", Stage.VERIFY_ROLLBACK, StepResult.PASS, "3/3 consecutive healthy"),
        ],
    )


def test_result_text_matches_the_documented_layout() -> None:
    assert result_text(rolled_back()).splitlines() == [
        "deployctl run r-0007  key 3f9a1c2b7e10",
        "orders-api/staging  1.4.2 -> 1.5.0  change CHG-1042  by catalin",
        "",
        "   #  time (UTC)    stage            result  detail",
        "   1  10:15:01.002  validate         PASS    manifest valid; sha256 verified",
        "   2  10:15:01.020  verify_rollback  PASS    3/3 consecutive healthy",
        "",
        "OUTCOME ROLLED_BACK   rollback SUCCEEDED   env STABLE @ 1.4.2   exit 4",
        "NEXT    investigate",
    ]


def test_result_text_with_no_identity_or_final_state() -> None:
    text = result_text(DeploymentResult(run_id="r-0001", outcome=Outcome.REJECTED))
    lines = text.splitlines()
    assert lines[0] == "deployctl run r-0001  key -"
    assert lines[1] == "-/-  - -> -  change -  by -"
    assert lines[-1] == "OUTCOME REJECTED   rollback NOT_NEEDED   env not read   exit 3"


def test_result_text_with_nothing_running() -> None:
    result = DeploymentResult(
        run_id="r-0001",
        outcome=Outcome.RECOVERED,
        final_state=FinalState(EnvStatus.STABLE, None),
    )
    assert "env STABLE @ nothing" in result_text(result)


def test_declared_text_cannot_inject_lines_or_escapes() -> None:
    result = DeploymentResult(
        run_id="r-0001",
        outcome=Outcome.REJECTED,
        service="evil\n\x1b[2Jservice",
        steps=[_step(1, "000", Stage.VALIDATE, StepResult.FAIL, "bad\nline")],
    )
    text = result_text(result)
    assert "\x1b" not in text
    assert "evil\\n\\x1b[2Jservice" in text
    assert "bad\\nline" in text


def test_result_document_adds_the_exit_code() -> None:
    document = result_document(rolled_back())
    assert document["exit_code"] == 4
    assert type(document["exit_code"]) is int
    assert document["final_state"] == {"status": "STABLE", "active_version": "1.4.2"}
    assert document["steps"][0]["seq"] == 1


def _state(status: EnvStatus = EnvStatus.STABLE, active: str | None = "1.4.2") -> EnvironmentState:
    return EnvironmentState("orders-api", "staging", status=status, active_version=active)


def test_status_reports_drift_only_when_settled() -> None:
    assert status_document(_state(), "1.4.2")["drift"] is False
    assert status_document(_state(), "9.9.9")["drift"] is True
    assert status_document(_state(EnvStatus.VERIFYING), "1.5.0")["drift"] is False


def test_status_text() -> None:
    state = _state(EnvStatus.NEEDS_ATTENTION)
    state.failed_versions = ["1.5.0", "1.6.0"]
    state.in_flight = InFlight(
        run_id="r-0007",
        deployment_key=None,
        from_version="1.4.2",
        to_version="1.6.0",
        stage=Stage.VERIFY_ROLLBACK,
        started_at="t",
        health_check=HealthCheckSpec(),
    )
    lines = status_text(status_document(state, "1.4.2")).splitlines()
    assert lines[0] == "orders-api/staging  NEEDS_ATTENTION"
    assert "  running     1.4.2" in lines
    assert "  known-bad   1.5.0, 1.6.0" in lines
    assert "  drift       no" in lines
    assert "  in flight   r-0007 at verify_rollback" in lines


def test_status_text_with_nothing_recorded_yet() -> None:
    lines = status_text(status_document(_state(active=None), "9.9.9")).splitlines()
    assert "  known-bad   -" in lines
    assert "  in flight   -" in lines
    assert "  drift       yes" in lines


def _run(run_id: str, outcome: str | None) -> dict[str, object]:
    return {
        "run_id": run_id,
        "started_at": "2026-11-01T10:15:01.002Z",
        "from_version": None,
        "to_version": "1.4.2",
        "change_id": "CHG-1042",
        "actor": "catalin",
        "outcome": outcome,
        "rollback_status": None,
        "exit_code": None,
        "events": [],
    }


def test_history_text_says_when_a_run_left_no_outcome() -> None:
    history = AuditHistory(runs=[_run("r-2", None), _run("r-1", "SUCCEEDED")], skipped_lines=0)
    lines = history_text("orders-api", "staging", history).splitlines()
    assert lines[0] == "orders-api/staging: latest 2 runs, newest first"
    assert lines[2].split() == [
        "started", "(UTC)", "run", "versions", "change", "actor", "outcome",
    ]  # fmt: skip
    assert lines[3].endswith("no outcome recorded")
    assert lines[4].endswith("SUCCEEDED")
    assert "- -> 1.4.2" in lines[3]


def test_history_text_with_one_run_reads_properly() -> None:
    history = AuditHistory(runs=[_run("r-1", "NO_OP")], skipped_lines=0)
    first_line = history_text("orders-api", "staging", history).splitlines()[0]
    assert first_line == "orders-api/staging: latest 1 run, newest first"


def test_history_text_with_no_runs_escapes_the_name() -> None:
    history = AuditHistory(runs=[], skipped_lines=0)
    assert history_text("a\nb", "qa", history) == "no recorded runs for a\\nb/qa"


def test_history_document_reports_skipped_lines() -> None:
    document = history_document("orders-api", "qa", AuditHistory(runs=[], skipped_lines=2))
    assert document == {
        "service": "orders-api",
        "environment": "qa",
        "runs": [],
        "skipped_lines": 2,
    }
