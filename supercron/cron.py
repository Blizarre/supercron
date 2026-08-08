"""5-field cron expression parsing and next-run computation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


class CronError(Exception):
    """Raised for an invalid cron expression or an unfindable next run."""


@dataclass(frozen=True)
class CronSchedule:
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]

    @classmethod
    def parse(cls, expression: str) -> CronSchedule:
        fields = expression.split()
        if len(fields) != 5:
            raise CronError(
                f"cron expression must have 5 fields, got {len(fields)}: {expression!r}"
            )
        minutes = _parse_field(fields[0], 0, 59)
        hours = _parse_field(fields[1], 0, 23)
        dom = _parse_field(fields[2], 1, 31)
        months = _parse_field(fields[3], 1, 12)
        dow = {0 if v in (0, 7) else v for v in _parse_field(fields[4], 0, 7)}
        return cls(
            minutes=frozenset(minutes),
            hours=frozenset(hours),
            days_of_month=frozenset(dom),
            months=frozenset(months),
            days_of_week=frozenset(dow),
        )

    def matches(self, dt: datetime) -> bool:
        if dt.minute not in self.minutes:
            return False
        if dt.hour not in self.hours:
            return False
        if dt.month not in self.months:
            return False
        return self._day_matches(dt)

    def _day_matches(self, dt: datetime) -> bool:
        dom_wild = self.days_of_month == _FULL_DOM
        dow_wild = self.days_of_week == _FULL_DOW
        dom_match = dt.day in self.days_of_month
        dow_match = dt.isoweekday() % 7 in self.days_of_week
        if dom_wild and dow_wild:
            return True
        if dom_wild:
            return dow_match
        if dow_wild:
            return dom_match
        return dom_match or dow_match

    def next_after(self, base: datetime, horizon_years: int = 3) -> datetime:
        """Return the next datetime strictly after ``base`` that matches, UTC."""
        candidate = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = horizon_years * 366 * 24 * 60
        for _ in range(limit):
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        raise CronError(f"no matching time within {horizon_years} years")


_FULL_DOM = frozenset(range(1, 32))
_FULL_DOW = frozenset(range(0, 7)) | {7}


def _parse_field(spec: str, lo: int, hi: int) -> set[int]:
    """Parse one cron field ('*', list, range, step) into a set of values."""
    result: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            base, step_s = part.split("/")
            step = int(step_s)
        else:
            base = part

        if base == "*":
            rng = (lo, hi)
        elif "-" in base:
            a, b = base.split("-")
            rng = (int(a), int(b))
        else:
            v = int(base)
            rng = (v, v)

        if step <= 0:
            raise CronError(f"invalid step in field {spec!r}")
        for v in range(rng[0], rng[1] + 1, step):
            result.add(v)

    bad = [v for v in result if not (lo <= v <= hi)]
    if bad:
        raise CronError(f"value(s) {bad} out of range [{lo}, {hi}] in {spec!r}")
    return result
