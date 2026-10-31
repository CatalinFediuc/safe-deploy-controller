from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from deployctl.audit import AuditLog, NullAudit, Recorder
from deployctl.clock import Clock, format_timestamp
from deployctl.errors import LockHeld, ManifestError, RecoveryRequired, StateCorruptError
from deployctl.health import HealthProbe, verify
from deployctl.manifest import load_manifest
from deployctl.models import (
    DeploymentResult,
    EnvironmentState,
    EnvStatus,
    HealthCheckSpec,
    Manifest,
    Outcome,
    RollbackStatus,
    Stage,
    StepResult,
)
from deployctl.state import StateStore, check_transition
from deployctl.target import DeploymentTarget

_RUN_RECOVER = "run `deployctl recover`"
_INTERRUPTED = "interrupted; taking no further action"
_SAFE_TO_RERUN = "nothing changed; safe to run again"


@dataclass(frozen=True)
class DeployOptions:
    dry_run: bool = False
    allow_known_bad: bool = False
    allow_downgrade: bool = False


@dataclass(frozen=True)
class _Gate:
    outcome: Outcome
    result: StepResult
    reason: str
    next_action: str | None = None


def _blocked(reason: str, next_action: str) -> _Gate:
    return _Gate(Outcome.BLOCKED, StepResult.FAIL, reason, next_action)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _is_downgrade(version: str, active: str | None) -> bool:
    return active is not None and _version_key(version) < _version_key(active)


def _flags_used(opts: DeployOptions) -> str:
    flags = (
        ("--dry-run", opts.dry_run),
        ("--allow-known-bad", opts.allow_known_bad),
        ("--allow-downgrade", opts.allow_downgrade),
    )
    return "".join(f"; {flag}" for flag, used in flags if used)


def _load(rec: Recorder, manifest_path: Path, artifact_dir: Path) -> Manifest | DeploymentResult:
    """The valid manifest, or the finished result of a run that went no further."""
    try:
        manifest = load_manifest(manifest_path, artifact_dir)
    except ManifestError as err:
        rec.bind_environment(err.service, err.environment)
        rec.step(Stage.VALIDATE, StepResult.FAIL, str(err))
        return rec.finish(Outcome.REJECTED, next_action="fix the manifest; nothing changed")
    except KeyboardInterrupt:
        rec.step(Stage.VALIDATE, StepResult.FAIL, _INTERRUPTED)
        return rec.finish(Outcome.INTERRUPTED, next_action=_SAFE_TO_RERUN)
    rec.bind(manifest)
    rec.step(Stage.VALIDATE, StepResult.PASS, "manifest valid; sha256 verified")
    return manifest


def validate(
    manifest_path: Path, artifact_dir: Path, clock: Clock, run_id: str
) -> DeploymentResult:
    """Check a manifest with no state and no audit trail, so it can run in CI on every PR."""
    rec = Recorder(NullAudit(), clock, run_id)
    loaded = _load(rec, manifest_path, artifact_dir)
    return loaded if isinstance(loaded, DeploymentResult) else rec.finish(Outcome.VALID)


