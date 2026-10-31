class DeployctlError(Exception):
    """Base class for every error this package raises on purpose."""


class ManifestError(DeployctlError):
    """The manifest was refused; `errors` lists every problem found, not just the first."""

    def __init__(
        self, errors: list[str], service: str | None = None, environment: str | None = None
    ) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors
        # As declared, valid or not: who tried what is worth auditing even when it was refused.
        self.service = service
        self.environment = environment


class LockHeld(DeployctlError):
    """Another run owns the lock; it is reported, never stolen."""

    def __init__(self, owner: str, since: str | None = None) -> None:
        super().__init__(f"lock held by {owner} since {since}")
        self.owner = owner
        self.since = since


class StateCorruptError(DeployctlError):
    """The ledger or target file is unreadable or malformed; it must not be overwritten."""


class RecoveryRequired(DeployctlError):
    """A run stopped with its in-flight state saved; `recover` has to finish the job."""


class HealthProfileError(DeployctlError):
    """The health profile is unusable; refused up front, not mistaken for an unhealthy release."""


class InvariantError(DeployctlError):
    """An illegal state transition was attempted: a bug, not an operational failure."""


class ProbeError(DeployctlError):
    """A health probe failed; the caller counts it as an unhealthy sample."""


class ProbeTimeout(ProbeError):
    """A health probe did not answer in time."""
