import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from deployctl.clock import Clock, format_timestamp
from deployctl.errors import InvariantError, LockHeld, StateCorruptError
from deployctl.manifest import parse_health_check
from deployctl.models import EnvironmentState, EnvStatus, HealthCheckSpec, InFlight, Stage

SCHEMA_VERSION = 1
MAX_KNOWN_GOOD_HISTORY = 10

ALLOWED_TRANSITIONS: dict[EnvStatus, frozenset[EnvStatus]] = {
    # ROLLING_BACK: `recover` reconciling drift, with the intent saved before the target changes.
    EnvStatus.STABLE: frozenset({EnvStatus.DEPLOYING, EnvStatus.ROLLING_BACK}),
    EnvStatus.DEPLOYING: frozenset({EnvStatus.VERIFYING, EnvStatus.ROLLING_BACK}),
    EnvStatus.VERIFYING: frozenset({EnvStatus.STABLE, EnvStatus.ROLLING_BACK}),
    # ROLLING_BACK: `recover` taking over a rollback that crashed part way through.
    EnvStatus.ROLLING_BACK: frozenset(
        {EnvStatus.STABLE, EnvStatus.NEEDS_ATTENTION, EnvStatus.ROLLING_BACK}
    ),
    EnvStatus.NEEDS_ATTENTION: frozenset({EnvStatus.ROLLING_BACK}),
}


def check_transition(current: EnvStatus, new: EnvStatus) -> None:
    if new not in ALLOWED_TRANSITIONS[current]:
        raise InvariantError(f"illegal status transition {current} -> {new}")


def _is_str(value: object) -> bool:
    return isinstance(value, str)


def _is_optional_str(value: object) -> bool:
    return value is None or isinstance(value, str)


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_member_of(enum: type[StrEnum]) -> Callable[[object], bool]:
    members = frozenset(enum)
    return lambda value: isinstance(value, str) and value in members


_ENTRY_RULES: dict[str, Callable[[object], bool]] = {
    "status": _is_member_of(EnvStatus),
    "active_version": _is_optional_str,
    "active_artifact_sha256": _is_optional_str,
    "last_known_good": _is_optional_str,
    "known_good_history": _is_str_list,
    "failed_versions": _is_str_list,
    "in_flight": lambda value: value is None or isinstance(value, dict),
    "updated_at": _is_optional_str,
    "last_run_id": _is_optional_str,
}
_IN_FLIGHT_RULES: dict[str, Callable[[object], bool]] = {
    "run_id": _is_str,
    "deployment_key": _is_str,
    "from_version": _is_optional_str,
    "to_version": _is_str,
    "stage": _is_member_of(Stage),
    "started_at": _is_str,
    "health_check": lambda value: isinstance(value, dict),
}


