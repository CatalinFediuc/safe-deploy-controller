import json
from typing import Any

import pytest

from conftest import ENV, OUTAGE, PROFILE, FaultyTarget, SimulatedCrash, World
from deployctl.controller import DeployOptions
from deployctl.errors import RecoveryRequired, StateCorruptError
from deployctl.models import (
    EnvStatus,
    ExitCode,
    FinalState,
    HealthCheckSpec,
    Outcome,
    RollbackStatus,
)

# (method, call number, after the call took effect) for deploying 1.5.0 over 1.4.2. Target reads
# during a deploy: preflight, the rollout wait, one per attempt, then the rollback's rollout wait.
CRASH_POINTS = [
    pytest.param("apply", 1, False, EnvStatus.DEPLOYING, id="intent-saved"),
    pytest.param("apply", 1, True, EnvStatus.DEPLOYING, id="after-apply"),
    pytest.param("running_version", 2, False, EnvStatus.VERIFYING, id="start-of-verify"),
    pytest.param("running_version", 4, False, EnvStatus.VERIFYING, id="mid-verify"),
    pytest.param("apply", 2, False, EnvStatus.ROLLING_BACK, id="before-rollback-apply"),
    pytest.param("apply", 2, True, EnvStatus.ROLLING_BACK, id="after-rollback-apply"),
    pytest.param("running_version", 6, False, EnvStatus.ROLLING_BACK, id="verifying-rollback"),
]


@pytest.mark.parametrize(("method", "call_no", "after", "status"), CRASH_POINTS)
def test_t11_crash_at_every_step_is_never_ambiguous(
    world: World, method: str, call_no: int, after: bool, status: EnvStatus
) -> None:
    world.deploy("1.4.2")
    world.target.calls.clear()
    faulty = FaultyTarget(
        world.target, fail_on=method, call_no=call_no, error=SimulatedCrash(), after=after
    )

    with pytest.raises(SimulatedCrash):
        world.deploy("1.5.0", target=faulty)

    state = world.ledger()
    assert state.status is status
    assert state.in_flight is not None
    assert state.in_flight.run_id == state.last_run_id
    assert state.active_version == "1.4.2"

    blocked = world.deploy("1.6.0")
    assert blocked.outcome is Outcome.BLOCKED
    assert blocked.next_action == "run `deployctl recover`"
    assert str(status) in blocked.steps[-1].message

    recovered = world.recover()
    assert recovered.outcome is Outcome.RECOVERED
    state = world.ledger()
    assert state.status is EnvStatus.STABLE
    assert state.in_flight is None
    assert world.simulated.running_version(ENV) == state.active_version == "1.4.2"
    assert state.last_known_good == "1.4.2"


def test_t11_crash_during_recover_can_be_recovered(world: World) -> None:
    world.deploy("1.4.2")
    with pytest.raises(SimulatedCrash):
        world.deploy("1.5.0", target=FaultyTarget(world.target, "apply", error=SimulatedCrash()))

    with pytest.raises(SimulatedCrash):
        world.recover(target=FaultyTarget(world.target, "apply", error=SimulatedCrash()))
    assert world.ledger().status is EnvStatus.ROLLING_BACK

    assert world.recover().outcome is Outcome.RECOVERED
    assert world.ledger().status is EnvStatus.STABLE


def test_t11_crash_of_a_first_deploy_recovers_to_nothing(world: World) -> None:
    with pytest.raises(SimulatedCrash):
        world.deploy(
            "1.4.2",
            target=FaultyTarget(world.target, "apply", error=SimulatedCrash(), after=True),
        )
    result = world.recover()
    assert result.outcome is Outcome.RECOVERED
    assert result.rollback_status is RollbackStatus.NO_PREVIOUS_VERSION
    assert result.final_state == FinalState(EnvStatus.STABLE, None)
    assert world.simulated.running_version(ENV) is None


def test_crash_does_not_mark_the_version_known_bad(world: World) -> None:
    world.deploy("1.4.2")
    with pytest.raises(SimulatedCrash):
        world.deploy(
            "1.6.0", target=FaultyTarget(world.target, "apply", error=SimulatedCrash(), after=True)
        )
    world.recover()
    assert world.ledger().failed_versions == []
    assert world.deploy("1.6.0").outcome is Outcome.SUCCEEDED


# Tighter than the defaults: only the stored thresholds can fail 1.4.2, at 0.001 errors.
STRICT: dict[str, Any] = {
    "attempts": 3,
    "required_consecutive": 3,
    "interval_seconds": 0.1,
    "max_error_rate": 0.0005,
    "rollout_timeout_seconds": 5,
}


