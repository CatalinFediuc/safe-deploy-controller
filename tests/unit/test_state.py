import json
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from conftest import FakeClock
from deployctl.errors import LockHeld, StateCorruptError
from deployctl.models import EnvironmentState, EnvStatus, HealthCheckSpec, InFlight, Stage
from deployctl.state import MAX_KNOWN_GOOD_HISTORY, FileLock, LockInfo, StateStore

ACTIVE_SHA256 = "b" * 64
# Differs from every default, so a test cannot pass by falling back to one.
HEALTH_CHECK = HealthCheckSpec(
    attempts=7,
    required_consecutive=2,
    interval_seconds=0.1,
    timeout_seconds=1.5,
    max_error_rate=0.02,
    max_p99_latency_ms=400.0,
    rollout_timeout_seconds=30.0,
)


def stable_state(active_version: str = "1.4.2") -> EnvironmentState:
    return EnvironmentState(
        service="orders-api",
        environment="staging",
        active_version=active_version,
        active_artifact_sha256=ACTIVE_SHA256,
        last_known_good=active_version,
        known_good_history=[active_version],
        failed_versions=["1.5.0"],
        updated_at="2026-11-01T10:15:02.300Z",
        last_run_id="r-0007",
    )


def deploying_state() -> EnvironmentState:
    state = stable_state()
    state.status = EnvStatus.DEPLOYING
    state.in_flight = InFlight(
        run_id="r-0008",
        deployment_key="3f9a1c2b7e10",
        from_version="1.4.2",
        to_version="1.5.1",
        stage=Stage.DEPLOY,
        started_at="2026-11-01T10:16:00.000Z",
        health_check=HEALTH_CHECK,
    )
    return state


def valid_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "environments": {
            "orders-api/staging": {
                "status": "DEPLOYING",
                "active_version": "1.4.2",
                "active_artifact_sha256": ACTIVE_SHA256,
                "last_known_good": "1.4.2",
                "known_good_history": ["1.4.2"],
                "failed_versions": ["1.5.0"],
                "in_flight": {
                    "run_id": "r-0008",
                    "deployment_key": "3f9a1c2b7e10",
                    "from_version": "1.4.2",
                    "to_version": "1.5.1",
                    "stage": "deploy",
                    "started_at": "2026-11-01T10:16:00.000Z",
                    "health_check": {
                        "attempts": 7,
                        "required_consecutive": 2,
                        "interval_seconds": 0.1,
                        "timeout_seconds": 1.5,
                        "max_error_rate": 0.02,
                        "max_p99_latency_ms": 400.0,
                        "rollout_timeout_seconds": 30.0,
                    },
                },
                "updated_at": "2026-11-01T10:15:02.300Z",
                "last_run_id": "r-0007",
            }
        },
    }


def with_value(field: str, value: object) -> str:
    document = valid_document()
    *parents, leaf = field.split(".")
    section = document
    for parent in parents:
        section = section[parent]
    section[leaf] = value
    return json.dumps(document)


def without(field: str) -> str:
    document = valid_document()
    *parents, leaf = field.split(".")
    section = document
    for parent in parents:
        section = section[parent]
    del section[leaf]
    return json.dumps(document)


ENTRY = "environments.orders-api/staging"
CORRUPT_FILES = [
    "{not json",
    "",
    "[]",
    "[" * 100_000,
    with_value("schema_version", 2),
    with_value("schema_version", True),
    with_value("schema_version", 1.0),
    without("schema_version"),
    with_value("environments", []),
    without("environments"),
    with_value("environments", {"orders-api": {}}),
    with_value(ENTRY, "STABLE"),
    with_value(f"{ENTRY}.surprise", True),
    without(f"{ENTRY}.failed_versions"),
    with_value(f"{ENTRY}.status", "ON_FIRE"),
    with_value(f"{ENTRY}.status", ["STABLE"]),
    with_value(f"{ENTRY}.active_version", 142),
    with_value(f"{ENTRY}.active_artifact_sha256", 7),
    without(f"{ENTRY}.active_artifact_sha256"),
    with_value(f"{ENTRY}.known_good_history", "1.4.2"),
    with_value(f"{ENTRY}.failed_versions", [1]),
    with_value(f"{ENTRY}.in_flight", "r-0008"),
    without(f"{ENTRY}.in_flight.stage"),
    with_value(f"{ENTRY}.in_flight.stage", "launch"),
    with_value(f"{ENTRY}.in_flight.to_version", 7),
    with_value(f"{ENTRY}.in_flight.deployment_key", 7),
    with_value(f"{ENTRY}.in_flight.health_check", None),
    without(f"{ENTRY}.in_flight.health_check"),
    # A hand-edited threshold must not be able to loosen the verification of a rollback.
    without(f"{ENTRY}.in_flight.health_check.max_error_rate"),
    with_value(f"{ENTRY}.in_flight.health_check.max_error_rate", 5),
    with_value(f"{ENTRY}.in_flight.health_check.required_consecutive", 0),
    without(f"{ENTRY}.in_flight.health_check.rollout_timeout_seconds"),
    with_value(f"{ENTRY}.in_flight.health_check.rollout_timeout_seconds", -1),
    with_value(f"{ENTRY}.in_flight.health_check.attempts", True),
    with_value(f"{ENTRY}.in_flight.health_check.surprise", 1),
]


