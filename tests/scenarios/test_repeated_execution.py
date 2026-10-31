import pytest

from conftest import ENV, GOOD, PROFILE, World
from deployctl.controller import DeployOptions
from deployctl.errors import StateCorruptError
from deployctl.models import EnvStatus, ExitCode, FinalState, Outcome, StepResult


def test_t9_same_manifest_twice_is_a_no_op(world: World) -> None:
    first = world.deploy("1.4.2")
    before = world.store.path.read_bytes()
    second = world.deploy("1.4.2")

    assert second.outcome is Outcome.NO_OP
    assert second.exit_code is ExitCode.OK
    assert second.final_state == FinalState(EnvStatus.STABLE, "1.4.2")
    assert second.steps[-1].result is StepResult.SKIP
    assert second.deployment_key == first.deployment_key
    assert second.run_id != first.run_id
    assert world.target.calls == [("apply", "1.4.2")]
    assert world.store.path.read_bytes() == before
    assert {event["run_id"] for event in world.audit_events()} == {first.run_id, second.run_id}


def test_t10_known_bad_is_blocked(world: World) -> None:
    world.deploy("1.4.2")
    world.deploy("1.5.0")
    world.target.calls.clear()
    result = world.deploy("1.5.0")

    assert result.outcome is Outcome.BLOCKED
    assert result.exit_code is ExitCode.BLOCKED
    assert result.next_action == "investigate, then retry with --allow-known-bad"
    assert world.target.calls == []


def test_t10_allow_known_bad_runs_the_pipeline(world: World) -> None:
    world.deploy("1.4.2")
    world.deploy("1.5.0")
    result = world.deploy("1.5.0", DeployOptions(allow_known_bad=True))
    assert result.outcome is Outcome.ROLLED_BACK


def test_known_bad_is_not_listed_twice(world: World) -> None:
    world.deploy("1.4.2")
    world.deploy("1.5.0")
    world.deploy("1.5.0", DeployOptions(allow_known_bad=True))
    assert world.ledger().failed_versions == ["1.5.0"]


def test_success_clears_the_known_bad_mark(world: World) -> None:
    world.deploy("1.4.2")
    world.deploy("1.5.0")
    world.deploy("1.6.0")
    world.deploy("1.5.0", DeployOptions(allow_known_bad=True, allow_downgrade=True))
    fixed = PROFILE | {"1.5.0": GOOD}
    world.deploy("1.5.0", DeployOptions(allow_known_bad=True, allow_downgrade=True), fixed)

    state = world.ledger()
    assert state.failed_versions == []
    assert state.active_version == "1.5.0"
    assert world.deploy("1.5.0").outcome is Outcome.NO_OP


def test_clearing_one_mark_keeps_the_others(world: World) -> None:
    world.deploy("1.4.2")
    world.deploy("1.5.0")
    world.deploy("1.6.0", profile=PROFILE | {"1.6.0": ["error"]})
    fixed = PROFILE | {"1.5.0": GOOD}
    world.deploy("1.5.0", DeployOptions(allow_known_bad=True), fixed)
    assert world.ledger().failed_versions == ["1.6.0"]


def test_t13_drift_blocks_and_recover_reconciles(world: World) -> None:
    world.deploy("1.4.2")
    world.simulated.apply(ENV, "1.3.9")
    before = world.store.path.read_bytes()

    blocked = world.deploy("1.6.0")
    assert blocked.outcome is Outcome.BLOCKED
    assert blocked.steps[-1].message == "drift: recorded 1.4.2, running 1.3.9"
    assert world.store.path.read_bytes() == before

    recovered = world.recover()
    assert recovered.outcome is Outcome.RECOVERED
    assert (recovered.from_version, recovered.to_version) == ("1.3.9", "1.4.2")
    assert recovered.final_state == FinalState(EnvStatus.STABLE, "1.4.2")
    assert world.simulated.running_version(ENV) == "1.4.2"
    assert world.ledger().failed_versions == []
    assert world.deploy("1.6.0").outcome is Outcome.SUCCEEDED


