from datetime import UTC, datetime, timedelta, timezone

import pytest

from conftest import FakeClock
from deployctl.clock import SystemClock, format_timestamp


def test_system_clock_is_timezone_aware_utc() -> None:
    assert SystemClock().now().utcoffset() == timedelta(0)


def test_system_clock_sleeps_for_the_requested_time(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    SystemClock().sleep(0.25)
    assert sleeps == [0.25]


def test_fake_clock_records_sleeps_and_moves_forward(clock: FakeClock) -> None:
    start = clock.now()
    clock.sleep(1.5)
    assert clock.sleeps == [1.5]
    assert clock.now() - start == timedelta(seconds=1.5)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 11, 1, 10, 15, 2, 300000, tzinfo=UTC), "2026-11-01T10:15:02.300Z"),
        (datetime(2026, 11, 1, 10, 15, 2, tzinfo=UTC), "2026-11-01T10:15:02.000Z"),
        (datetime(2026, 11, 1, 10, 15, 2, 999999, tzinfo=UTC), "2026-11-01T10:15:02.999Z"),
        (
            datetime(2026, 11, 1, 12, 15, 2, tzinfo=timezone(timedelta(hours=2))),
            "2026-11-01T10:15:02.000Z",
        ),
    ],
)
def test_format_timestamp(moment: datetime, expected: str) -> None:
    assert format_timestamp(moment) == expected
