import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from deployctl.errors import ManifestError
from deployctl.models import HealthCheckSpec, Manifest

ENVIRONMENTS = ("dev", "staging", "prod")

# Always applied with fullmatch: `$` would accept a trailing newline, and `\d` non-ASCII digits.
_SERVICE_RE = re.compile(r"[a-z][a-z0-9-]{1,40}")
_VERSION_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_CHANGE_ID_RE = re.compile(r"CHG-[0-9]{3,}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

_TOP_LEVEL_KEYS = frozenset(
    {
        "service",
        "environment",
        "version",
        "artifact",
        "change_id",
        "requested_by",
        "health_check",
    }
)
_ARTIFACT_KEYS = frozenset({"path", "sha256"})
_MAX_ATTEMPTS = 20
_NUMBER_RULES: dict[str, tuple[Callable[[float], bool], str]] = {
    "interval_seconds": (lambda value: 0 <= value <= 60, "must be between 0 and 60"),
    "timeout_seconds": (lambda value: 0 < value <= 30, "must be above 0 and at most 30"),
    "max_error_rate": (lambda value: 0 <= value <= 1, "must be between 0 and 1"),
    "max_p99_latency_ms": (lambda value: value > 0, "must be above 0"),
}


def load_manifest(path: Path, artifact_dir: Path) -> Manifest:
    """Parse and validate a manifest, raising ManifestError with every problem found."""
    data, errors = _read_object(path)
    _reject_unknown(data, _TOP_LEVEL_KEYS, "manifest", errors)
    service = _matching(data, "service", _SERVICE_RE, "2-41 of a-z, 0-9, '-'", errors)
    environment = _environment(data, errors)
    version = _matching(data, "version", _VERSION_RE, "MAJOR.MINOR.PATCH", errors)
    artifact = _artifact(data, artifact_dir, errors)
    change_id = _matching(data, "change_id", _CHANGE_ID_RE, "CHG-<3+ digits>", errors)
    requested_by = _requested_by(data, errors)
    health_check = _health_check(data, errors)

    if artifact is None or errors:
        raise ManifestError(errors)
    artifact_path, artifact_sha256 = artifact
    return Manifest(
        service=service,
        environment=environment,
        version=version,
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
        change_id=change_id,
        requested_by=requested_by,
        health_check=health_check,
    )


def _read_object(path: Path) -> tuple[dict[str, Any], list[str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError([f"manifest: cannot read {path}: {exc}"]) from exc

    duplicates: set[str] = set()

    def keep_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        counts = Counter(key for key, _ in pairs)
        duplicates.update(key for key, count in counts.items() if count > 1)
        return dict(pairs)

    try:
        data = json.loads(text, object_pairs_hook=keep_pairs)
    except (ValueError, RecursionError) as exc:
        raise ManifestError([f"manifest: invalid JSON: {exc}"]) from exc
    if not isinstance(data, dict):
        raise ManifestError(["manifest: top level must be a JSON object"])
    # json.loads keeps the last duplicate silently, so the file would not say what it appears to.
    return data, [f"manifest: duplicate key {key!r}" for key in sorted(duplicates)]


def _reject_unknown(
    section: dict[str, Any], allowed: Iterable[str], label: str, errors: list[str]
) -> None:
    errors.extend(f"{label}: unknown key {key!r}" for key in sorted(set(section) - set(allowed)))


def _string(section: dict[str, Any], key: str, label: str, errors: list[str]) -> str:
    if key not in section:
        errors.append(f"{label}: required")
        return ""
    value = section[key]
    if not isinstance(value, str):
        errors.append(f"{label}: must be a string")
        return ""
    return value


def _matching(
    section: dict[str, Any],
    key: str,
    pattern: re.Pattern[str],
    expected: str,
    errors: list[str],
    label: str | None = None,
) -> str:
    label = label or key
    before = len(errors)
    value = _string(section, key, label, errors)
    if len(errors) == before and pattern.fullmatch(value) is None:
        errors.append(f"{label}: {value!r} must be {expected}")
    return value


def _environment(data: dict[str, Any], errors: list[str]) -> str:
    before = len(errors)
    value = _string(data, "environment", "environment", errors)
    if len(errors) == before and value not in ENVIRONMENTS:
        errors.append(f"environment: {value!r} must be one of {', '.join(ENVIRONMENTS)}")
    return value


def _requested_by(data: dict[str, Any], errors: list[str]) -> str:
    before = len(errors)
    value = _string(data, "requested_by", "requested_by", errors)
    if len(errors) == before and not value.strip():
        errors.append("requested_by: must not be empty")
    return value


def _artifact(
    data: dict[str, Any], artifact_dir: Path, errors: list[str]
) -> tuple[Path, str] | None:
    if "artifact" not in data:
        errors.append("artifact: required")
        return None
    section = data["artifact"]
    if not isinstance(section, dict):
        errors.append("artifact: must be an object")
        return None
    _reject_unknown(section, _ARTIFACT_KEYS, "artifact", errors)

    before = len(errors)
    declared = _matching(
        section,
        "sha256",
        _SHA256_RE,
        "64 lowercase hex characters",
        errors,
        label="artifact.sha256",
    )
    declared_ok = len(errors) == before
    resolved = _resolve_artifact(section, artifact_dir, errors)
    if resolved is None or not declared_ok:
        return None
    try:
        with resolved.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        errors.append(f"artifact.path: cannot read artifact: {exc}")
        return None
    if actual != declared:
        errors.append(f"artifact.sha256: mismatch, declared {declared}, file has {actual}")
        return None
    return resolved, declared


def _resolve_artifact(
    section: dict[str, Any], artifact_dir: Path, errors: list[str]
) -> Path | None:
    label = "artifact.path"
    before = len(errors)
    raw = _string(section, "path", label, errors)
    if len(errors) > before:
        return None
    if not raw:
        errors.append(f"{label}: must not be empty")
        return None
    if Path(raw).is_absolute():
        errors.append(f"{label}: {raw!r} must be relative to the artifact directory")
        return None
    try:
        # resolve() follows symlinks, so a link pointing outside the directory is refused too.
        root = artifact_dir.resolve()
        resolved = (root / raw).resolve()
        if not resolved.is_relative_to(root):
            errors.append(f"{label}: {raw!r} escapes the artifact directory")
            return None
        if not resolved.is_file():
            errors.append(f"{label}: {raw!r} not found in the artifact directory")
            return None
    except (OSError, RuntimeError, ValueError) as exc:  # NUL bytes, symlink loops
        errors.append(f"{label}: {raw!r} cannot be resolved: {exc}")
        return None
    return resolved


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an integer too large for a float
        return False


def _health_check(data: dict[str, Any], errors: list[str]) -> HealthCheckSpec:
    if "health_check" not in data:
        return HealthCheckSpec()
    section = data["health_check"]
    if not isinstance(section, dict):
        errors.append("health_check: must be an object")
        return HealthCheckSpec()

    defaults = asdict(HealthCheckSpec())
    # A misspelled threshold must not silently fall back to its default.
    _reject_unknown(section, defaults, "health_check", errors)
    values = defaults | {key: value for key, value in section.items() if key in defaults}

    attempts, required = values["attempts"], values["required_consecutive"]
    attempts_valid = _is_int(attempts) and 1 <= attempts <= _MAX_ATTEMPTS
    if not _is_int(attempts):
        errors.append("health_check.attempts: must be an integer")
    elif not attempts_valid:
        errors.append(f"health_check.attempts: must be between 1 and {_MAX_ATTEMPTS}")
    if not _is_int(required):
        errors.append("health_check.required_consecutive: must be an integer")
    elif required < 1:
        errors.append("health_check.required_consecutive: must be at least 1")
    elif attempts_valid and required > attempts:
        errors.append(
            f"health_check.required_consecutive: {required} must not exceed attempts ({attempts})"
        )

    for key, (in_range, expected) in _NUMBER_RULES.items():
        value = values[key]
        if not _is_finite_number(value):
            errors.append(f"health_check.{key}: must be a finite number")
        elif not in_range(value):
            errors.append(f"health_check.{key}: {expected}")
        else:
            values[key] = float(value)
    return HealthCheckSpec(**values)