def test_t13_vanished_service_is_drift_too(world: World) -> None:
    world.deploy("1.4.2")
    world.simulated.remove(ENV)
    assert world.deploy("1.4.2").outcome is Outcome.BLOCKED
    assert world.recover().outcome is Outcome.RECOVERED
    assert world.simulated.running_version(ENV) == "1.4.2"


def test_t14_corrupt_ledger_is_refused_and_kept(world: World) -> None:
    world.state_dir.mkdir()
    world.store.path.write_text("{not json", encoding="utf-8")

    with pytest.raises(StateCorruptError):
        world.deploy("1.4.2")

    assert world.store.path.read_text(encoding="utf-8") == "{not json"
    assert world.target.calls == []
    assert not world.store.lock_path.exists()
    event = world.audit_events()[-1]
    assert (event["stage"], event["result"]) == ("preflight", "FAIL")
    assert event["message"].startswith("refused: ")


def test_t17_dry_run_changes_nothing(world: World) -> None:
    world.deploy("1.4.2")
    world.target.calls.clear()
    before = (world.store.path.read_bytes(), world.simulated.path.read_bytes())

    result = world.deploy("1.6.0", DeployOptions(dry_run=True))

    assert result.outcome is Outcome.NO_OP
    assert result.exit_code is ExitCode.OK
    assert result.next_action == "dry-run: no changes made"
    assert world.target.calls == []
    assert (world.store.path.read_bytes(), world.simulated.path.read_bytes()) == before
    assert not world.store.lock_path.exists()


def test_t17_dry_run_still_applies_the_gates(world: World) -> None:
    world.deploy("1.4.2")
    world.deploy("1.5.0")
    result = world.deploy("1.5.0", DeployOptions(dry_run=True))
    assert result.outcome is Outcome.BLOCKED


def test_t18_downgrade_is_blocked(world: World) -> None:
    world.deploy("1.6.0")
    world.target.calls.clear()
    result = world.deploy("1.4.2")
    assert result.outcome is Outcome.BLOCKED
    assert result.next_action == "use --allow-downgrade if this is intended"
    assert world.target.calls == []


def test_t18_allow_downgrade_runs_the_pipeline(world: World) -> None:
    world.deploy("1.6.0")
    result = world.deploy("1.4.2", DeployOptions(allow_downgrade=True))
    assert result.outcome is Outcome.SUCCEEDED
    assert world.ledger().last_known_good == "1.4.2"


def test_t18_versions_compare_as_numbers(world: World) -> None:
    world.deploy("1.4.2")
    higher = world.deploy("1.10.0", profile=PROFILE | {"1.10.0": GOOD})
    assert higher.outcome is Outcome.SUCCEEDED


def test_t20_rebuilt_artifact_at_the_active_version_is_blocked(world: World) -> None:
    world.deploy("1.4.2")
    sha = world.ledger().active_artifact_sha256
    world.target.calls.clear()

    path = world.manifest("1.4.2", artifact_content="rebuilt\n")
    result = world.controller().deploy(path, world.artifact_dir, DeployOptions())

    assert result.outcome is Outcome.BLOCKED
    assert result.exit_code is ExitCode.BLOCKED
    assert result.steps[-1].message == "1.4.2 already active with a different artifact"
    assert world.target.calls == []
    assert world.ledger().active_artifact_sha256 == sha


@pytest.mark.parametrize(
    ("opts", "suffix"),
    [
        (DeployOptions(), ""),
        (DeployOptions(dry_run=True), "; --dry-run"),
        (DeployOptions(allow_known_bad=True), "; --allow-known-bad"),
        (DeployOptions(allow_downgrade=True), "; --allow-downgrade"),
        (
            DeployOptions(allow_known_bad=True, allow_downgrade=True),
            "; --allow-known-bad; --allow-downgrade",
        ),
    ],
    ids=["none", "dry-run", "known-bad", "downgrade", "both"],
)
def test_overrides_and_dry_run_are_audited(world: World, opts: DeployOptions, suffix: str) -> None:
    world.deploy("1.4.2")
    run_id = world.deploy("1.6.0", opts).run_id

    message = next(
        event["message"]
        for event in world.audit_events()
        if event["run_id"] == run_id and event["stage"] == "preflight"
    )
    assert message == f"STABLE @ 1.4.2; lock acquired{suffix}"
