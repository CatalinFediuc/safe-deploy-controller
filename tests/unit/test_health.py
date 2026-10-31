import json
import math
from pathlib import Path

import pytest

from conftest import FakeClock
from deployctl.errors import HealthProfileError, ProbeError, ProbeTimeout
from deployctl.health import FixtureHealthProbe, HealthProbe, HealthSample, Verdict, verify
from deployctl.manifest import load_manifest
from deployctl.models import HealthCheckSpec

ENV = "orders-api/staging"
VERSION = "1.5.0"
EXAMPLES = Path(__file__).parents[2] / "examples"
# Differs from every default, so a test cannot pass by falling back to one.
SPEC = HealthCheckSpec(
    attempts=5,
    required_consecutive=3,
    interval_seconds=0.25,
    timeout_seconds=1.5,
    max_error_rate=0.02,
    max_p99_latency_ms=400.0,
    rollout_timeout_seconds=5.0,
)
HEALTHY = HealthSample(error_rate=0.001, p99_latency_ms=100.0)
UNHEALTHY = HealthSample(error_rate=0.120, p99_latency_ms=950.0)


class ScriptedProbe:
    """Answers from a script, repeating the last entry, and records every call."""

    def __init__(self, *script: HealthSample | ProbeError) -> None:
        self._script = script
        self.calls: list[tuple[str, str, float]] = []

    def sample(self, env_key: str, version: str, timeout_seconds: float) -> HealthSample:
        answer = self._script[min(len(self.calls), len(self._script) - 1)]
        self.calls.append((env_key, version, timeout_seconds))
        if isinstance(answer, ProbeError):
            raise answer
        return answer


class ScriptedTarget:
    """Reports a sequence of running versions, repeating the last; verify must only read it."""

    def __init__(self, *versions: str | None) -> None:
        self._versions = list(versions)

    def running_version(self, env_key: str) -> str | None:
        return self._versions.pop(0) if len(self._versions) > 1 else self._versions[0]

    def apply(self, env_key: str, version: str) -> None:
        raise AssertionError("verify must not change the target")

    def remove(self, env_key: str) -> None:
        raise AssertionError("verify must not change the target")


def run_verify(
    probe: HealthProbe,
    clock: FakeClock,
    spec: HealthCheckSpec = SPEC,
    target: ScriptedTarget | None = None,
) -> Verdict:
    return verify(probe, target or ScriptedTarget(VERSION), clock, ENV, VERSION, spec)


def test_healthy_passes_after_the_required_streak_and_stops(clock: FakeClock) -> None:
    probe = ScriptedProbe(HEALTHY)
    verdict = run_verify(probe, clock)
    assert verdict == Verdict(
        passed=True, probes=3, consecutive=3, summary="3/3 consecutive healthy"
    )
    assert probe.calls == [(ENV, VERSION, 1.5)] * 3


def test_unhealthy_fails_with_the_documented_summary(clock: FakeClock) -> None:
    verdict = run_verify(ScriptedProbe(UNHEALTHY), clock)
    assert verdict == Verdict(
        passed=False,
        probes=3,
        consecutive=0,
        summary="0/3 consecutive healthy after 3 probes (error_rate 0.120 > 0.020)",
    )


def test_flaky_start_still_passes(clock: FakeClock) -> None:
    verdict = run_verify(ScriptedProbe(UNHEALTHY, HEALTHY), clock)
    assert verdict.passed
    assert verdict.probes == 4


def test_failure_resets_the_streak(clock: FakeClock) -> None:
    probe = ScriptedProbe(HEALTHY, HEALTHY, UNHEALTHY, HEALTHY)
    verdict = run_verify(probe, clock)
    # Two healthy probes are lost and the two attempts left cannot reach three.
    assert not verdict.passed
    assert verdict.probes == 3


def test_streak_can_finish_on_the_last_attempt(clock: FakeClock) -> None:
    verdict = run_verify(ScriptedProbe(UNHEALTHY, UNHEALTHY, HEALTHY), clock)
    assert verdict.passed
    assert verdict.probes == 5


def test_stops_as_soon_as_passing_is_impossible(clock: FakeClock) -> None:
    probe = ScriptedProbe(HEALTHY, UNHEALTHY)
    spec = HealthCheckSpec(attempts=20, required_consecutive=19)
    verdict = run_verify(probe, clock, spec)
    assert not verdict.passed
    assert len(probe.calls) == verdict.probes == 2


def test_sleeps_only_between_probes_and_only_on_the_clock(clock: FakeClock) -> None:
    run_verify(ScriptedProbe(HEALTHY), clock)
    assert clock.sleeps == [0.25, 0.25]