def test_t21_recover_after_a_crash_uses_the_stored_thresholds(world: World) -> None:
    world.deploy("1.4.2")
    faulty = FaultyTarget(world.target, "apply", error=SimulatedCrash())
    with pytest.raises(SimulatedCrash):
        world.controller(target=faulty).deploy(
            world.manifest("1.5.0", health_check=STRICT), DeployOptions()
        )

    result = world.recover()

    assert result.outcome is Outcome.ROLLBACK_FAILED
    assert world.ledger().status is EnvStatus.NEEDS_ATTENTION
    in_flight = world.ledger().in_flight
    assert in_flight is not None
    assert in_flight.health_check == HealthCheckSpec(**STRICT)


def test_t21_recover_after_needs_attention_uses_the_stored_thresholds(world: World) -> None:
    world.deploy("1.4.2")
    world.controller(OUTAGE).deploy(world.manifest("1.6.0", health_check=STRICT), DeployOptions())
    assert world.ledger().status is EnvStatus.NEEDS_ATTENTION

    assert world.recover().outcome is Outcome.ROLLBACK_FAILED
    in_flight = world.ledger().in_flight
    assert in_flight is not None
    assert in_flight.health_check.max_error_rate == 0.0005


def test_recover_stays_linked_to_the_deployment_it_cleans_up(world: World) -> None:
    world.deploy("1.4.2")
    failed = world.deploy("1.6.0", profile=OUTAGE)

    recovered = world.recover()

    assert recovered.deployment_key == failed.deployment_key
    assert world.audit_events()[-1]["deployment_key"] == failed.deployment_key


def test_drift_recover_uses_the_default_thresholds(world: World) -> None:
    world.deploy("1.4.2")
    world.simulated.apply(ENV, "1.3.9")
    assert world.recover(PROFILE).outcome is Outcome.RECOVERED
    # The defaults sleep a whole second between probes; the manifest here uses 0.1.
    assert world.clock.sleeps[-2:] == [1.0, 1.0]


def test_t13_drift_with_nothing_known_good_removes_what_is_running(world: World) -> None:
    world.simulated.apply(ENV, "9.9.9")

    result = world.recover()

    assert result.outcome is Outcome.RECOVERED
    assert result.rollback_status is RollbackStatus.NO_PREVIOUS_VERSION
    assert (result.from_version, result.to_version) == ("9.9.9", None)
    assert result.deployment_key is None
    assert world.simulated.running_version(ENV) is None


def test_crash_while_removing_drift_keeps_a_loadable_record(world: World) -> None:
    world.simulated.apply(ENV, "9.9.9")
    with pytest.raises(SimulatedCrash):
        world.recover(target=FaultyTarget(world.target, "remove", error=SimulatedCrash()))

    state = world.ledger()
    assert state.status is EnvStatus.ROLLING_BACK
    assert state.in_flight is not None
    assert state.in_flight.to_version is None
    assert state.in_flight.deployment_key is None
    assert world.recover().outcome is Outcome.RECOVERED


def test_recover_is_a_no_op_when_there_is_nothing_to_do(world: World) -> None:
    world.deploy("1.4.2")
    world.target.calls.clear()
    before = world.store.path.read_bytes()

    result = world.recover()

    assert result.outcome is Outcome.NO_OP
    assert result.exit_code is ExitCode.OK
    assert world.target.calls == []
    assert world.store.path.read_bytes() == before
    assert "operator says so" in world.audit_events()[-1]["message"]


def test_recover_records_who_and_why(world: World) -> None:
    world.deploy("1.4.2")
    world.simulated.apply(ENV, "1.3.9")
    run_id = world.recover().run_id

    events = [event for event in world.audit_events() if event["run_id"] == run_id]
    assert [event["stage"] for event in events] == ["recover", "rollback", "verify_rollback"]
    assert {event["actor"] for event in events} == {"oncall"}
    assert "reason: operator says so" in events[0]["message"]


def test_failed_recover_stops_again_and_can_be_retried(world: World) -> None:
    world.deploy("1.4.2")
    world.deploy("1.6.0", profile=OUTAGE)

    again = world.recover(OUTAGE)
    assert again.outcome is Outcome.ROLLBACK_FAILED
    assert again.exit_code is ExitCode.NEEDS_ATTENTION
    assert world.ledger().status is EnvStatus.NEEDS_ATTENTION

    assert world.recover().outcome is Outcome.RECOVERED


def test_recover_stopped_by_a_target_error_stays_explicit(world: World) -> None:
    world.deploy("1.4.2")
    world.deploy("1.6.0", profile=OUTAGE)
    # Reads: recover's own check, then the rollout wait.
    faulty = FaultyTarget(world.target, "running_version", call_no=2)

    with pytest.raises(RecoveryRequired, match="run `deployctl recover`"):
        world.recover(target=faulty)

    assert world.ledger().status is EnvStatus.ROLLING_BACK


