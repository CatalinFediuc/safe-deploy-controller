from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import FakeClock
from deployctl.audit import AuditLog
from deployctl.controller import DeploymentController, DeployOptions
from deployctl.health import FixtureHealthProbe
from deployctl.models import DeploymentResult, EnvStatus, FinalState, Outcome, RollbackStatus
from deployctl.state import StateStore
from deployctl.target import SimulatedTarget

EXAMPLES = Path(__file__).parents[2] / "examples"
HEALTHY = "default.json"
OUTAGE = "dependency-outage.json"


class Demo:
    """The `make demo` sequence: real example files, and a new process for every command."""

    def __init__(self, state_dir: Path, clock: FakeClock, run_ids: Callable[[], str]) -> None:
        self.state_dir = state_dir
        self.clock = clock
        self.run_ids = run_ids

    def controller(self, profile: str) -> DeploymentController:
        return DeploymentController(
            store=StateStore(self.state_dir, self.clock),
            target=SimulatedTarget(self.state_dir, self.clock),
            probe=FixtureHealthProbe(EXAMPLES / "health" / profile),
            audit=AuditLog(self.state_dir),
            clock=self.clock,
            run_id_factory=self.run_ids,
        )

    def deploy(self, manifest: str, profile: str = HEALTHY) -> DeploymentResult:
        path = EXAMPLES / "manifests" / f"{manifest}.json"
        return self.controller(profile).deploy(path, EXAMPLES / "artifacts", DeployOptions())


@pytest.fixture
def demo(state_dir: Path, clock: FakeClock, run_ids: Callable[[], str]) -> Demo:
    return Demo(state_dir, clock, run_ids)


def test_every_step_of_the_demo_ends_as_the_makefile_says(demo: Demo) -> None:
    assert demo.deploy("orders-api-1.4.2").outcome is Outcome.SUCCEEDED
    assert demo.deploy("orders-api-1.4.2").outcome is Outcome.NO_OP

    rolled_back = demo.deploy("orders-api-1.5.0")
    assert rolled_back.outcome is Outcome.ROLLED_BACK
    assert rolled_back.rollback_status is RollbackStatus.SUCCEEDED
    assert rolled_back.final_state == FinalState(EnvStatus.STABLE, "1.4.2")

    assert demo.deploy("orders-api-1.5.0").outcome is Outcome.BLOCKED
    assert demo.deploy("orders-api-1.5.1").outcome is Outcome.SUCCEEDED
    assert demo.deploy("invalid-schema").outcome is Outcome.REJECTED
    assert demo.deploy("checksum-mismatch").outcome is Outcome.REJECTED
    assert demo.deploy("path-traversal").outcome is Outcome.REJECTED

    outage = demo.deploy("orders-api-1.6.0", OUTAGE)
    assert outage.outcome is Outcome.ROLLBACK_FAILED
    assert outage.final_state == FinalState(EnvStatus.NEEDS_ATTENTION, "1.5.1")

    assert demo.deploy("orders-api-1.5.1").outcome is Outcome.BLOCKED

    recovered = demo.controller(HEALTHY).recover("orders-api", "staging", "outage over", "oncall")
    assert recovered.outcome is Outcome.RECOVERED
    assert recovered.final_state == FinalState(EnvStatus.STABLE, "1.5.1")
