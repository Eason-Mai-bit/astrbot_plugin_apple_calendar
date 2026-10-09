"""Recurrence expansion, reminder scheduling and occurrence bookkeeping.

Pure functions + a tiny store layer, no AstrBot imports so it can be unit
tested standalone.
"""

import datetime as _dt
import json
import os
import uuid
from typing import Any, Optional

DATE_FMT = "%Y-%m-%d"
DATETIME_FMT = "%Y-%m-%dT%H:%M:%S"

REPEAT_CHOICES = ("none", "daily", "weekly", "monthly")
COLOR_CHOICES = ("blue", "teal", "indigo", "slate")


def parse_date(value: str) -> _dt.date:
    """Parse YYYY-MM-DD; raises ValueError with a readable message."""
    try:
        return _dt.datetime.strptime(str(value).strip()[:10], DATE_FMT).date()
    except Exception:
        raise ValueError(f"日期格式应为 YYYY-MM-DD，收到: {value!r}")


def parse_datetime(value: Any) -> _dt.datetime:
    """Parse flexible datetime input into a naive local datetime.

    Accepts 'YYYY-MM-DD HH:MM(:SS)', 'YYYY-MM-DDTHH:MM(:SS)' and
    'YYYY-MM-DD' (midnight). Raises ValueError with a readable message.
    """
    s = str(value or "").strip().replace("T", " ").replace("t", " ")
    if not s:
        raise ValueError("缺少时间")
    s = s.split("+")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"时间格式应为 'YYYY-MM-DD HH:MM'，收到: {value!r}")


def fmt_dt(dt: _dt.datetime) -> str:
    return dt_str(dt)


def dt_str(dt: _dt.datetime) -> str:
    return dt.strftime(DATETIME_FMT)


class RecurrenceError(ValueError):
    pass


def occurrence_starts(
    event: dict, from_date: _dt.date, to_date: _dt.date
) -> list[_dt.datetime]:
    """Expand an event into occurrence start datetimes within [from, to].

    All-day events yield midnight of each covered day. Multi-day events yield
    their original start only when the range overlaps.
    """
    start = parse_datetime(event["start"])
    end = parse_datetime(event.get("end") or event["start"])
    span = max(end - start, _dt.timedelta(0))
    repeat = event.get("repeat") or "none"
    first_day = start.date()
    if repeat == "none" or span > _dt.timedelta(days=1):
        # Multi-day events are not re-expanded: they render as one long bar.
        if first_day > to_date or (start + span).date() < from_date:
            return []
        return [start]

    step = {
        "daily": _dt.timedelta(days=1),
        "weekly": _dt.timedelta(days=7),
    }
    starts: list[_dt.datetime] = []
    if repeat in step:
        # Walk occurrences back to the range head.
        delta = from_date - first_day
        unit = step[repeat]
        if repeat == "weekly":
            k = max(0, delta.days // 7)
        else:
            k = max(0, delta.days)
        cur = start + k * unit
        while cur.date() <= to_date:
            if cur.date() >= from_date and cur.date() >= first_day:
                starts.append(cur)
            cur = cur + unit
            if len(starts) > 400:  # safety bound for monthly/daily hops
                break
        return starts

    if repeat == "monthly":
        # Same day-of-month, clamped to month length; skip months without it.
        day = start.day
        y, m = from_date.year, from_date.month
        # step back one month to catch the leading edge
        if m == 1:
            y, m = y - 1, 12
        else:
            m -= 1
        for _ in range(64):  # hard cap: enough for any sane range
            try:
                cand = start.replace(year=y, month=m, day=day)
            except ValueError:
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
                continue
            if cand.date() > to_date:
                break
            if cand.date() >= from_date and cand >= start:
                starts.append(cand)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return starts

    return []


def occurrence_end(event: dict, occ_start: _dt.datetime) -> _dt.datetime:
    end = parse_datetime(event.get("end") or event["start"])
    return occ_start + max(end - parse_datetime(event["start"]), _dt.timedelta(0))


def reminder_fire_time(
    event: dict,
    occ_start: _dt.datetime,
    all_day_remind_time: str = "09:00",
) -> Optional[_dt.datetime]:
    """When the reminder for an occurrence should pop. None = no reminder."""
    minutes = event.get("remind_minutes")
    if minutes is None:
        return None
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return None
    if minutes <= 0:
        return None
    if event.get("all_day"):
        try:
            hh, mm = all_day_remind_time.split(":")
            fire = occ_start.replace(hour=int(hh), minute=int(mm), second=0)
        except Exception:
            fire = occ_start.replace(hour=9, minute=0, second=0)
        return fire
    return occ_start - _dt.timedelta(minutes=minutes)


def occ_key(event_id: str, occ_start: _dt.datetime) -> str:
    return f"{event_id}|{occ_start.strftime(DATETIME_FMT)}"


# ---------------------------------------------------------------------------
# store: events.json + state.json with atomic writes
# ---------------------------------------------------------------------------


def atomic_write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_json_file(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if data is not None else default
    except FileNotFoundError:
        return default
    except Exception as exc:
        # Corrupted store: keep the bytes aside so the user can recover them.
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
        print(f"[astrbot_plugin_apple_calendar] corrupt json {path}: {exc}")
        return default


def now_dt() -> _dt.datetime:
    return _dt.datetime.now()