def test_single_probe_never_sleeps(clock: FakeClock) -> None:
    spec = HealthCheckSpec(attempts=1, required_consecutive=1)
    verdict = run_verify(ScriptedProbe(UNHEALTHY), clock, spec)
    assert verdict.summary == ("0/1 consecutive healthy after 1 probe (error_rate 0.120 > 0.010)")
    assert clock.sleeps == []


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (ProbeTimeout("probe timed out after 2s"), "probe timed out after 2s"),
        (ProbeError("probe returned an error"), "probe returned an error"),
        (ProbeError(), "probe failed with no detail"),  # must not read as "no problem"
        (ProbeTimeout(), "probe failed with no detail"),
    ],
)
def test_probe_failures_count_as_failed_probes(
    clock: FakeClock, failure: ProbeError, reason: str
) -> None:
    verdict = run_verify(ScriptedProbe(failure), clock)
    assert not verdict.passed
    assert verdict.summary == f"0/3 consecutive healthy after 3 probes ({reason})"


def test_timeout_then_healthy_passes(clock: FakeClock) -> None:
    assert run_verify(ScriptedProbe(ProbeTimeout("slow"), HEALTHY), clock).passed


def test_no_data_fails(clock: FakeClock) -> None:
    verdict = run_verify(ScriptedProbe(HealthSample(None, None)), clock)
    assert not verdict.passed
    assert verdict.summary.endswith("(no data)")


@pytest.mark.parametrize(
    ("sample", "reason"),
    [
        (HealthSample(None, 100.0), "error_rate missing"),
        (HealthSample(0.001, None), "p99_latency_ms missing"),
    ],
)
def test_one_missing_metric_fails(clock: FakeClock, sample: HealthSample, reason: str) -> None:
    verdict = run_verify(ScriptedProbe(sample), clock)
    assert not verdict.passed
    assert verdict.summary.endswith(f"({reason})")


@pytest.mark.parametrize(
    ("sample", "reason"),
    [
        (HealthSample(math.nan, 100.0), "error_rate invalid (nan)"),
        (HealthSample(0.001, math.nan), "p99_latency_ms invalid (nan)"),
        (HealthSample(math.inf, 100.0), "error_rate invalid (inf)"),
        (HealthSample(0.001, math.inf), "p99_latency_ms invalid (inf)"),
        (HealthSample(-1.0, 100.0), "error_rate invalid (-1.0)"),
        (HealthSample(0.001, -1.0), "p99_latency_ms invalid (-1.0)"),
    ],
)
def test_values_that_are_not_measurements_fail(
    clock: FakeClock, sample: HealthSample, reason: str
) -> None:
    verdict = run_verify(ScriptedProbe(sample), clock)
    assert not verdict.passed
    assert verdict.summary.endswith(f"({reason})")


def test_zero_metrics_are_healthy(clock: FakeClock) -> None:
    assert run_verify(ScriptedProbe(HealthSample(0.0, 0.0)), clock).passed


@pytest.mark.parametrize(
    ("sample", "passed"),
    [
        (HealthSample(0.02, 400.0), True),
        (HealthSample(0.0200001, 400.0), False),
        (HealthSample(0.02, 400.1), False),
    ],
)
def test_thresholds_are_inclusive(clock: FakeClock, sample: HealthSample, passed: bool) -> None:
    assert run_verify(ScriptedProbe(sample), clock).passed is passed


def test_latency_problem_is_reported(clock: FakeClock) -> None:
    verdict = run_verify(ScriptedProbe(HealthSample(0.001, 950.0)), clock)
    assert verdict.summary.endswith("(p99_latency_ms 950 > 400)")


@pytest.mark.parametrize("running", ["1.4.2", None])
def test_version_lost_after_the_rollout_fails_without_probing(
    clock: FakeClock, running: str | None
) -> None:
    probe = ScriptedProbe(HEALTHY)
    verdict = run_verify(probe, clock, target=ScriptedTarget(VERSION, running))
    assert not verdict.passed
    assert verdict.summary.endswith(f"(running version is {running}, expected {VERSION})")
    assert probe.calls == []


def test_version_changing_mid_verification_resets_the_streak(clock: FakeClock) -> None:
    target = ScriptedTarget(VERSION, VERSION, VERSION, "1.4.2", VERSION)
    probe = ScriptedProbe(HEALTHY)
    verdict = run_verify(probe, clock, target=target)
    assert not verdict.passed
    assert verdict.probes == 3
    assert len(probe.calls) == 2


def test_slow_rollout_waits_without_using_up_attempts(clock: FakeClock) -> None:
    probe = ScriptedProbe(HEALTHY)
    target = ScriptedTarget("1.4.2", "1.4.2", VERSION)
    verdict = run_verify(probe, clock, target=target)
    assert verdict.passed
    assert verdict.probes == 3
    assert clock.sleeps == [1.0, 1.0, 0.25, 0.25]


