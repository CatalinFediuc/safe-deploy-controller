from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from deployctl.audit import AuditLog
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


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def store(state_dir: Path, clock: FakeClock) -> StateStore:
    return StateStore(state_dir, clock)


@pytest.fixture
def audit_log(state_dir: Path) -> AuditLog:
    return AuditLog(state_dir)
