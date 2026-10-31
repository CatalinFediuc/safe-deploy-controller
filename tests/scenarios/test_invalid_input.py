from typing import Any

import pytest

from conftest import World
from deployctl.controller import DeployOptions
from deployctl.errors import ManifestError
from deployctl.manifest import load_manifest
from deployctl.models import ExitCode, Outcome

INVALID_OVERRIDES: list[dict[str, Any]] = [
    {"service": "Orders API"},
    {"environment": "qa"},
    {"version": "1.5"},
    {"change_id": "1042"},
    {"requested_by": " "},
    {"artifact": {"path": "orders-api-1.4.2.bin", "sha256": "0" * 64}},
    {"artifact": {"path": "../../etc/hosts", "sha256": "0" * 64}},
    {"artifact": {"path": "absent.bin", "sha256": "0" * 64}},
    {"health_check": {"attempts": 0}},
    {"health_check": {"rollout_timeout_seconds": -1}},
    {"verison": "1.5.0"},
]


def snapshot(world: World) -> tuple[bytes | None, bytes | None]:
    return tuple(  # type: ignore[return-value]
        path.read_bytes() if path.exists() else None
        for path in (world.store.path, world.simulated.path)
    )


@pytest.mark.parametrize("overrides", INVALID_OVERRIDES)
def test_t3_t4_invalid_manifest_changes_nothing(world: World, overrides: dict[str, Any]) -> None:
    before = snapshot(world)
    result = world.controller().deploy(world.manifest("1.4.2", **overrides), DeployOptions())

    assert result.outcome is Outcome.REJECTED
    assert result.exit_code is ExitCode.REJECTED
    assert result.final_state is None
    assert snapshot(world) == before
    assert world.target.calls == []
    assert not world.store.lock_path.exists()


@pytest.mark.parametrize("overrides", INVALID_OVERRIDES)
def test_t3_t4_invalid_manifest_leaves_an_existing_env_identical(
    world: World, overrides: dict[str, Any]
) -> None:
    world.deploy("1.4.2")
    world.target.calls.clear()
    before = snapshot(world)
    result = world.controller().deploy(world.manifest("1.6.0", **overrides), DeployOptions())

    assert result.outcome is Outcome.REJECTED
    assert snapshot(world) == before
    assert world.target.calls == []


def test_t3_all_errors_are_listed_and_audited(world: World) -> None:
    path = world.manifest("1.5", environment="qa", change_id="1042")
    result = world.controller().deploy(path, DeployOptions())

    message = result.steps[0].message
    assert "environment: 'qa'" in message
    assert "version: '1.5'" in message
    assert "change_id: '1042'" in message


def test_rejected_run_is_audited_with_the_declared_identity(world: World) -> None:
    path = world.manifest("1.5", environment="qa")
    result = world.controller().deploy(path, DeployOptions())

    assert (result.service, result.environment) == ("orders-api", "qa")
    event = world.audit_events()[0]
    assert (event["stage"], event["result"]) == ("validate", "FAIL")
    assert (event["service"], event["environment"]) == ("orders-api", "qa")


@pytest.mark.parametrize("content", ["{not json", "[]", '{"service": 7}', '{"environment": ""}'])
def test_identity_that_cannot_be_read_stays_null(world: World, content: str) -> None:
    path = world.manifest("1.4.2")
    path.write_text(content, encoding="utf-8")
    result = world.controller().deploy(path, DeployOptions())
    assert result.outcome is Outcome.REJECTED
    assert (result.service, result.environment) == (None, None)
    event = world.audit_events()[0]
    assert (event["service"], event["environment"]) == (None, None)


def test_missing_manifest_is_rejected(world: World) -> None:
    result = world.controller().deploy(world.root / "absent.json", DeployOptions())
    assert result.outcome is Outcome.REJECTED


def test_manifest_error_carries_what_was_declared(world: World) -> None:
    path = world.manifest("1.5", service="Orders API", environment="qa")
    with pytest.raises(ManifestError) as raised:
        load_manifest(path, world.artifact_dir)
    assert (raised.value.service, raised.value.environment) == ("Orders API", "qa")
