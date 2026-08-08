"""The billing period every "this month" metric is measured over.

One definition, shared by the call-minutes stats (calls/db.py) and the AI-tool
credit counters (usage/db.py), so two cards on the same dashboard can never
disagree about when this month started.

Today that period is the Rome calendar month (same UTC offset as Bratislava, so
it is correct for both IT and SK tenants). Timestamps are stored as UTC ISO
strings, so the bounds come back as UTC ISO strings too and compare directly
against them.
"""

import datetime
from typing import Optional
from zoneinfo import ZoneInfo

_ROME = ZoneInfo("Europe/Rome")


def month_bounds_utc(
    now: Optional[datetime.datetime] = None,
) -> tuple[int, int, str, str]:
    """(year, month, start_utc_iso, next_month_utc_iso) for the Rome calendar
    month containing `now` (defaults to the current instant)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    now_rome = now.astimezone(_ROME)
    start_rome = now_rome.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_rome.month == 12:
        next_rome = start_rome.replace(year=start_rome.year + 1, month=1)
    else:
        next_rome = start_rome.replace(month=start_rome.month + 1)
    start_utc = start_rome.astimezone(datetime.timezone.utc).isoformat()
    next_utc = next_rome.astimezone(datetime.timezone.utc).isoformat()
    return start_rome.year, start_rome.month, start_utc, next_utc
