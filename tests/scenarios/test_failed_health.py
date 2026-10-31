import pytest

from conftest import ENV, OUTAGE, PROFILE, FaultyTarget, World
from deployctl.errors import RecoveryRequired
from deployctl.models import EnvStatus, ExitCode, FinalState, Outcome, RollbackStatus


def test_t5_unhealthy_version_is_rolled_back_and_marked(world: World) -> None:
    world.deploy("1.4.2")
    result = world.deploy("1.5.0")

    assert result.outcome is Outcome.ROLLED_BACK
    assert result.exit_code is ExitCode.ROLLED_BACK
    assert result.rollback_status is RollbackStatus.SUCCEEDED
    assert result.final_state == FinalState(EnvStatus.STABLE, "1.4.2")
    assert result.next_action == (
        "1.5.0 is marked known-bad for orders-api/staging; "
        "investigate before retrying with --allow-known-bad"
    )
    state = world.ledger()
    assert state.status is EnvStatus.STABLE
    assert state.active_version == state.last_known_good == "1.4.2"
    assert state.known_good_history == ["1.4.2"]
    assert state.failed_versions == ["1.5.0"]
    assert state.in_flight is None
    assert world.simulated.running_version(ENV) == "1.4.2"


def test_t5_steps_match_the_documented_sequence(world: World) -> None:
    world.deploy("1.4.2")
    result = world.deploy("1.5.0")

    assert [(step.stage, step.result) for step in result.steps] == [
        ("validate", "PASS"),
        ("preflight", "PASS"),
        ("deploy", "PASS"),
        ("verify", "FAIL"),
        ("rollback", "PASS"),
        ("verify_rollback", "PASS"),
    ]
    assert result.steps[3].message == (
        "0/3 consecutive healthy after 3 probes (error_rate 0.500 > 0.010)"
    )
    assert world.target.calls == [("apply", "1.4.2"), ("apply", "1.5.0"), ("apply", "1.4.2")]


def test_t5_a_rollback_keeps_the_known_good_artifact(world: World) -> None:
    world.deploy("1.4.2")
    sha = world.ledger().active_artifact_sha256
    world.deploy("1.5.0")
    assert world.ledger().active_artifact_sha256 == sha


def test_t6_failed_first_deploy_leaves_nothing_running(world: World) -> None:
    result = world.deploy("1.5.0")

    assert result.outcome is Outcome.ROLLED_BACK
    assert result.exit_code is ExitCode.ROLLED_BACK
    assert result.rollback_status is RollbackStatus.NO_PREVIOUS_VERSION
    assert result.final_state == FinalState(EnvStatus.STABLE, None)
    state = world.ledger()
    assert state.status is EnvStatus.STABLE
    assert state.active_version is None
    assert state.active_artifact_sha256 is None
    assert state.failed_versions == ["1.5.0"]
    assert world.simulated.running_version(ENV) is None


def test_t7_failed_rollback_stops_and_recover_restores(world: World) -> None:
    world.deploy("1.4.2")
    result = world.deploy("1.6.0", profile=OUTAGE)

    assert result.outcome is Outcome.ROLLBACK_FAILED
    assert result.exit_code is ExitCode.NEEDS_ATTENTION
    assert result.rollback_status is RollbackStatus.FAILED
    assert result.final_state == FinalState(EnvStatus.NEEDS_ATTENTION, "1.4.2")
    assert "page a human" in (result.next_action or "")
    state = world.ledger()
    assert state.status is EnvStatus.NEEDS_ATTENTION
    assert state.in_flight is not None
    assert state.failed_versions == ["1.6.0"]
    calls = len(world.target.calls)

    blocked = world.deploy("1.5.1")
    assert blocked.outcome is Outcome.BLOCKED
    assert blocked.exit_code is ExitCode.BLOCKED
    assert len(world.target.calls) == calls

    recovered = world.recover()
    assert recovered.outcome is Outcome.RECOVERED
    assert recovered.exit_code is ExitCode.OK
    assert recovered.final_state == FinalState(EnvStatus.STABLE, "1.4.2")


def test_t8_failed_apply_rolls_back_without_marking(world: World) -> None:
    world.deploy("1.4.2")
    faulty = FaultyTarget(world.target, fail_on="apply")
    result = world.deploy("1.6.0", target=faulty)

    assert result.outcome is Outcome.ROLLED_BACK
    assert result.rollback_status is RollbackStatus.SUCCEEDED
    assert result.steps[2].message == "apply failed: target exploded"
    assert result.next_action == "1.6.0 was not marked known-bad; fix the cause and retry"
    state = world.ledger()
    assert (state.status, state.active_version) == (EnvStatus.STABLE, "1.4.2")
    assert state.failed_versions == []
    assert world.simulated.running_version(ENV) == "1.4.2"
    assert world.deploy("1.6.0").outcome is Outcome.SUCCEEDED


def test_half_applied_version_is_rolled_back_too(world: World) -> None:
    world.deploy("1.4.2")
    faulty = FaultyTarget(world.target, fail_on="apply", after=True)
    result = world.deploy("1.6.0", target=faulty)
    assert result.outcome is Outcome.ROLLED_BACK
    assert world.simulated.running_version(ENV) == "1.4.2"


def test_failed_apply_of_a_first_deploy_removes_what_landed(world: World) -> None:
    faulty = FaultyTarget(world.target, fail_on="apply", after=True)
    result = world.deploy("1.4.2", target=faulty)
    assert result.rollback_status is RollbackStatus.NO_PREVIOUS_VERSION
    assert world.simulated.running_version(ENV) is None