class DeploymentController:
    """Drives a release through its stages, and owns every change of environment status."""

    def __init__(
        self,
        store: StateStore,
        target: DeploymentTarget,
        probe: HealthProbe,
        audit: AuditLog,
        clock: Clock,
        run_id_factory: Callable[[], str],
    ) -> None:
        self._store = store
        self._target = target
        self._probe = probe
        self._audit = audit
        self._clock = clock
        self._run_id_factory = run_id_factory

    def deploy(
        self, manifest_path: Path, artifact_dir: Path, opts: DeployOptions
    ) -> DeploymentResult:
        rec = self._recorder()
        # Before the lock and before state is read: a bad manifest must change nothing.
        manifest = _load(rec, manifest_path, artifact_dir)
        if isinstance(manifest, DeploymentResult):
            return manifest
        return self._locked(
            rec,
            Stage.PREFLIGHT,
            manifest.service,
            manifest.environment,
            lambda: self._deploy(manifest, opts, rec),
        )

    def recover(
        self,
        service: str,
        environment: str,
        reason: str,
        actor: str,
        *,
        break_lock: bool = False,
    ) -> DeploymentResult:
        rec = self._recorder()
        rec.bind_environment(service, environment, actor)
        if break_lock:
            holder = self._store.break_lock()
            rec.step(
                Stage.RECOVER,
                StepResult.PASS,
                f"lock broken, held by {holder.owner} since {holder.since}"
                if holder is not None
                else "no lock to break",
            )
        return self._locked(
            rec,
            Stage.RECOVER,
            service,
            environment,
            lambda: self._recover(service, environment, reason, rec),
        )

    def _recorder(self) -> Recorder:
        return Recorder(self._audit, self._clock, run_id=self._run_id_factory())

    def _locked(
        self,
        rec: Recorder,
        stage: Stage,
        service: str,
        environment: str,
        run: Callable[[], DeploymentResult],
    ) -> DeploymentResult:
        try:
            with self._store.lock(owner=rec.run_id):
                return run()
        except LockHeld as held:
            rec.step(stage, StepResult.FAIL, f"lock held by {held.owner} since {held.since}")
            return rec.finish(
                Outcome.BLOCKED,
                next_action="wait for that run; if it died, use `deployctl recover --break-lock`",
            )
        except StateCorruptError as exc:
            rec.step(stage, StepResult.FAIL, f"refused: {exc}")
            raise
        except KeyboardInterrupt:
            # The operator said stop, so nothing else is attempted. Write-ahead state says the rest.
            state = self._store.load(service, environment)
            in_flight = state.in_flight
            rec.step(
                in_flight.stage if in_flight else stage,
                StepResult.FAIL,
                _INTERRUPTED,
            )
            return rec.finish(
                Outcome.INTERRUPTED,
                state=state,
                next_action=_RUN_RECOVER if in_flight else _SAFE_TO_RERUN,
            )

    def _deploy(self, manifest: Manifest, opts: DeployOptions, rec: Recorder) -> DeploymentResult:
        state = self._store.load(manifest.service, manifest.environment)
        rec.bind_from_version(state.active_version)
        gate = self._preflight(state, manifest, opts)
        if gate is not None:
            rec.step(Stage.PREFLIGHT, gate.result, gate.reason)
            return rec.finish(gate.outcome, state=state, next_action=gate.next_action)
        rec.step(
            Stage.PREFLIGHT,
            StepResult.PASS,
            f"{state.status} @ {state.active_version}; lock acquired{_flags_used(opts)}",
        )
        if opts.dry_run:
            return rec.finish(Outcome.NO_OP, state=state, next_action="dry-run: no changes made")

        # Write-ahead: the intent is on disk before the target is touched.
        state.in_flight = rec.in_flight(Stage.DEPLOY)
        self._transition(state, EnvStatus.DEPLOYING, rec)
        try:
            return self._roll_out(state, manifest, rec)
        except Exception as exc:
            raise self._stopped(state, rec, exc) from exc

    def _preflight(
        self, state: EnvironmentState, manifest: Manifest, opts: DeployOptions
    ) -> _Gate | None:
        version = manifest.version
        if state.status is not EnvStatus.STABLE:
            in_flight = state.in_flight
            detail = f": run {in_flight.run_id} at stage {in_flight.stage}" if in_flight else ""
            return _blocked(f"{state.env_key} is {state.status}{detail}", _RUN_RECOVER)
        running = self._target.running_version(state.env_key)
        if running != state.active_version:
            return _blocked(
                f"drift: recorded {state.active_version}, running {running}", _RUN_RECOVER
            )
        if state.active_version == version:
            if state.active_artifact_sha256 != manifest.artifact_sha256:
                return _blocked(
                    f"{version} already active with a different artifact",
                    "release the rebuilt artifact as a new version",
                )
            return _Gate(Outcome.NO_OP, StepResult.SKIP, f"{version} already active")
        if version in state.failed_versions and not opts.allow_known_bad:
            return _blocked(
                f"{version} previously failed verification here",
                "investigate, then retry with --allow-known-bad",
            )
        if _is_downgrade(version, state.active_version) and not opts.allow_downgrade:
            return _blocked(
                f"{version} is older than the active {state.active_version}",
                "use --allow-downgrade if this is intended",
            )
        return None

    def _roll_out(
        self, state: EnvironmentState, manifest: Manifest, rec: Recorder
    ) -> DeploymentResult:
        version = manifest.version
        try:
            self._target.apply(state.env_key, version)
        except Exception as exc:  # the environment may be half-changed
            rec.step(Stage.DEPLOY, StepResult.FAIL, f"apply failed: {exc}")
            return self._rollback(state, rec, manifest, mark_failed=False)
        rec.step(Stage.DEPLOY, StepResult.PASS, f"running version -> {version}")

        try:
            self._transition(state, EnvStatus.VERIFYING, rec, Stage.VERIFY)
            verdict = verify(
                self._probe,
                self._target,
                self._clock,
                state.env_key,
                version,
                manifest.health_check,
            )
        except Exception as exc:
            rec.step(Stage.VERIFY, StepResult.FAIL, f"verification failed: {exc}")
            return self._rollback(state, rec, manifest, mark_failed=False)
        if not verdict.passed:
            rec.step(Stage.VERIFY, StepResult.FAIL, verdict.summary)
            return self._rollback(state, rec, manifest, mark_failed=verdict.health_measured)
        rec.step(Stage.VERIFY, StepResult.PASS, verdict.summary)

        self._complete(state, manifest, rec)
        rec.step(Stage.COMPLETE, StepResult.PASS, f"{version} is now known-good")
        return rec.finish(Outcome.SUCCEEDED, state=state)

    def _complete(self, state: EnvironmentState, manifest: Manifest, rec: Recorder) -> None:
        version = manifest.version
        state.active_version = version
        state.active_artifact_sha256 = manifest.artifact_sha256
        state.last_known_good = version
        state.known_good_history.append(version)
        # Reached only through --allow-known-bad: it has now proved itself here.
        state.failed_versions = [known for known in state.failed_versions if known != version]
        self._transition(state, EnvStatus.STABLE, rec)

    def _rollback(
        self, state: EnvironmentState, rec: Recorder, manifest: Manifest, *, mark_failed: bool
    ) -> DeploymentResult:
        version = manifest.version
        if mark_failed and version not in state.failed_versions:
            state.failed_versions.append(version)
        self._transition(state, EnvStatus.ROLLING_BACK, rec, Stage.ROLLBACK)
        status = self._restore(state, rec, manifest.health_check)
        if status is RollbackStatus.FAILED:
            return self._gave_up(state, rec)
        next_action = (
            f"{version} is marked known-bad for {state.env_key}; "
            "investigate before retrying with --allow-known-bad"
            if version in state.failed_versions
            else f"{version} was not marked known-bad; fix the cause and retry"
        )
        return rec.finish(Outcome.ROLLED_BACK, status, state, next_action)

    def _recover(
        self, service: str, environment: str, reason: str, rec: Recorder
    ) -> DeploymentResult:
        state = self._store.find(service, environment)
        if state is None:
            # An empty ledger says nothing about the target: what runs there would read as drift.
            message = f"no record of {service}/{environment}; reason: {reason}"
            rec.step(Stage.RECOVER, StepResult.FAIL, message)
            return rec.finish(
                Outcome.BLOCKED,
                next_action="check the service, environment and --state-dir; nothing changed",
            )
        running = self._target.running_version(state.env_key)
        good = state.last_known_good
        previous = state.in_flight
        # Drift has no in-flight record, so it has no failed deploy's thresholds to reuse.
        spec = previous.health_check if previous else HealthCheckSpec()
        rec.bind_recovery(previous.deployment_key if previous else None, running, good, spec)

        if state.status is EnvStatus.STABLE and running == state.active_version:
            message = f"nothing to recover: STABLE @ {running}; reason: {reason}"
            rec.step(Stage.RECOVER, StepResult.SKIP, message)
            return rec.finish(Outcome.NO_OP, state=state)
        rec.step(
            Stage.RECOVER,
            StepResult.PASS,
            f"{state.status}, running {running}, restoring {good or 'nothing'}; reason: {reason}",
        )

        state.in_flight = rec.in_flight(Stage.ROLLBACK)
        try:
            self._transition(state, EnvStatus.ROLLING_BACK, rec)
            status = self._restore(state, rec, spec)
            if status is RollbackStatus.FAILED:
                return self._gave_up(state, rec)
        except Exception as exc:
            raise self._stopped(state, rec, exc) from exc
        return rec.finish(Outcome.RECOVERED, status, state)

    def _restore(
        self, state: EnvironmentState, rec: Recorder, spec: HealthCheckSpec
    ) -> RollbackStatus:
        good = state.last_known_good
        try:
            if good is None:
                self._target.remove(state.env_key)
            else:
                self._target.apply(state.env_key, good)
        except Exception as exc:
            rec.step(Stage.ROLLBACK, StepResult.FAIL, f"rollback failed: {exc}")
            return RollbackStatus.FAILED

        if good is None:
            rec.step(
                Stage.ROLLBACK, StepResult.PASS, "no previous version; removed what was running"
            )
            self._transition(state, EnvStatus.STABLE, rec)
            return RollbackStatus.NO_PREVIOUS_VERSION

        rec.step(Stage.ROLLBACK, StepResult.PASS, f"running version -> {good}")
        self._transition(state, EnvStatus.ROLLING_BACK, rec, Stage.VERIFY_ROLLBACK)
        try:
            verdict = verify(self._probe, self._target, self._clock, state.env_key, good, spec)
        except Exception as exc:  # a rollback nobody could confirm has failed: page a human
            rec.step(Stage.VERIFY_ROLLBACK, StepResult.FAIL, f"verification failed: {exc}")
            return RollbackStatus.FAILED
        result = StepResult.PASS if verdict.passed else StepResult.FAIL
        rec.step(Stage.VERIFY_ROLLBACK, result, verdict.summary)
        if not verdict.passed:
            return RollbackStatus.FAILED
        self._transition(state, EnvStatus.STABLE, rec)
        return RollbackStatus.SUCCEEDED

    def _gave_up(self, state: EnvironmentState, rec: Recorder) -> DeploymentResult:
        # in_flight stays: `recover` has no other source for the thresholds.
        self._transition(state, EnvStatus.NEEDS_ATTENTION, rec)
        return rec.finish(
            Outcome.ROLLBACK_FAILED,
            RollbackStatus.FAILED,
            state,
            f"page a human; once the cause is fixed, {_RUN_RECOVER}",
        )

    def _stopped(self, state: EnvironmentState, rec: Recorder, exc: Exception) -> RecoveryRequired:
        stage = state.in_flight.stage if state.in_flight else Stage.RECOVER
        with suppress(OSError):  # the audit log may be what failed; keep the instruction anyway
            rec.step(stage, StepResult.FAIL, f"run stopped: {exc}")
        return RecoveryRequired(f"{state.env_key}: {exc}; {_RUN_RECOVER}")

    def _transition(
        self,
        state: EnvironmentState,
        status: EnvStatus,
        rec: Recorder,
        stage: Stage | None = None,
    ) -> None:
        check_transition(state.status, status)
        state.status = status
        if status is EnvStatus.STABLE:  # in the same write: stable never has a run in flight
            state.in_flight = None
        elif state.in_flight is not None and stage is not None:
            state.in_flight.stage = stage
        state.updated_at = format_timestamp(self._clock.now())
        state.last_run_id = rec.run_id
        self._store.save(state)
