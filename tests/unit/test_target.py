import json
import os
from pathlib import Path

import pytest

from conftest import FakeClock
from deployctl.errors import StateCorruptError
from deployctl.target import DeploymentTarget, SimulatedTarget

ENV = "orders-api/staging"
ENTRY = {"running_version": "1.4.2", "updated_at": "2026-11-01T10:15:01.000Z"}


@pytest.fixture
def target(state_dir: Path, clock: FakeClock) -> SimulatedTarget:
    return SimulatedTarget(state_dir, clock)


def test_simulated_target_satisfies_the_port(target: SimulatedTarget) -> None:
    port: DeploymentTarget = target  # checked by mypy
    assert port.running_version(ENV) is None


def test_nothing_runs_until_applied_and_reading_creates_no_file(target: SimulatedTarget) -> None:
    assert target.running_version(ENV) is None
    assert not target.path.exists()


def test_apply_is_seen_by_a_new_instance(
    target: SimulatedTarget, state_dir: Path, clock: FakeClock
) -> None:
    target.apply(ENV, "1.4.2")
    assert SimulatedTarget(state_dir, clock).running_version(ENV) == "1.4.2"


def test_apply_replaces_the_running_version(target: SimulatedTarget) -> None:
    target.apply(ENV, "1.4.2")
    target.apply(ENV, "1.5.0")
    assert target.running_version(ENV) == "1.5.0"


def test_apply_writes_the_documented_format(target: SimulatedTarget, clock: FakeClock) -> None:
    clock.advance(1.29)
    target.apply(ENV, "1.4.2")
    assert json.loads(target.path.read_text(encoding="utf-8")) == {
        ENV: {"running_version": "1.4.2", "updated_at": "2026-11-01T10:15:02.290Z"}
    }


def test_environments_do_not_affect_each_other(target: SimulatedTarget) -> None:
    target.apply(ENV, "1.4.2")
    target.apply("orders-api/prod", "1.3.0")
    target.remove(ENV)
    assert target.running_version(ENV) is None
    assert target.running_version("orders-api/prod") == "1.3.0"


def test_removing_what_is_not_running_writes_nothing(target: SimulatedTarget) -> None:
    target.remove(ENV)
    assert not target.path.exists()

    target.apply("orders-api/prod", "1.3.0")
    before = target.path.read_bytes()
    target.remove(ENV)
    assert target.path.read_bytes() == before


def test_failed_write_keeps_the_previous_running_version(
    target: SimulatedTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    target.apply(ENV, "1.4.2")

    def fail(*_: object) -> None:
        raise OSError("disk went away")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk went away"):
        target.apply(ENV, "1.5.0")

    assert target.running_version(ENV) == "1.4.2"


CORRUPT_FILES = [
    "",
    "{not json",
    "[]",
    "[" * 100_000,
    json.dumps({ENV: "1.4.2"}),
    json.dumps({ENV: {"running_version": "1.4.2"}}),
    json.dumps({ENV: {"updated_at": ENTRY["updated_at"]}}),
    json.dumps({ENV: ENTRY | {"extra": "kept by no one"}}),
    json.dumps({ENV: ENTRY | {"running_version": None}}),
    json.dumps({ENV: ENTRY | {"running_version": 142}}),
    json.dumps({ENV: ENTRY | {"updated_at": None}}),
]


@pytest.mark.parametrize("content", CORRUPT_FILES)
def test_corrupt_file_raises_and_is_never_overwritten(
    target: SimulatedTarget, content: str
) -> None:
    target.path.parent.mkdir(parents=True)
    target.path.write_text(content, encoding="utf-8")

    with pytest.raises(StateCorruptError, match=r"environment\.json"):
        target.running_version(ENV)
    with pytest.raises(StateCorruptError):
        target.apply(ENV, "1.5.0")
    with pytest.raises(StateCorruptError):
        target.remove(ENV)

    assert target.path.read_text(encoding="utf-8") == content


def test_non_utf8_file_counts_as_corrupt(target: SimulatedTarget) -> None:
    target.path.parent.mkdir(parents=True)
    target.path.write_bytes(b"\xff\xfe")
    with pytest.raises(StateCorruptError):
        target.running_version(ENV)
