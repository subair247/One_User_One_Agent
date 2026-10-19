"""Time helpers. All timestamps are UTC; users live in IST (UTC+05:30)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def parse(s):
    if s is None or s == "":
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def iso(dt):
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ist_day(dt):
    return dt.astimezone(IST).date()


def ist_hour(dt):
    return dt.astimezone(IST).hour


def in_quiet_hours(dt, start_h=22, end_h=8):
    h = dt.astimezone(IST).hour
    return h >= start_h or h < end_h


def next_send_slot(earliest, preferred_hour, start_h=22, end_h=8):
    """First instant >= earliest that is outside quiet hours and, when possible, at the preferred IST hour.

    If `earliest` is already allowed it is kept only when it is not later than the preferred hour that day;
    otherwise the send is moved to the next preferred-hour boundary. Quiet hours are never returned.
    """
    ph = min(max(preferred_hour, end_h + 1), start_h - 1)
    e = earliest.astimezone(IST)
    cand = e.replace(hour=ph, minute=0, second=0, microsecond=0)
    if cand < e:
        cand += timedelta(days=1)
    return cand.astimezone(UTC)
