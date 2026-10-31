import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from deployctl.clock import Clock
from deployctl.errors import HealthProfileError, ProbeError, ProbeTimeout
from deployctl.manifest import is_finite_number
from deployctl.models import HealthCheckSpec
from deployctl.target import DeploymentTarget

_METRICS = frozenset({"error_rate", "p99_latency_ms"})


@dataclass(frozen=True)
class HealthSample:
    error_rate: float | None
    p99_latency_ms: float | None


class HealthProbe(Protocol):
    """Real implementations: Prometheus/Datadog query, synthetic check. Simulated: JSON fixture.

    Raises ProbeTimeout / ProbeError on failure; the caller treats both as unhealthy.
    """

    def sample(self, env_key: str, version: str, timeout_seconds: float) -> HealthSample: ...


@dataclass(frozen=True)
class Verdict:
    passed: bool
    probes: int
    consecutive: int
    summary: str  # e.g. "0/3 consecutive healthy after 3 probes (error_rate 0.120 > 0.010)"


_Response = HealthSample | Literal["timeout", "error"]


def _decode_response(value: object, label: str) -> _Response:
    if value == "timeout":
        return "timeout"
    if value == "error":
        return "error"
    if not isinstance(value, dict):
        raise ValueError(f'{label} must be an object, "timeout" or "error"')
    unknown = set(value) - _METRICS
    if unknown:
        raise ValueError(f"{label} has unknown keys {sorted(unknown)}")
    for key, metric in value.items():
        if metric is not None and not (is_finite_number(metric) and metric >= 0):
            raise ValueError(f"{label}.{key} must be a finite number, at least 0, or null")
    return HealthSample(value.get("error_rate"), value.get("p99_latency_ms"))


def _decode_profile(text: str) -> dict[str, dict[str, list[_Response]]]:
    document = json.loads(text)
    if not isinstance(document, dict):
        raise ValueError("top level must be an object")
    profile: dict[str, dict[str, list[_Response]]] = {}
    for service, versions in document.items():
        if not isinstance(versions, dict):
            raise ValueError(f"{service} must be an object of versions")
        profile[service] = {}
        for version, responses in versions.items():
            label = f"{service} {version}"
            if not isinstance(responses, list) or not responses:
                raise ValueError(f"{label} must be a non-empty list")
            profile[service][version] = [
                _decode_response(response, f"{label}[{index}]")
                for index, response in enumerate(responses)
            ]
    return profile


class FixtureHealthProbe:
    """Replays a profile per service and version; the last response of a sequence repeats."""

    def __init__(self, path: Path) -> None:
        try:
            self._profile = _decode_profile(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            raise HealthProfileError(f"{path}: {exc}") from exc
        self._calls: Counter[tuple[str, str]] = Counter()

    def sample(self, env_key: str, version: str, timeout_seconds: float) -> HealthSample:
        service, _, _ = env_key.partition("/")
        responses = self._profile.get(service, {}).get(version)
        if responses is None:
            return HealthSample(None, None)  # no data is unhealthy, never assumed healthy
        position = min(self._calls[env_key, version], len(responses) - 1)
        self._calls[env_key, version] += 1
        response = responses[position]
        if response == "timeout":
            raise ProbeTimeout(f"probe timed out after {timeout_seconds:g}s")
        if response == "error":
            raise ProbeError("probe returned an error")
        return response


def _problem(sample: HealthSample, spec: HealthCheckSpec) -> str | None:
    error_rate, p99 = sample.error_rate, sample.p99_latency_ms
    if error_rate is None and p99 is None:
        return "no data"
    # `not value <= limit` rather than `value > limit`, so that NaN fails too.
    if error_rate is None:
        return "error_rate missing"
    if not error_rate <= spec.max_error_rate:
        return f"error_rate {error_rate:.3f} > {spec.max_error_rate:.3f}"
    if p99 is None:
        return "p99_latency_ms missing"
    if not p99 <= spec.max_p99_latency_ms:
        return f"p99_latency_ms {p99:.0f} > {spec.max_p99_latency_ms:.0f}"
    return None


def _attempt(
    probe: HealthProbe,
    target: DeploymentTarget,
    env_key: str,
    version: str,
    spec: HealthCheckSpec,
) -> str | None:
    running = target.running_version(env_key)
    if running != version:  # health of some other version says nothing about this one
        return f"running version is {running}, expected {version}"
    try:
        sample = probe.sample(env_key, version, spec.timeout_seconds)
    except ProbeError as exc:
        return str(exc)
    return _problem(sample, spec)


def verify(
    probe: HealthProbe,
    target: DeploymentTarget,
    clock: Clock,
    env_key: str,
    version: str,
    spec: HealthCheckSpec,
) -> Verdict:
    """Pass on `required_consecutive` healthy probes; stop once that is out of reach."""
    required = spec.required_consecutive
    probes = consecutive = 0
    problem: str | None = None
    while consecutive < required and spec.attempts - probes >= required - consecutive:
        if probes:
            clock.sleep(spec.interval_seconds)
        problem = _attempt(probe, target, env_key, version, spec)
        probes += 1
        consecutive = consecutive + 1 if problem is None else 0

    passed = consecutive >= required
    summary = f"{consecutive}/{required} consecutive healthy"
    if not passed:
        summary += f" after {probes} probe{'' if probes == 1 else 's'} ({problem})"
    return Verdict(passed=passed, probes=probes, consecutive=consecutive, summary=summary)
