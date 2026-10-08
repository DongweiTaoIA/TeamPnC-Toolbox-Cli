from __future__ import annotations

from collections.abc import Collection
from datetime import date, timedelta


def add_business_days(start: date, offset: int, holidays: Collection[date] = ()) -> date:
    if offset == 0:
        return start

    holiday_dates = set(holidays)
    direction = 1 if offset > 0 else -1
    remaining = abs(offset)
    result = start
    while remaining:
        result += timedelta(days=direction)
        if result.weekday() < 5 and result not in holiday_dates:
            remaining -= 1
    return result