def test_stalled_rollout_rolls_back_without_marking(world: World) -> None:
    world.deploy("1.4.2")

    class NeverLands(FaultyTarget):
        def apply(self, env_key: str, version: str) -> None:
            if version == "1.6.0":
                return
            self.inner.apply(env_key, version)

    result = world.deploy("1.6.0", target=NeverLands(world.target, fail_on="none"))

    assert result.outcome is Outcome.ROLLED_BACK
    assert "rollout not finished after 5s" in result.steps[3].message
    assert result.next_action == "1.6.0 was not marked known-bad; fix the cause and retry"
    assert world.ledger().failed_versions == []


def test_version_overwritten_during_verification_is_not_marked(world: World) -> None:
    world.deploy("1.4.2")

    class Overwritten(FaultyTarget):
        def running_version(self, env_key: str) -> str | None:
            running = self.inner.running_version(env_key)
            if running == "1.6.0":
                self._reads = getattr(self, "_reads", 0) + 1
                return "1.6.0" if self._reads == 1 else "9.9.9"
            return running

    result = world.deploy("1.6.0", target=Overwritten(world.target, fail_on="none"))

    assert result.outcome is Outcome.ROLLED_BACK
    assert "running version is 9.9.9" in result.steps[3].message
    assert world.ledger().failed_versions == []


@pytest.mark.parametrize("responses", [["timeout"], ["error"]])
def test_probes_that_never_answer_still_mark_the_version(
    world: World, responses: list[object]
) -> None:
    world.deploy("1.4.2")
    result = world.deploy("1.6.0", profile=PROFILE | {"1.6.0": responses})
    assert result.outcome is Outcome.ROLLED_BACK
    assert world.ledger().failed_versions == ["1.6.0"]


def test_no_data_for_a_version_still_marks_it(world: World) -> None:
    world.deploy("1.4.2")
    result = world.deploy("1.6.0", profile={"1.4.2": PROFILE["1.4.2"]})
    assert result.outcome is Outcome.ROLLED_BACK
    assert world.ledger().failed_versions == ["1.6.0"]


def test_failed_rollback_apply_stops_at_needs_attention(world: World) -> None:
    world.deploy("1.4.2")
    faulty = FaultyTarget(world.target, fail_on="apply", call_no=2)
    result = world.deploy("1.5.0", target=faulty)

    assert result.outcome is Outcome.ROLLBACK_FAILED
    assert result.exit_code is ExitCode.NEEDS_ATTENTION
    assert result.steps[-1].message == "rollback failed: target exploded"
    assert world.ledger().status is EnvStatus.NEEDS_ATTENTION
    assert world.ledger().in_flight is not None


def test_failed_removal_stops_at_needs_attention(world: World) -> None:
    faulty = FaultyTarget(world.target, fail_on="remove")
    result = world.deploy("1.5.0", target=faulty)
    assert result.outcome is Outcome.ROLLBACK_FAILED
    assert world.ledger().status is EnvStatus.NEEDS_ATTENTION


def test_unhealthy_rollback_makes_exactly_one_attempt(world: World) -> None:
    world.deploy("1.4.2")
    world.target.calls.clear()
    world.deploy("1.6.0", profile=OUTAGE)
    assert world.target.calls == [("apply", "1.6.0"), ("apply", "1.4.2")]


def test_target_read_error_during_verify_rolls_back(world: World) -> None:
    world.deploy("1.4.2")
    # Reads: preflight, then the rollout wait. The rollback reads run on later calls.
    faulty = FaultyTarget(world.target, fail_on="running_version", call_no=2)
    result = world.deploy("1.6.0", target=faulty)

    assert result.outcome is Outcome.ROLLED_BACK
    assert result.steps[3].message == "verification failed: target exploded"
    assert world.ledger().failed_versions == []
    assert world.simulated.running_version(ENV) == "1.4.2"


def test_verify_error_during_rollback_leaves_explicit_state(world: World) -> None:
    world.deploy("1.4.2")
    # Reads: preflight, the rollout wait, three probes, then the rollback's rollout wait.
    faulty = FaultyTarget(world.target, fail_on="running_version", call_no=6)
    with pytest.raises(RecoveryRequired, match="run `deployctl recover`"):
        world.deploy("1.5.0", target=faulty)

    state = world.ledger()
    assert state.status is EnvStatus.ROLLING_BACK
    assert state.in_flight is not None
    assert state.in_flight.stage == "verify_rollback"
    assert world.audit_events()[-1]["message"] == "run stopped: target exploded"
    assert not world.store.lock_path.exists()
    assert world.recover().outcome is Outcome.RECOVERED


def test_ledger_failure_after_verify_leaves_explicit_state(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.deploy("1.4.2")
    real_save = world.store.save
    saves = iter([real_save, real_save])

    def save(state: object) -> None:
        next(saves, _disk_full)(state)  # type: ignore[arg-type]

    def _disk_full(_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(world.store, "save", save)
    with pytest.raises(RecoveryRequired, match="disk full"):
        world.deploy("1.6.0")
    monkeypatch.undo()

    state = world.ledger()
    assert state.status is EnvStatus.VERIFYING
    assert state.active_version == "1.4.2"
    assert world.recover().final_state == FinalState(EnvStatus.STABLE, "1.4.2")


def test_stage_of_a_stopped_run_falls_back_once_in_flight_is_cleared(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.deploy("1.4.2")
    saves = 0
    real_save = world.store.save

    def save(state: object) -> None:
        nonlocal saves
        saves += 1
        if saves == 3:  # DEPLOYING, VERIFYING, then the final STABLE
            raise OSError("disk full")
        real_save(state)  # type: ignore[arg-type]

    monkeypatch.setattr(world.store, "save", save)
    with pytest.raises(RecoveryRequired):
        world.deploy("1.6.0")
    assert world.audit_events()[-1]["stage"] == "recover"
