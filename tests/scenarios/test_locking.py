import json

import pytest

from conftest import FaultyTarget, SimulatedCrash, World
from deployctl.controller import DeployOptions
from deployctl.models import EnvStatus, ExitCode, Outcome


def hold_lock(world: World, owner: str = "r-0099") -> str:
    world.state_dir.mkdir(exist_ok=True)
    content = json.dumps({"owner": owner, "pid": 4242, "since": "2026-11-01T09:00:00.000Z"})
    world.store.lock_path.write_text(content, encoding="utf-8")
    return content


def test_t12_held_lock_blocks_a_deploy_and_is_not_stolen(world: World) -> None:
    content = hold_lock(world)

    result = world.deploy("1.4.2")

    assert result.outcome is Outcome.BLOCKED
    assert result.exit_code is ExitCode.BLOCKED
    assert result.steps[-1].message == "lock held by r-0099 since 2026-11-01T09:00:00.000Z"
    assert "--break-lock" in (result.next_action or "")
    assert world.store.lock_path.read_text(encoding="utf-8") == content
    assert world.target.calls == []


def test_t12_held_lock_blocks_recover_too(world: World) -> None:
    world.deploy("1.4.2")
    content = hold_lock(world)

    result = world.recover()

    assert result.outcome is Outcome.BLOCKED
    assert result.steps[-1].stage == "recover"
    assert world.store.lock_path.read_text(encoding="utf-8") == content


def test_t12_break_lock_clears_a_stale_lock_and_audits_it(world: World) -> None:
    world.deploy("1.4.2")
    hold_lock(world)

    result = world.recover(break_lock=True)

    assert result.outcome is Outcome.NO_OP
    assert not world.store.lock_path.exists()
    event = next(event for event in world.audit_events() if event["run_id"] == result.run_id)
    assert event["stage"] == "recover"
    assert event["actor"] == "oncall"
    assert event["message"] == "lock broken, held by r-0099 since 2026-11-01T09:00:00.000Z"
    assert world.deploy("1.6.0").outcome is Outcome.SUCCEEDED


def test_hard_kill_leaves_a_stale_lock_that_recover_can_clear(world: World) -> None:
    world.deploy("1.4.2")
    with pytest.raises(SimulatedCrash):
        world.deploy(
            "1.6.0", target=FaultyTarget(world.target, "apply", error=SimulatedCrash(), after=True)
        )
    hold_lock(world)  # a real SIGKILL skips the release that the crash above still ran

    assert world.deploy("1.6.0").outcome is Outcome.BLOCKED
    assert world.recover().outcome is Outcome.BLOCKED
    assert world.ledger().status is EnvStatus.DEPLOYING

    result = world.recover(break_lock=True)

    assert result.outcome is Outcome.RECOVERED
    assert world.ledger().status is EnvStatus.STABLE
    assert not world.store.lock_path.exists()


def test_break_lock_with_no_lock_is_harmless(world: World) -> None:
    world.deploy("1.4.2")
    result = world.recover(break_lock=True)
    assert result.steps[0].message == "no lock to break"
    assert result.outcome is Outcome.NO_OP


def test_lock_is_released_after_every_kind_of_run(world: World) -> None:
    world.deploy("1.4.2")
    world.deploy("1.4.2")
    world.deploy("1.5.0")
    world.deploy("1.5.0")
    world.recover()
    assert not world.store.lock_path.exists()


def test_invalid_manifest_never_waits_for_the_lock(world: World) -> None:
    hold_lock(world)
    path = world.manifest("1.5")
    result = world.controller().deploy(path, world.artifact_dir, DeployOptions())
    assert result.outcome is Outcome.REJECTED


def test_lock_is_held_while_the_target_changes(world: World) -> None:
    seen: list[bool] = []
    real_apply = world.simulated.apply

    def apply(env_key: str, version: str) -> None:
        seen.append(world.store.lock_path.exists())
        real_apply(env_key, version)

    world.simulated.apply = apply  # type: ignore[method-assign]
    world.deploy("1.4.2")
    assert seen == [True]
    assert world.ledger().status is EnvStatus.STABLE