def test_interrupted_deploy_stops_with_explicit_state(world: World) -> None:
    world.deploy("1.4.2")
    world.target.calls.clear()
    faulty = FaultyTarget(world.target, "running_version", call_no=3, error=KeyboardInterrupt())

    result = world.deploy("1.5.0", target=faulty)

    assert result.outcome is Outcome.INTERRUPTED
    assert result.exit_code is ExitCode.INTERRUPTED
    assert result.next_action == "run `deployctl recover`"
    assert result.final_state == FinalState(EnvStatus.VERIFYING, "1.4.2")
    assert result.steps[-1].stage == "verify"
    assert result.steps[-1].message == "interrupted; taking no further action"
    assert world.target.calls == [("apply", "1.5.0")]  # no rollback after being told to stop
    assert not world.store.lock_path.exists()


def test_interrupted_preflight_changed_nothing(world: World) -> None:
    world.deploy("1.4.2")
    faulty = FaultyTarget(world.target, "running_version", error=KeyboardInterrupt())

    result = world.deploy("1.6.0", target=faulty)

    assert result.outcome is Outcome.INTERRUPTED
    assert result.next_action == "nothing changed; safe to run again"
    assert result.steps[-1].stage == "preflight"
    assert world.ledger().status is EnvStatus.STABLE


def test_interrupted_recover_takes_no_further_action(world: World) -> None:
    world.deploy("1.4.2")
    world.deploy("1.6.0", profile=OUTAGE)
    world.target.calls.clear()
    faulty = FaultyTarget(world.target, "apply", error=KeyboardInterrupt())

    result = world.recover(target=faulty)

    assert result.outcome is Outcome.INTERRUPTED
    assert result.next_action == "run `deployctl recover`"
    assert world.ledger().status is EnvStatus.ROLLING_BACK


def test_inconsistent_ledger_is_still_blocked_and_recoverable(world: World) -> None:
    world.deploy("1.4.2")
    state = world.ledger()
    state.status = EnvStatus.DEPLOYING
    world.store.save(state)

    blocked = world.deploy("1.6.0")
    assert blocked.outcome is Outcome.BLOCKED
    assert blocked.steps[-1].message == "orders-api/staging is DEPLOYING"
    assert world.recover().outcome is Outcome.RECOVERED


def corrupt_target_file(world: World) -> None:
    world.simulated.path.write_text("{not json", encoding="utf-8")


def test_repair_a_corrupt_target_file_by_fixing_it(world: World) -> None:
    world.deploy("1.4.2")
    good = world.simulated.path.read_text(encoding="utf-8")
    corrupt_target_file(world)

    with pytest.raises(StateCorruptError):
        world.deploy("1.6.0")
    with pytest.raises(StateCorruptError):
        world.recover()
    assert world.simulated.path.read_text(encoding="utf-8") == "{not json"
    assert world.audit_events()[-1]["message"].startswith("refused: ")

    world.simulated.path.write_text(good, encoding="utf-8")

    assert world.recover().outcome is Outcome.NO_OP
    assert world.deploy("1.6.0").outcome is Outcome.SUCCEEDED


def test_repair_a_corrupt_target_file_by_deleting_it(world: World) -> None:
    world.deploy("1.4.2")
    corrupt_target_file(world)
    with pytest.raises(StateCorruptError):
        world.recover()

    world.simulated.path.unlink()

    blocked = world.deploy("1.6.0")
    assert blocked.outcome is Outcome.BLOCKED
    assert blocked.steps[-1].message == "drift: recorded 1.4.2, running None"
    assert world.recover().final_state == FinalState(EnvStatus.STABLE, "1.4.2")
    assert world.simulated.running_version(ENV) == "1.4.2"
    assert world.deploy("1.6.0").outcome is Outcome.SUCCEEDED


def test_deleting_the_target_file_needs_a_recover_per_environment(world: World) -> None:
    world.deploy("1.4.2")
    prod = world.manifest("1.4.2", environment="prod")
    world.controller().deploy(prod, DeployOptions())
    world.simulated.path.unlink()

    world.recover()

    assert world.simulated.running_version(ENV) == "1.4.2"
    assert world.simulated.running_version("orders-api/prod") is None
    blocked = world.controller().deploy(
        world.manifest("1.6.0", environment="prod"), DeployOptions()
    )
    assert blocked.outcome is Outcome.BLOCKED
    assert json.loads(world.simulated.path.read_text(encoding="utf-8")).keys() == {ENV}