def test_rollout_may_finish_exactly_at_the_timeout(clock: FakeClock) -> None:
    target = ScriptedTarget(*["1.4.2"] * 5, VERSION)
    assert run_verify(ScriptedProbe(HEALTHY), clock, target=target).passed


@pytest.mark.parametrize("running", ["1.4.2", None])
def test_rollout_timeout_fails_without_probing(clock: FakeClock, running: str | None) -> None:
    probe = ScriptedProbe(HEALTHY)
    verdict = run_verify(probe, clock, target=ScriptedTarget(running))
    assert verdict == Verdict(
        passed=False,
        probes=0,
        consecutive=0,
        summary=(
            "0/3 consecutive healthy (rollout not finished after 5s: "
            f"running version is {running}, expected {VERSION})"
        ),
    )
    assert probe.calls == []
    assert clock.sleeps == [1.0] * 5


def test_last_rollout_wait_stops_at_the_timeout(clock: FakeClock) -> None:
    spec = HealthCheckSpec(rollout_timeout_seconds=2.5)
    run_verify(ScriptedProbe(HEALTHY), clock, spec, ScriptedTarget("1.4.2"))
    assert clock.sleeps == [1.0, 1.0, 0.5]


def test_rollout_wait_is_on_by_default(clock: FakeClock) -> None:
    run_verify(ScriptedProbe(HEALTHY), clock, HealthCheckSpec(), ScriptedTarget("1.4.2"))
    assert sum(clock.sleeps) == 60.0


def test_zero_rollout_timeout_checks_once_without_waiting(clock: FakeClock) -> None:
    spec = HealthCheckSpec(rollout_timeout_seconds=0)
    assert not run_verify(ScriptedProbe(HEALTHY), clock, spec, ScriptedTarget("1.4.2")).passed
    assert clock.sleeps == []


def test_slow_target_calls_count_towards_the_rollout_timeout(clock: FakeClock) -> None:
    class SlowTarget(ScriptedTarget):
        def running_version(self, env_key: str) -> str | None:
            clock.advance(3.0)
            return super().running_version(env_key)

    verdict = run_verify(ScriptedProbe(HEALTHY), clock, target=SlowTarget("1.4.2"))
    assert not verdict.passed
    assert clock.sleeps == [1.0]


def write_profile(tmp_path: Path, content: object) -> Path:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


def profile_probe(tmp_path: Path, responses: list[object]) -> FixtureHealthProbe:
    return FixtureHealthProbe(write_profile(tmp_path, {"orders-api": {VERSION: responses}}))


def test_fixture_replays_the_sequence_and_repeats_the_last(tmp_path: Path) -> None:
    first = {"error_rate": 0.1, "p99_latency_ms": 100}
    last = {"error_rate": 0.2, "p99_latency_ms": 200}
    probe = profile_probe(tmp_path, [first, last])
    samples = [probe.sample(ENV, VERSION, 1.0) for _ in range(4)]
    assert samples == [
        HealthSample(0.1, 100),
        HealthSample(0.2, 200),
        HealthSample(0.2, 200),
        HealthSample(0.2, 200),
    ]


def test_fixture_timeout_entry_raises_and_names_the_timeout(tmp_path: Path) -> None:
    probe = profile_probe(tmp_path, ["timeout"])
    with pytest.raises(ProbeTimeout, match=r"after 1\.5s"):
        probe.sample(ENV, VERSION, 1.5)


def test_fixture_error_entry_raises_a_plain_probe_error(tmp_path: Path) -> None:
    probe = profile_probe(tmp_path, ["error"])
    with pytest.raises(ProbeError) as raised:
        probe.sample(ENV, VERSION, 1.0)
    assert not isinstance(raised.value, ProbeTimeout)


@pytest.mark.parametrize(
    ("env_key", "version"),
    [(ENV, "9.9.9"), ("billing-api/staging", VERSION)],
)
def test_fixture_returns_no_data_for_anything_unlisted(
    tmp_path: Path, env_key: str, version: str
) -> None:
    probe = profile_probe(tmp_path, [{"error_rate": 0.1, "p99_latency_ms": 100}])
    assert probe.sample(env_key, version, 1.0) == HealthSample(None, None)


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"error_rate": 0.1}, HealthSample(0.1, None)),
        ({"p99_latency_ms": 100}, HealthSample(None, 100)),
        ({"error_rate": None, "p99_latency_ms": 100}, HealthSample(None, 100)),
        ({}, HealthSample(None, None)),
    ],
)
def test_fixture_can_express_missing_metrics(
    tmp_path: Path, entry: object, expected: HealthSample
) -> None:
    assert profile_probe(tmp_path, [entry]).sample(ENV, VERSION, 1.0) == expected


