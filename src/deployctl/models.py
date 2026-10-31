import hashlib
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path


class EnvStatus(StrEnum):
    STABLE = "STABLE"  # running == active == known-good (or nothing deployed)
    DEPLOYING = "DEPLOYING"  # intent persisted; target may be changing
    VERIFYING = "VERIFYING"  # new version running, not yet trusted
    ROLLING_BACK = "ROLLING_BACK"  # restoring last known-good
    NEEDS_ATTENTION = "NEEDS_ATTENTION"  # automation stopped; human must run `recover`


class Stage(StrEnum):
    VALIDATE = "validate"
    PREFLIGHT = "preflight"
    DEPLOY = "deploy"
    VERIFY = "verify"
    COMPLETE = "complete"
    ROLLBACK = "rollback"
    VERIFY_ROLLBACK = "verify_rollback"
    RECOVER = "recover"
    FINISH = "finish"  # audit only: closes a run, and is not one of the result's steps


class StepResult(StrEnum):
    PASS = "PASS"  # noqa: S105 - a step result, not a credential
    FAIL = "FAIL"
    SKIP = "SKIP"


class Outcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    NO_OP = "NO_OP"
    RECOVERED = "RECOVERED"
    VALID = "VALID"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    INTERRUPTED = "INTERRUPTED"


class RollbackStatus(StrEnum):
    NOT_NEEDED = "NOT_NEEDED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    NO_PREVIOUS_VERSION = "NO_PREVIOUS_VERSION"


class ExitCode(IntEnum):
    OK = 0
    INTERNAL_ERROR = 1
    USAGE = 2
    REJECTED = 3
    ROLLED_BACK = 4
    NEEDS_ATTENTION = 5
    BLOCKED = 6
    INTERRUPTED = 130


EXIT_FOR_OUTCOME: dict[Outcome, ExitCode] = {
    Outcome.SUCCEEDED: ExitCode.OK,
    Outcome.NO_OP: ExitCode.OK,
    Outcome.RECOVERED: ExitCode.OK,
    Outcome.VALID: ExitCode.OK,
    Outcome.REJECTED: ExitCode.REJECTED,
    Outcome.BLOCKED: ExitCode.BLOCKED,
    Outcome.ROLLED_BACK: ExitCode.ROLLED_BACK,
    Outcome.ROLLBACK_FAILED: ExitCode.NEEDS_ATTENTION,
    Outcome.INTERRUPTED: ExitCode.INTERRUPTED,
}


@dataclass(frozen=True)
class HealthCheckSpec:
    attempts: int = 5
    required_consecutive: int = 3
    interval_seconds: float = 1.0
    timeout_seconds: float = 2.0
    max_error_rate: float = 0.01
    max_p99_latency_ms: float = 500.0
    rollout_timeout_seconds: float = 60.0


@dataclass(frozen=True)
class Manifest:
    service: str
    environment: str
    version: str
    artifact_path: Path
    artifact_sha256: str
    change_id: str
    requested_by: str
    health_check: HealthCheckSpec = field(default_factory=HealthCheckSpec)

    @property
    def env_key(self) -> str:
        return f"{self.service}/{self.environment}"

    @property
    def deployment_key(self) -> str:
        """Idempotency key: identical inputs always produce the same key."""
        raw = "|".join(
            (self.service, self.environment, self.version, self.artifact_sha256, self.change_id)
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:12]


@dataclass
class InFlight:
    run_id: str
    deployment_key: str | None  # None: a `recover` reconciling drift is not a deployment
    from_version: str | None
    to_version: str | None  # None: a `recover` with no known-good version removes what is running
    stage: Stage
    started_at: str
    health_check: HealthCheckSpec  # `recover` has no manifest to take its thresholds from


@dataclass
class EnvironmentState:
    service: str
    environment: str
    status: EnvStatus = EnvStatus.STABLE
    active_version: str | None = None
    active_artifact_sha256: str | None = None  # a version number alone can hide a rebuild
    last_known_good: str | None = None
    known_good_history: list[str] = field(default_factory=list)
    failed_versions: list[str] = field(default_factory=list)
    in_flight: InFlight | None = None
    updated_at: str | None = None
    last_run_id: str | None = None

    @property
    def env_key(self) -> str:
        return f"{self.service}/{self.environment}"


@dataclass(frozen=True)
class StepRecord:
    seq: int
    ts: str
    stage: Stage
    result: StepResult
    message: str


@dataclass(frozen=True)
class FinalState:
    status: EnvStatus
    active_version: str | None


@dataclass
class DeploymentResult:
    run_id: str
    outcome: Outcome
    rollback_status: RollbackStatus = RollbackStatus.NOT_NEEDED
    # Identity fields stay None when the manifest was rejected before it could be parsed.
    deployment_key: str | None = None
    service: str | None = None
    environment: str | None = None
    change_id: str | None = None
    actor: str | None = None
    from_version: str | None = None
    to_version: str | None = None
    final_state: FinalState | None = None
    next_action: str | None = None
    steps: list[StepRecord] = field(default_factory=list)

    @property
    def exit_code(self) -> ExitCode:
        return EXIT_FOR_OUTCOME[self.outcome]
