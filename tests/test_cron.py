from datetime import UTC, datetime

import pytest

from supercron.cron import CronError, CronSchedule

UTC = UTC


def dt(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


@pytest.mark.parametrize(
    "expr,base,expected",
    [
        ("*/5 * * * *", dt(2026, 8, 8, 10, 0), dt(2026, 8, 8, 10, 5)),
        ("* * * * *", dt(2026, 8, 8, 10, 59), dt(2026, 8, 8, 11, 0)),
        ("0 12 * * 1-5", dt(2026, 8, 8, 10, 0), dt(2026, 8, 10, 12, 0)),
        ("30 9 * * 0", dt(2026, 8, 8, 10, 0), dt(2026, 8, 9, 9, 30)),
        ("0 0 1 1 *", dt(2026, 8, 8, 10, 0), dt(2027, 1, 1, 0, 0)),
        ("15,45 * * * *", dt(2026, 8, 8, 10, 20), dt(2026, 8, 8, 10, 45)),
        ("0 0 * * 7", dt(2026, 8, 8, 10, 0), dt(2026, 8, 9, 0, 0)),  # Sunday=7
    ],
)
def test_next_after(expr, base, expected):
    assert CronSchedule.parse(expr).next_after(base) == expected


def test_next_after_returns_strictly_after():
    s = CronSchedule.parse("* * * * *")
    base = datetime(2026, 8, 8, 10, 0, 30, tzinfo=UTC)
    assert s.next_after(base) == dt(2026, 8, 8, 10, 1)


def test_matches():
    s = CronSchedule.parse("30 9 * * 1-5")
    assert s.matches(dt(2026, 8, 10, 9, 30))  # Monday
    assert not s.matches(dt(2026, 8, 8, 9, 30))  # Saturday


def test_dom_or_dow_semantics():
    # 0 0 13 * 5 : Friday the 13th (either clause matches)
    s = CronSchedule.parse("0 0 13 * 5")
    fri_13 = dt(2026, 11, 13, 0, 0)  # a Friday the 13th
    assert s.matches(fri_13)


def test_invalid_field_count():
    with pytest.raises(CronError):
        CronSchedule.parse("*/5 * * *")


def test_out_of_range_value():
    with pytest.raises(CronError):
        CronSchedule.parse("60 * * * *")


def test_invalid_step():
    with pytest.raises(CronError):
        CronSchedule.parse("*/0 * * * *")