def test_valid_document_passes_the_shape_checks(store: StateStore) -> None:
    # Keeps the corruption cases honest: each one is this document with a single change.
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps(valid_document()), encoding="utf-8")
    assert store.load("orders-api", "staging") == deploying_state()


def test_load_without_a_file_is_a_blank_stable_environment(store: StateStore) -> None:
    state = store.load("orders-api", "staging")
    assert state == EnvironmentState(service="orders-api", environment="staging")
    assert state.status is EnvStatus.STABLE
    assert not store.path.exists()


def test_load_of_an_unknown_environment_is_blank(store: StateStore) -> None:
    store.save(stable_state())
    assert store.load("orders-api", "prod").active_version is None


@pytest.mark.parametrize("make_state", [stable_state, deploying_state])
def test_round_trip(store: StateStore, make_state: Any) -> None:
    state = make_state()
    store.save(state)
    assert store.load("orders-api", "staging") == state


def test_round_trip_restores_enums(store: StateStore) -> None:
    store.save(deploying_state())
    loaded = store.load("orders-api", "staging")
    assert loaded.status is EnvStatus.DEPLOYING
    assert loaded.in_flight is not None
    assert loaded.in_flight.stage is Stage.DEPLOY


def test_round_trip_keeps_the_health_thresholds(store: StateStore) -> None:
    store.save(deploying_state())
    loaded = store.load("orders-api", "staging")
    assert loaded.in_flight is not None
    assert loaded.in_flight.health_check == HEALTH_CHECK


def test_saved_file_matches_the_ledger_contract(store: StateStore) -> None:
    store.save(deploying_state())
    assert json.loads(store.path.read_text(encoding="utf-8")) == valid_document()


def test_save_keeps_other_environments(store: StateStore) -> None:
    prod = EnvironmentState(service="orders-api", environment="prod", active_version="1.3.0")
    store.save(prod)
    store.save(stable_state())
    assert store.load("orders-api", "prod") == prod
    assert store.load("orders-api", "staging") == stable_state()


def test_known_good_history_keeps_the_most_recent(store: StateStore) -> None:
    state = stable_state()
    state.known_good_history = [f"1.0.{patch}" for patch in range(MAX_KNOWN_GOOD_HISTORY + 5)]
    store.save(state)
    history = store.load("orders-api", "staging").known_good_history
    assert len(history) == MAX_KNOWN_GOOD_HISTORY
    assert history[-1] == state.known_good_history[-1]