def _check_shape(
    value: object, rules: Mapping[str, Callable[[object], bool]], label: str
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    # Unknown keys count as corrupt too: saving would silently drop what we do not understand.
    if set(value) != set(rules):
        raise ValueError(f"{label} has keys {sorted(value)}, expected {sorted(rules)}")
    invalid = sorted(key for key, is_valid in rules.items() if not is_valid(value[key]))
    if invalid:
        raise ValueError(f"{label} has invalid {', '.join(invalid)}")
    return value


def _decode_health_check(section: dict[str, Any], label: str) -> HealthCheckSpec:
    # Unlike a manifest, the ledger always writes every key, so a missing one is not a default.
    missing = sorted(set(asdict(HealthCheckSpec())) - set(section))
    errors = [f"health_check.{key}: required" for key in missing]
    spec = parse_health_check(section, errors)
    if errors:
        raise ValueError(f"{label}: {'; '.join(errors)}")
    return spec


def _decode_in_flight(value: object, label: str) -> InFlight:
    raw = _check_shape(value, _IN_FLIGHT_RULES, label)
    decoded = {
        "stage": Stage(raw["stage"]),
        "health_check": _decode_health_check(raw["health_check"], label),
    }
    return InFlight(**(raw | decoded))


def _decode_entry(env_key: str, value: object) -> EnvironmentState:
    service, _, environment = env_key.partition("/")
    if not service or not environment:
        raise ValueError(f"{env_key!r} is not a service/environment key")
    entry = _check_shape(value, _ENTRY_RULES, env_key)
    in_flight = entry["in_flight"]
    if in_flight is not None:
        in_flight = _decode_in_flight(in_flight, f"{env_key}.in_flight")
    return EnvironmentState(
        service=service,
        environment=environment,
        **(entry | {"status": EnvStatus(entry["status"]), "in_flight": in_flight}),
    )


def _decode(text: str) -> dict[str, EnvironmentState]:
    document = json.loads(text)
    if not isinstance(document, dict):
        raise ValueError("top level must be an object")
    version = document.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ValueError(f"unknown schema_version {version!r}")
    environments = document.get("environments")
    if not isinstance(environments, dict):
        raise ValueError("environments must be an object")
    return {key: _decode_entry(key, value) for key, value in environments.items()}


def _encode_entry(state: EnvironmentState) -> dict[str, Any]:
    entry = asdict(state)
    del entry["service"], entry["environment"]  # already spelled out by the entry's key
    entry["known_good_history"] = entry["known_good_history"][-MAX_KNOWN_GOOD_HISTORY:]
    return entry


def _encode(environments: dict[str, EnvironmentState]) -> str:
    document = {
        "schema_version": SCHEMA_VERSION,
        "environments": {key: _encode_entry(state) for key, state in environments.items()},
    }
    return json.dumps(document, indent=2) + "\n"


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    # The rename is only durable once the directory is: write-ahead intent must survive power loss.
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@dataclass(frozen=True)
class LockInfo:
    owner: str
    since: str | None = None


_UNKNOWN_HOLDER = LockInfo(owner="unknown")


def _read_lock(path: Path) -> LockInfo | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):  # half-written or tampered with: held, by whom we cannot say
        return _UNKNOWN_HOLDER
    if not isinstance(data, dict) or not isinstance(data.get("owner"), str):
        return _UNKNOWN_HOLDER
    since = data.get("since")
    return LockInfo(owner=data["owner"], since=since if isinstance(since, str) else None)


class FileLock:
    """A held lockfile, created with O_EXCL so that exactly one run can win it."""

    def __init__(self, path: Path, owner: str) -> None:
        self._path = path
        self._owner = owner

    @classmethod
    def acquire(cls, path: Path, owner: str, since: str) -> "FileLock":
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            holder = _read_lock(path) or _UNKNOWN_HOLDER
            raise LockHeld(holder.owner, holder.since) from None
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"owner": owner, "pid": os.getpid(), "since": since}, stream)
        return cls(path, owner)

    def release(self) -> None:
        # After a `--break-lock` the file may belong to another run, which must keep it.
        holder = _read_lock(self._path)
        if holder is not None and holder.owner == self._owner:
            self._path.unlink(missing_ok=True)

    def __enter__(self) -> "FileLock":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


class StateStore:
    """The controller's ledger: what it believes and intends, one entry per service/environment."""

    def __init__(self, state_dir: Path, clock: Clock) -> None:
        self.path = state_dir / "state.json"
        self.lock_path = state_dir / "deployctl.lock"
        self._clock = clock

    def load(self, service: str, environment: str) -> EnvironmentState:
        blank = EnvironmentState(service=service, environment=environment)
        return self._read().get(blank.env_key, blank)

    def save(self, state: EnvironmentState) -> None:
        # Reading first also means a corrupt file raises here instead of being overwritten.
        environments = self._read()
        environments[state.env_key] = state
        _write_atomic(self.path, _encode(environments))

    def lock(self, owner: str) -> FileLock:
        return FileLock.acquire(self.lock_path, owner, format_timestamp(self._clock.now()))

    def break_lock(self) -> LockInfo | None:
        """Remove the lockfile and report who held it, for the audit trail."""
        holder = _read_lock(self.lock_path)
        self.lock_path.unlink(missing_ok=True)
        return holder

    def _read(self) -> dict[str, EnvironmentState]:
        try:
            return _decode(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, RecursionError) as exc:
            raise StateCorruptError(f"{self.path}: {exc}") from exc
