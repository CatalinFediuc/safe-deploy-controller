import json
import os
from dataclasses import dataclass
from pathlib import Path

from deployctl.clock import Clock, format_timestamp
from deployctl.errors import InvariantError
from deployctl.models import (
    DeploymentResult,
    EnvironmentState,
    FinalState,
    HealthCheckSpec,
    InFlight,
    Manifest,
    Outcome,
    RollbackStatus,
    Stage,
    StepRecord,
    StepResult,
)


class AuditLog:
    """Append-only JSONL trail; each event is on disk before the run moves on."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "audit.jsonl"

    def append(self, event: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


@dataclass
class _RunIdentity:
    # All None until the manifest is bound: rejected runs are audited too.
    deployment_key: str | None = None
    service: str | None = None
    environment: str | None = None
    from_version: str | None = None
    to_version: str | None = None
    change_id: str | None = None
    actor: str | None = None
    health_check: HealthCheckSpec | None = None


class Recorder:
    """Numbers one run's steps, writes each to the audit log and builds the final result."""

    def __init__(self, audit: AuditLog, clock: Clock, run_id: str) -> None:
        self.run_id = run_id
        self._audit = audit
        self._clock = clock
        self._identity = _RunIdentity()
        self._steps: list[StepRecord] = []

    def bind(self, manifest: Manifest) -> None:
        self._identity = _RunIdentity(
            deployment_key=manifest.deployment_key,
            service=manifest.service,
            environment=manifest.environment,
            to_version=manifest.version,
            change_id=manifest.change_id,
            actor=manifest.requested_by,
            health_check=manifest.health_check,
        )

    def bind_environment(
        self, service: str | None, environment: str | None, actor: str | None = None
    ) -> None:
        """Partial identity, for rejected runs and `recover`, which have no valid manifest."""
        self._identity = _RunIdentity(service=service, environment=environment, actor=actor)

    def bind_from_version(self, version: str | None) -> None:
        """Set once state is read: the manifest cannot say what is running today."""
        self._identity.from_version = version

    def bind_recovery(
        self,
        deployment_key: str | None,
        from_version: str | None,
        to_version: str | None,
        health_check: HealthCheckSpec,
    ) -> None:
        """Set once state is read: `recover` learns all of this from the ledger and the target."""
        self._identity.deployment_key = deployment_key
        self._identity.from_version = from_version
        self._identity.to_version = to_version
        self._identity.health_check = health_check

    def step(self, stage: Stage, result: StepResult, message: str) -> None:
        record = StepRecord(
            seq=len(self._steps) + 1,
            ts=format_timestamp(self._clock.now()),
            stage=stage,
            result=result,
            message=message,
        )
        self._audit.append(self._event(record))
        self._steps.append(record)

    def in_flight(self, stage: Stage) -> InFlight:
        identity = self._identity
        if identity.health_check is None:
            raise InvariantError("in-flight record requested before the run was bound")
        return InFlight(
            run_id=self.run_id,
            deployment_key=identity.deployment_key,
            from_version=identity.from_version,
            to_version=identity.to_version,
            stage=stage,
            started_at=format_timestamp(self._clock.now()),
            health_check=identity.health_check,
        )

    def finish(
        self,
        outcome: Outcome,
        rollback_status: RollbackStatus = RollbackStatus.NOT_NEEDED,
        state: EnvironmentState | None = None,
        next_action: str | None = None,
    ) -> DeploymentResult:
        identity = self._identity
        return DeploymentResult(
            run_id=self.run_id,
            outcome=outcome,
            rollback_status=rollback_status,
            deployment_key=identity.deployment_key,
            service=identity.service,
            environment=identity.environment,
            change_id=identity.change_id,
            from_version=identity.from_version,
            to_version=identity.to_version,
            final_state=(
                FinalState(status=state.status, active_version=state.active_version)
                if state is not None
                else None
            ),
            next_action=next_action,
            steps=list(self._steps),
        )

    def _event(self, record: StepRecord) -> dict[str, object]:
        identity = self._identity
        return {
            "seq": record.seq,
            "ts": record.ts,
            "run_id": self.run_id,
            "deployment_key": identity.deployment_key,
            "service": identity.service,
            "environment": identity.environment,
            "stage": record.stage,
            "result": record.result,
            "from_version": identity.from_version,
            "to_version": identity.to_version,
            "change_id": identity.change_id,
            "actor": identity.actor,
            "message": record.message,
        }
