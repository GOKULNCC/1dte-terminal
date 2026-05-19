"""
Event-window guard.

ForexFactory econ_calendar rows store wall-clock times in US Eastern.
We treat any high-impact USD event within +/- WINDOW_MIN minutes of `now`
as "blocking": for 0DTE long premium, IV crush + headline whipsaw around
CPI/FOMC/NFP makes the model's directional read close to useless.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import db_connect

ET = ZoneInfo("America/New_York")
WINDOW_MIN = 30  # minutes before AND after each high-impact event


_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*(am|pm)?$")


def _event_dt_utc(date_str: str, time_str: str) -> datetime | None:
    """Build a UTC datetime from a calendar row, or None if non-timed (All Day / Tentative)."""
    if not time_str:
        return None
    s = time_str.strip().lower()
    if s in ("all day", "tentative", "—", ""):
        return None
    m = _TIME_RE.match(s)
    if not m:
        return None
    hh, mm, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
    if ampm == "pm" and hh != 12:
        hh += 12
    elif ampm == "am" and hh == 12:
        hh = 0
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None
    et_dt = datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET)
    return et_dt.astimezone(timezone.utc)


def active_window_events(
    now_utc: datetime | None = None,
    window_min: int = WINDOW_MIN,
    look_ahead_min: int | None = None,
) -> list[dict]:
    """Return high-impact USD events within +/- window_min minutes of `now_utc`.

    look_ahead_min overrides the forward half of the window — useful for the
    dashboard banner ("show events in the next 60 min so the trader sees them
    approaching") versus the hard SKIP gate (strict 30 min both sides).
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    forward = look_ahead_min if look_ahead_min is not None else window_min

    # Query a 3-day window in ET-date space to safely span TZ rollover.
    now_et = now_utc.astimezone(ET).date()
    candidate_dates = [
        (now_et - timedelta(days=1)).isoformat(),
        now_et.isoformat(),
        (now_et + timedelta(days=1)).isoformat(),
    ]

    conn = db_connect(row_factory=True)
    try:
        placeholders = ",".join("?" * len(candidate_dates))
        rows = conn.execute(
            f"SELECT * FROM econ_calendar WHERE date IN ({placeholders}) "
            f"AND currency = 'USD' AND impact = 'high'",
            candidate_dates,
        ).fetchall()
    finally:
        conn.close()

    hits: list[dict] = []
    for r in rows:
        ev_utc = _event_dt_utc(r["date"], r["time"])
        if ev_utc is None:
            continue
        delta_min = (ev_utc - now_utc).total_seconds() / 60.0
        if -window_min <= delta_min <= forward:
            hits.append({
                "id": r["id"],
                "event": r["event"],
                "date": r["date"],
                "time_et": r["time"],
                "minutes_until": round(delta_min, 1),
                "currency": r["currency"],
                "impact": r["impact"],
                "forecast": r["forecast"],
                "previous": r["previous"],
            })
    hits.sort(key=lambda h: abs(h["minutes_until"]))
    return hits


def should_block(now_utc: datetime | None = None) -> tuple[bool, list[dict]]:
    """Strict guard for predict_move(): blocking iff any event is within +/- WINDOW_MIN."""
    hits = active_window_events(now_utc, window_min=WINDOW_MIN, look_ahead_min=WINDOW_MIN)
    return (bool(hits), hits)