def test_failed_replace_leaves_the_old_file_intact(
    store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save(stable_state("1.4.2"))
    before = store.path.read_bytes()

    def fail(*_: object) -> None:
        raise OSError("disk went away")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk went away"):
        store.save(stable_state("1.5.1"))

    assert store.path.read_bytes() == before
    assert not store.path.with_name("state.json.tmp").exists()


def test_leftover_tmp_file_from_a_crash_is_ignored(store: StateStore) -> None:
    store.save(stable_state())
    tmp = store.path.with_name("state.json.tmp")
    tmp.write_text("half a wri", encoding="utf-8")
    assert store.load("orders-api", "staging") == stable_state()
    store.save(stable_state("1.5.1"))
    assert store.load("orders-api", "staging").active_version == "1.5.1"
    assert not tmp.exists()


@pytest.mark.parametrize("content", CORRUPT_FILES, ids=range(len(CORRUPT_FILES)))
def test_corrupt_file_raises_and_is_not_overwritten(store: StateStore, content: str) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text(content, encoding="utf-8")

    with pytest.raises(StateCorruptError, match=r"state\.json"):
        store.load("orders-api", "staging")
    with pytest.raises(StateCorruptError):
        store.save(stable_state())

    assert store.path.read_text(encoding="utf-8") == content


def test_undecodable_file_raises(store: StateStore) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(b"\xff\xfe")
    with pytest.raises(StateCorruptError):
        store.load("orders-api", "staging")


def test_unreadable_path_raises(store: StateStore) -> None:
    store.path.mkdir(parents=True)
    with pytest.raises(StateCorruptError):
        store.load("orders-api", "staging")


def test_lock_records_its_owner(store: StateStore, clock: FakeClock) -> None:
    store.lock(owner="r-0007")
    metadata = json.loads(store.lock_path.read_text(encoding="utf-8"))
    assert metadata == {
        "owner": "r-0007",
        "pid": os.getpid(),
        "since": "2026-11-01T10:15:01.000Z",
    }


def test_lock_contention_raises_and_is_not_stolen(store: StateStore) -> None:
    store.lock(owner="r-0007")
    before = store.lock_path.read_bytes()

    with pytest.raises(LockHeld) as held:
        store.lock(owner="r-0008")

    assert held.value.owner == "r-0007"
    assert held.value.since == "2026-11-01T10:15:01.000Z"
    assert store.lock_path.read_bytes() == before


@pytest.mark.parametrize(
    "content",
    [b"", b"{half", b"[]", b'{"pid": 1}', b'{"owner": 7}', b"\xff"],
)
def test_unreadable_lockfile_still_counts_as_held(store: StateStore, content: bytes) -> None:
    store.lock_path.parent.mkdir(parents=True)
    store.lock_path.write_bytes(content)

    with pytest.raises(LockHeld) as held:
        store.lock(owner="r-0008")

    assert held.value.owner == "unknown"
    assert store.lock_path.read_bytes() == content


def test_lockfile_without_a_valid_since_keeps_its_owner(store: StateStore) -> None:
    store.lock_path.parent.mkdir(parents=True)
    store.lock_path.write_text('{"owner": "r-0007", "since": 5}', encoding="utf-8")
    with pytest.raises(LockHeld) as held:
        store.lock(owner="r-0008")
    assert (held.value.owner, held.value.since) == ("r-0007", None)


def test_only_one_of_many_concurrent_runs_gets_the_lock(store: StateStore) -> None:
    def attempt(run: int) -> bool:
        try:
            store.lock(owner=f"r-{run:04d}")
        except LockHeld:
            return False
        return True

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(attempt, range(64)))

    assert outcomes.count(True) == 1


def test_context_manager_releases_the_lock(store: StateStore) -> None:
    with store.lock(owner="r-0007"):
        assert store.lock_path.exists()
    assert not store.lock_path.exists()


def test_lock_is_released_even_on_a_base_exception(store: StateStore) -> None:
    with pytest.raises(KeyboardInterrupt), store.lock(owner="r-0007"):
        raise KeyboardInterrupt
    assert not store.lock_path.exists()


def test_lock_can_be_taken_again_after_release(store: StateStore) -> None:
    with store.lock(owner="r-0007"):
        pass
    with store.lock(owner="r-0008"):
        assert store.lock_path.exists()


def test_release_twice_is_harmless(store: StateStore) -> None:
    lock = store.lock(owner="r-0007")
    lock.release()
    lock.release()
    assert not store.lock_path.exists()


def test_release_leaves_a_lock_now_held_by_another_run(store: StateStore) -> None:
    stale = store.lock(owner="r-0007")
    store.break_lock()
    store.lock(owner="r-0008")

    stale.release()

    assert store.break_lock() == LockInfo(owner="r-0008", since="2026-11-01T10:15:01.000Z")


def test_break_lock_reports_the_holder_and_frees_the_lock(store: StateStore) -> None:
    store.lock(owner="r-0007")

    assert store.break_lock() == LockInfo(owner="r-0007", since="2026-11-01T10:15:01.000Z")

    assert not store.lock_path.exists()
    assert isinstance(store.lock(owner="r-0008"), FileLock)


def test_break_lock_clears_an_unreadable_lockfile(store: StateStore) -> None:
    store.lock_path.parent.mkdir(parents=True)
    store.lock_path.write_text("", encoding="utf-8")
    assert store.break_lock() == LockInfo(owner="unknown")
    assert not store.lock_path.exists()


def test_break_lock_without_a_lock_reports_nothing(store: StateStore) -> None:
    assert store.break_lock() is None
