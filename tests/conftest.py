import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from deployctl.audit import AuditLog
from deployctl.controller import DeploymentController, DeployOptions
from deployctl.health import FixtureHealthProbe
from deployctl.models import DeploymentResult, Manifest
from deployctl.state import StateStore
from deployctl.target import DeploymentTarget, SimulatedTarget


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


class SimulatedCrash(BaseException):
    """A hard kill: bypasses `except Exception`, as SIGKILL bypasses every handler."""


class RecordingTarget:
    """Logs every change that reaches the target."""

    def __init__(self, inner: DeploymentTarget) -> None:
        self.inner = inner
        self.calls: list[tuple[str, ...]] = []

    def running_version(self, env_key: str) -> str | None:
        return self.inner.running_version(env_key)

    def apply(self, env_key: str, version: str) -> None:
        self.calls.append(("apply", version))
        self.inner.apply(env_key, version)

    def remove(self, env_key: str) -> None:
        self.calls.append(("remove",))
        self.inner.remove(env_key)


class FaultyTarget:
    """Raises on the `call_no`-th call of one method, before or after it took effect."""

    def __init__(
        self,
        inner: DeploymentTarget,
        fail_on: str,
        call_no: int = 1,
        error: BaseException | None = None,
        after: bool = False,
    ) -> None:
        self.inner = inner
        self._fail_on = fail_on
        self._call_no = call_no
        self._error = error or RuntimeError("target exploded")
        self._after = after
        self._calls = 0

    def _guarded(self, method: str, call: Callable[[], Any]) -> Any:
        if method != self._fail_on:
            return call()
        self._calls += 1
        due = self._calls == self._call_no
        if due and not self._after:
            raise self._error
        result = call()
        if due:
            raise self._error
        return result

    def running_version(self, env_key: str) -> str | None:
        version: str | None = self._guarded(
            "running_version", lambda: self.inner.running_version(env_key)
        )
        return version

    def apply(self, env_key: str, version: str) -> None:
        self._guarded("apply", lambda: self.inner.apply(env_key, version))

    def remove(self, env_key: str) -> None:
        self._guarded("remove", lambda: self.inner.remove(env_key))


GOOD = [{"error_rate": 0.001, "p99_latency_ms": 100}]
BAD = [{"error_rate": 0.5, "p99_latency_ms": 2000}]
PROFILE: dict[str, list[Any]] = {
    "1.4.2": GOOD,
    "1.5.0": BAD,
    "1.5.1": ["timeout", *GOOD],
    "1.6.0": GOOD,
}
OUTAGE: dict[str, list[Any]] = dict.fromkeys(PROFILE, BAD)
ENV = "orders-api/staging"


class World:
    """A state dir, artifacts and a target, with one call per controller operation."""

    def __init__(self, root: Path, clock: FakeClock, run_ids: Callable[[], str]) -> None:
        self.root = root
        self.clock = clock
        self.run_ids = run_ids
        self.state_dir = root / "state"
        self.artifact_dir = root / "artifacts"
        self.artifact_dir.mkdir()
        self.store = StateStore(self.state_dir, clock)
        self.audit = AuditLog(self.state_dir)
        self.simulated = SimulatedTarget(self.state_dir, clock)
        self.target = RecordingTarget(self.simulated)
        self._profiles = count(1)

    def manifest(self, version: str, /, **overrides: Any) -> Path:
        artifact = self.artifact_dir / f"orders-api-{version}.bin"
        content = overrides.pop("artifact_content", f"orders-api {version}\n")
        artifact.write_text(content, encoding="utf-8")
        document = {
            "service": "orders-api",
            "environment": "staging",
            "version": version,
            "artifact": {
                "path": artifact.name,
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
            },
            "change_id": "CHG-1042",
            "requested_by": "catalin",
            "health_check": {
                "attempts": 5,
                "required_consecutive": 3,
                "interval_seconds": 0.1,
                "rollout_timeout_seconds": 5,
            },
        }
        path = self.root / f"manifest-{version}.json"
        path.write_text(json.dumps(document | overrides), encoding="utf-8")
        return path

    def controller(
        self,
        profile: dict[str, list[Any]] | None = None,
        target: DeploymentTarget | None = None,
    ) -> DeploymentController:
        path = self.root / f"profile-{next(self._profiles)}.json"
        versions = PROFILE if profile is None else profile
        path.write_text(json.dumps({"orders-api": versions}), encoding="utf-8")
        return DeploymentController(
            store=self.store,
            target=target or self.target,
            probe=FixtureHealthProbe(path),
            audit=self.audit,
            clock=self.clock,
            run_id_factory=self.run_ids,
        )

    def deploy(
        self,
        version: str,
        opts: DeployOptions | None = None,
        profile: dict[str, list[Any]] | None = None,
        target: DeploymentTarget | None = None,
    ) -> DeploymentResult:
        controller = self.controller(profile, target)
        return controller.deploy(self.manifest(version), self.artifact_dir, opts or DeployOptions())

    def recover(
        self,
        profile: dict[str, list[Any]] | None = None,
        target: DeploymentTarget | None = None,
        *,
        break_lock: bool = False,
    ) -> DeploymentResult:
        controller = self.controller(profile, target)
        return controller.recover(
            "orders-api", "staging", "operator says so", "oncall", break_lock=break_lock
        )

    def audit_events(self) -> list[dict[str, Any]]:
        lines = self.audit.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def ledger(self) -> Any:
        return self.store.load("orders-api", "staging")


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


@pytest.fixture
def world(tmp_path: Path, clock: FakeClock, run_ids: Callable[[], str]) -> World:
    return World(tmp_path, clock, run_ids)
