from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from deployctl.audit import AuditLog
from deployctl.models import Manifest
from deployctl.state import StateStore


class FakeClock:
    """Moves only when told to, and records sleeps instead of taking them."""

    def __init__(self) -> None:
        self.current = datetime(2026, 11, 1, 10, 15, 1, tzinfo=UTC)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def make_manifest(**overrides: Any) -> Manifest:
    fields: dict[str, Any] = {
        "service": "orders-api",
        "environment": "staging",
        "version": "1.5.0",
        "artifact_path": Path("orders-api-1.5.0.bin"),
        "artifact_sha256": "a" * 64,
        "change_id": "CHG-1042",
        "requested_by": "catalin",
    }
    return Manifest(**(fields | overrides))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def run_ids() -> Callable[[], str]:
    """Deterministic stand-in for the real run id factory: r-0001, r-0002, ..."""
    numbers = count(1)
    return lambda: f"r-{next(numbers):04d}"


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def store(state_dir: Path, clock: FakeClock) -> StateStore:
    return StateStore(state_dir, clock)


@pytest.fixture
def audit_log(state_dir: Path) -> AuditLog:
    return AuditLog(state_dir)