def test_fixture_sequences_are_kept_per_environment_and_version(tmp_path: Path) -> None:
    healthy = {"error_rate": 0.1, "p99_latency_ms": 100}
    probe = profile_probe(tmp_path, ["timeout", healthy])
    with pytest.raises(ProbeTimeout):
        probe.sample(ENV, VERSION, 1.0)
    # A different environment starts at the beginning of the same sequence.
    with pytest.raises(ProbeTimeout):
        probe.sample("orders-api/prod", VERSION, 1.0)
    assert probe.sample(ENV, VERSION, 1.0) == HealthSample(0.1, 100)


INVALID_PROFILES = [
    "{not json",
    "[]",
    "[" * 100_000,
    '{"orders-api": []}',
    '{"orders-api": {"1.5.0": {}}}',
    '{"orders-api": {"1.5.0": []}}',
    '{"orders-api": {"1.5.0": "timeout"}}',
    '{"orders-api": {"1.5.0": ["slow"]}}',
    '{"orders-api": {"1.5.0": [7]}}',
    '{"orders-api": {"1.5.0": [null]}}',
    '{"orders-api": {"1.5.0": [{"error_rte": 0.1}]}}',
    '{"orders-api": {"1.5.0": [{"error_rate": "0.1"}]}}',
    '{"orders-api": {"1.5.0": [{"error_rate": true}]}}',
    '{"orders-api": {"1.5.0": [{"error_rate": -0.1}]}}',
    '{"orders-api": {"1.5.0": [{"error_rate": NaN}]}}',
    '{"orders-api": {"1.5.0": [{"p99_latency_ms": 1e999}]}}',
    '{"Orders_API": {"1.5.0": ["timeout"]}}',
    '{"orders-api\\n": {"1.5.0": ["timeout"]}}',
    '{"orders-api": {"1.6.O": ["timeout"]}}',
    '{"orders-api": {"1.5": ["timeout"]}}',
    '{"orders-api": {"01.5.0": ["timeout"]}}',
    '{"orders-api": {"1.5.0\\n": ["timeout"]}}',
]


@pytest.mark.parametrize("content", INVALID_PROFILES)
def test_invalid_profile_is_refused_when_loaded(tmp_path: Path, content: str) -> None:
    path = tmp_path / "profile.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(HealthProfileError, match=r"profile\.json"):
        FixtureHealthProbe(path)


def test_error_names_the_bad_entry(tmp_path: Path) -> None:
    good = {"error_rate": 0.1, "p99_latency_ms": 100}
    path = write_profile(tmp_path, {"orders-api": {VERSION: [good, "slow"]}})
    with pytest.raises(HealthProfileError, match=r"orders-api 1\.5\.0\[1\]"):
        FixtureHealthProbe(path)


@pytest.mark.parametrize(
    ("content", "named"),
    [
        ({"orders_api": {VERSION: ["timeout"]}}, "orders_api"),
        ({"orders-api": {"1.6.O": ["timeout"]}}, r"orders-api 1\.6\.O"),
    ],
)
def test_error_names_the_bad_key(tmp_path: Path, content: object, named: str) -> None:
    with pytest.raises(HealthProfileError, match=named):
        FixtureHealthProbe(write_profile(tmp_path, content))


def test_missing_profile_is_refused(tmp_path: Path) -> None:
    with pytest.raises(HealthProfileError):
        FixtureHealthProbe(tmp_path / "absent.json")


def example_verdict(profile: str, version: str, clock: FakeClock) -> Verdict:
    manifest = load_manifest(
        EXAMPLES / "manifests" / f"orders-api-{version}.json", EXAMPLES / "artifacts"
    )
    return verify(
        FixtureHealthProbe(EXAMPLES / "health" / profile),
        ScriptedTarget(version),
        clock,
        manifest.env_key,
        version,
        manifest.health_check,
    )


@pytest.mark.parametrize(
    ("version", "passed", "probes"),
    [("1.4.2", True, 3), ("1.5.0", False, 3), ("1.5.1", True, 4), ("1.6.0", True, 3)],
)
def test_default_example_profile(clock: FakeClock, version: str, passed: bool, probes: int) -> None:
    verdict = example_verdict("default.json", version, clock)
    assert (verdict.passed, verdict.probes) == (passed, probes)


@pytest.mark.parametrize("version", ["1.4.2", "1.5.0", "1.5.1", "1.6.0"])
def test_outage_example_profile_fails_every_version(clock: FakeClock, version: str) -> None:
    assert not example_verdict("dependency-outage.json", version, clock).passed
