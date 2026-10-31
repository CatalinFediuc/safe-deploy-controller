import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from deployctl.clock import Clock, format_timestamp
from deployctl.errors import InvariantError
from deployctl.models import (
    DeploymentResult,
    EnvironmentState,
    ExitCode,
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


class AuditSink(Protocol):
    def append(self, event: dict[str, object]) -> None: ...


class NullAudit:
    """For `validate`, which runs in CI with no state and writes nothing."""

    def append(self, event: dict[str, object]) -> None:
        pass


@dataclass(frozen=True)
class AuditHistory:
    runs: list[dict[str, Any]]  # newest first
    skipped_lines: int  # across the whole file: a torn line can't say whose it was


def _decode_event(line: str) -> dict[str, Any] | None:
    try:
        event = json.loads(line)
    except ValueError:
        return None
    # A fragment of a torn line can still parse, as a number for example.
    if not isinstance(event, dict) or not isinstance(event.get("run_id"), str):
        return None
    return event


def _summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    last = events[-1]  # `recover` only learns its versions once it has read the ledger
    return {
        "run_id": last["run_id"],
        "started_at": events[0].get("ts"),
        "from_version": last.get("from_version"),
        "to_version": last.get("to_version"),
        "change_id": last.get("change_id"),
        "actor": last.get("actor"),
        # None when the run left no `finish` event: it died, or stopped with exit 1.
        "outcome": last.get("outcome"),
        "rollback_status": last.get("rollback_status"),
        "exit_code": last.get("exit_code"),
        "events": events,
    }


class AuditLog:
    """Append-only JSONL trail; each event is on disk before the run moves on."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "audit.jsonl"

    def append(self, event: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as stream:
            # A hard kill can leave a torn line, which would swallow this event if joined to it.
            if stream.seek(0, os.SEEK_END) > 0:
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    stream.write(b"\n")
            stream.write(json.dumps(event).encode() + b"\n")
            stream.flush()
            os.fsync(stream.fileno())

    def history(self, service: str, environment: str, limit: int) -> AuditHistory:
        """The latest `limit` runs of an environment, with a count of unreadable lines."""
        runs: dict[str, list[dict[str, Any]]] = {}
        skipped = 0
        try:
            with self.path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    event = _decode_event(line)
                    if event is None:
                        skipped += 1
                    else:
                        runs.setdefault(event["run_id"], []).append(event)
        except FileNotFoundError:
            return AuditHistory(runs=[], skipped_lines=0)
        matching = [
            _summarize(events)
            for events in runs.values()
            if (events[-1].get("service"), events[-1].get("environment")) == (service, environment)
        ]
        return AuditHistory(runs=matching[-limit:][::-1], skipped_lines=skipped)


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

    def __init__(self, audit: AuditSink, clock: Clock, run_id: str) -> None:
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
        result = DeploymentResult(
            run_id=self.run_id,
            outcome=outcome,
            rollback_status=rollback_status,
            deployment_key=identity.deployment_key,
            service=identity.service,
            environment=identity.environment,
            change_id=identity.change_id,
            actor=identity.actor,
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
        self._audit.append(self._finish_event(result))
        return result

    def _finish_event(self, result: DeploymentResult) -> dict[str, object]:
        record = StepRecord(
            seq=len(self._steps) + 1,
            ts=format_timestamp(self._clock.now()),
            stage=Stage.FINISH,
            result=StepResult.PASS if result.exit_code is ExitCode.OK else StepResult.FAIL,
            message=result.next_action or result.outcome,
        )
        return self._event(record) | {
            "outcome": result.outcome,
            "rollback_status": result.rollback_status,
            "exit_code": int(result.exit_code),
        }

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
