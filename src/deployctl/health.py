import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal, Protocol

from deployctl.clock import Clock
from deployctl.errors import HealthProfileError, ProbeError, ProbeTimeout
from deployctl.manifest import SERVICE_RE, VERSION_RE, is_finite_number
from deployctl.models import HealthCheckSpec
from deployctl.target import DeploymentTarget

_METRICS = frozenset({"error_rate", "p99_latency_ms"})
_ROLLOUT_POLL_SECONDS = 1.0


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
        # A mistyped key would hide the real one, which then reads as "no data" and fails verify.
        if SERVICE_RE.fullmatch(service) is None:
            raise ValueError(f"{service!r} is not a valid service name")
        if not isinstance(versions, dict):
            raise ValueError(f"{service} must be an object of versions")
        profile[service] = {}
        for version, responses in versions.items():
            label = f"{service} {version}"
            if VERSION_RE.fullmatch(version) is None:
                raise ValueError(f"{label} is not MAJOR.MINOR.PATCH")
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


def _metric_problem(name: str, value: float | None, limit: float, digits: int) -> str | None:
    if value is None:
        return f"{name} missing"
    # Written so that NaN fails too, and a negative placeholder such as -1 is not a measurement.
    if not 0 <= value < math.inf:
        return f"{name} invalid ({value})"
    if value > limit:
        return f"{name} {value:.{digits}f} > {limit:.{digits}f}"
    return None


def _problem(sample: HealthSample, spec: HealthCheckSpec) -> str | None:
    error_rate, p99 = sample.error_rate, sample.p99_latency_ms
    if error_rate is None and p99 is None:
        return "no data"
    return _metric_problem("error_rate", error_rate, spec.max_error_rate, 3) or _metric_problem(
        "p99_latency_ms", p99, spec.max_p99_latency_ms, 0
    )


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
        return str(exc) or "probe failed with no detail"
    return _problem(sample, spec)


def _await_rollout(
    target: DeploymentTarget, clock: Clock, env_key: str, version: str, timeout_seconds: float
) -> str | None:
    """Wait for `version` to be running; a slow rollout must not use up the health attempts."""
    deadline = clock.now() + timedelta(seconds=timeout_seconds)
    while True:
        running = target.running_version(env_key)
        if running == version:
            return None
        remaining = (deadline - clock.now()).total_seconds()
        if remaining <= 0:
            return (
                f"rollout not finished after {timeout_seconds:g}s: "
                f"running version is {running}, expected {version}"
            )
        clock.sleep(min(_ROLLOUT_POLL_SECONDS, remaining))


def verify(
    probe: HealthProbe,
    target: DeploymentTarget,
    clock: Clock,
    env_key: str,
    version: str,
    spec: HealthCheckSpec,
) -> Verdict:
    """Wait for the rollout, then pass on `required_consecutive` healthy probes in a row.

    Stops once that is out of reach. The version is still checked before every probe, because
    it can change after the rollout finished.
    """
    required = spec.required_consecutive
    stalled = _await_rollout(target, clock, env_key, version, spec.rollout_timeout_seconds)
    if stalled is not None:
        summary = f"0/{required} consecutive healthy ({stalled})"
        return Verdict(passed=False, probes=0, consecutive=0, summary=summary)

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
