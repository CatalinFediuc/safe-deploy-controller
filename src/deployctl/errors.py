class DeployctlError(Exception):
    """Base class for every error this package raises on purpose."""


class ManifestError(DeployctlError):
    """The manifest was refused; `errors` lists every problem found, not just the first."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class LockHeld(DeployctlError):
    """Another run owns the lock; it is reported, never stolen."""

    def __init__(self, owner: str, since: str | None = None) -> None:
        super().__init__(f"lock held by {owner} since {since}")
        self.owner = owner
        self.since = since


class StateCorruptError(DeployctlError):
    """The ledger is unreadable or has an unknown schema; it must not be overwritten."""


class InvariantError(DeployctlError):
    """An illegal state transition was attempted: a bug, not an operational failure."""


class ProbeError(DeployctlError):
    """A health probe failed; the caller counts it as an unhealthy sample."""


class ProbeTimeout(ProbeError):
    """A health probe did not answer in time."""
