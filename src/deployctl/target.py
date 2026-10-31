import json
from pathlib import Path
from typing import Protocol

from deployctl.clock import Clock, format_timestamp
from deployctl.errors import StateCorruptError
from deployctl.fileio import write_atomic

_ENTRY_KEYS = frozenset({"running_version", "updated_at"})


class DeploymentTarget(Protocol):
    """Real implementations: Kubernetes/Argo CD/ECS. Simulated: environment.json."""

    def running_version(self, env_key: str) -> str | None: ...
    def apply(self, env_key: str, version: str) -> None: ...
    def remove(self, env_key: str) -> None: ...


def _decode(text: str) -> dict[str, dict[str, str]]:
    document = json.loads(text)
    if not isinstance(document, dict):
        raise ValueError("top level must be an object")
    for env_key, entry in document.items():
        # Unknown keys count as corrupt too: saving would silently drop what we do not understand.
        if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
            raise ValueError(f"{env_key} must have exactly {sorted(_ENTRY_KEYS)}")
        if not all(isinstance(value, str) for value in entry.values()):
            raise ValueError(f"{env_key} must hold strings")
    return document


class SimulatedTarget:
    """What is actually running, one entry per service/environment. Nothing running: no entry."""

    def __init__(self, state_dir: Path, clock: Clock) -> None:
        self.path = state_dir / "environment.json"
        self._clock = clock

    def running_version(self, env_key: str) -> str | None:
        entry = self._read().get(env_key)
        return None if entry is None else entry["running_version"]

    def apply(self, env_key: str, version: str) -> None:
        environments = self._read()
        environments[env_key] = {
            "running_version": version,
            "updated_at": format_timestamp(self._clock.now()),
        }
        self._write(environments)

    def remove(self, env_key: str) -> None:
        environments = self._read()
        if environments.pop(env_key, None) is not None:
            self._write(environments)

    def _read(self) -> dict[str, dict[str, str]]:
        try:
            return _decode(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, RecursionError) as exc:
            raise StateCorruptError(f"{self.path}: {exc}") from exc

    def _write(self, environments: dict[str, dict[str, str]]) -> None:
        write_atomic(self.path, json.dumps(environments, indent=2) + "\n")
