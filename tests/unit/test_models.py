from conftest import make_manifest
from deployctl.errors import LockHeld, ManifestError
from deployctl.models import (
    EXIT_FOR_OUTCOME,
    DeploymentResult,
    EnvironmentState,
    ExitCode,
    Outcome,
)


def test_every_outcome_has_an_exit_code() -> None:
    assert set(EXIT_FOR_OUTCOME) == set(Outcome)


def test_exit_codes_match_the_documented_contract() -> None:
    assert {outcome: int(code) for outcome, code in EXIT_FOR_OUTCOME.items()} == {
        Outcome.SUCCEEDED: 0,
        Outcome.NO_OP: 0,
        Outcome.RECOVERED: 0,
        Outcome.VALID: 0,
        Outcome.REJECTED: 3,
        Outcome.ROLLED_BACK: 4,
        Outcome.ROLLBACK_FAILED: 5,
        Outcome.BLOCKED: 6,
        Outcome.INTERRUPTED: 130,
    }


def test_result_exit_code_follows_its_outcome() -> None:
    result = DeploymentResult(run_id="r-0001", outcome=Outcome.ROLLED_BACK)
    assert result.exit_code is ExitCode.ROLLED_BACK


def test_env_key_is_shared_by_manifest_and_state() -> None:
    state = EnvironmentState(service="orders-api", environment="staging")
    assert make_manifest().env_key == state.env_key == "orders-api/staging"


def test_deployment_key_is_stable_for_identical_inputs() -> None:
    assert make_manifest().deployment_key == make_manifest().deployment_key
    assert len(make_manifest().deployment_key) == 12


def test_deployment_key_changes_with_its_inputs() -> None:
    first = make_manifest(change_id="CHG-1042")
    second = make_manifest(change_id="CHG-1043")
    assert first.deployment_key != second.deployment_key


def test_state_defaults_are_not_shared_between_instances() -> None:
    first = EnvironmentState(service="orders-api", environment="staging")
    second = EnvironmentState(service="orders-api", environment="prod")
    first.failed_versions.append("1.5.0")
    assert second.failed_versions == []


def test_manifest_error_keeps_every_problem() -> None:
    error = ManifestError(["first", "second"])
    assert error.errors == ["first", "second"]
    assert str(error) == "first; second"


def test_lock_held_names_its_owner() -> None:
    error = LockHeld("r-0007", since="2026-11-01T10:15:01.002Z")
    assert error.owner == "r-0007"
    assert error.since == "2026-11-01T10:15:01.002Z"
    assert "r-0007" in str(error)
