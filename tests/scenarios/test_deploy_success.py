import hashlib

from conftest import ENV, World
from deployctl.models import EnvStatus, ExitCode, FinalState, Outcome, RollbackStatus


def test_t1_first_deploy_becomes_known_good(world: World) -> None:
    result = world.deploy("1.4.2")

    assert result.outcome is Outcome.SUCCEEDED
    assert result.exit_code is ExitCode.OK
    assert result.rollback_status is RollbackStatus.NOT_NEEDED
    assert result.final_state == FinalState(EnvStatus.STABLE, "1.4.2")
    assert (result.from_version, result.to_version) == (None, "1.4.2")
    state = world.ledger()
    assert state.status is EnvStatus.STABLE
    assert state.active_version == state.last_known_good == "1.4.2"
    assert state.known_good_history == ["1.4.2"]
    assert state.in_flight is None
    assert state.updated_at == "2026-11-01T10:15:01.200Z"  # two 0.1s waits between the probes
    assert state.last_run_id == result.run_id
    assert world.simulated.running_version(ENV) == "1.4.2"


def test_t1_audit_is_contiguous_and_in_pipeline_order(world: World) -> None:
    result = world.deploy("1.4.2")

    events = world.audit_events()
    assert [event["seq"] for event in events] == [1, 2, 3, 4, 5, 6]
    assert [event["stage"] for event in events] == [
        "validate",
        "preflight",
        "deploy",
        "verify",
        "complete",
        "finish",
    ]
    assert {event["result"] for event in events} == {"PASS"}
    assert (events[-1]["outcome"], events[-1]["exit_code"]) == ("SUCCEEDED", 0)
    assert {event["run_id"] for event in events} == {result.run_id}
    assert [step.seq for step in result.steps] == [1, 2, 3, 4, 5]


def test_t1_the_recorded_artifact_is_the_deployed_one(world: World) -> None:
    world.deploy("1.4.2")
    content = (world.artifact_dir / "orders-api-1.4.2.bin").read_bytes()
    assert world.ledger().active_artifact_sha256 == hashlib.sha256(content).hexdigest()


def test_t2_upgrade_extends_the_history(world: World) -> None:
    world.deploy("1.4.2")
    result = world.deploy("1.6.0")

    assert result.outcome is Outcome.SUCCEEDED
    assert (result.from_version, result.to_version) == ("1.4.2", "1.6.0")
    state = world.ledger()
    assert state.known_good_history == ["1.4.2", "1.6.0"]
    assert state.active_version == state.last_known_good == "1.6.0"
    assert world.simulated.running_version(ENV) == "1.6.0"


def test_flaky_first_probe_is_tolerated_and_shown(world: World) -> None:
    result = world.deploy("1.5.1")
    assert result.outcome is Outcome.SUCCEEDED
    verify_step = next(step for step in result.steps if step.stage == "verify")
    assert verify_step.message == "3/3 consecutive healthy after 4 probes"


def test_intent_is_saved_before_the_target_is_touched(world: World) -> None:
    seen: list[EnvStatus] = []
    real_apply = world.simulated.apply

    def apply(env_key: str, version: str) -> None:
        seen.append(world.ledger().status)
        real_apply(env_key, version)

    world.simulated.apply = apply  # type: ignore[method-assign]
    world.deploy("1.4.2")
    assert seen == [EnvStatus.DEPLOYING]
