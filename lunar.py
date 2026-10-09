"""Offline lunar / solar-term / holiday helpers for the calendar plugin.

All external-library calls are wrapped with graceful fallbacks:
- `lunar_python` provides lunar dates, ganzhi, solar terms and festivals.
- `chinese_calendar` provides the State Council's 休/班 arrangements; when the
  library is missing or the queried year is not covered, callers can supply a
  manual override JSON (config `holiday_override_path`) shaped as
  `{"2026-10-01": "休", "2026-10-10": "班", ...}`.

This module stays importable without any third-party library installed.
"""

import datetime as _dt
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

try:  # pragma: no cover - depends on deployed environment
    from lunar_python import Lunar, Solar

    _HAS_LUNAR = True
except Exception:  # pragma: no cover
    _HAS_LUNAR = False

try:  # pragma: no cover
    import chinese_calendar as _cc

    _HAS_CCAL = True
except Exception:  # pragma: no cover
    _HAS_CCAL = False

LogFn = Any

_log = print
_ccal_warned = False
_lunar_warned = False
_ccal_year_range: Optional[tuple[int, int]] = None


def set_logger(fn: LogFn) -> None:
    """Bind astrbot's logger so warnings show in the host log."""
    global _log
    _log = fn


def _warn_once(msg: str) -> None:
    try:
        _log("warning " + msg)
    except Exception:
        pass


def load_holiday_override(path: str) -> dict[str, str]:
    """Load a manual 休/班 override JSON. Returns {} on any problem."""
    if not path:
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        out: dict[str, str] = {}
        for k, v in (raw or {}).items():
            v = str(v).strip()
            if v in ("休", "班"):
                out[str(k)] = v
        return out
    except Exception as exc:
        _warn_once(f"holiday override {path} unreadable: {exc}")
        return {}


def _ccal_year_bounds() -> Optional[tuple[int, int]]:
    """Best-effort detection of the supported year range of chinese_calendar."""
    global _ccal_year_range
    if not _HAS_CCAL:
        return None
    if _ccal_year_range is None:
        lo = getattr(_cc, "MinDate", None)
        hi = getattr(_cc, "MaxDate", None)
        if lo is not None and hi is not None:
            _ccal_year_range = (lo.year, hi.year)
    return _ccal_year_range


def _ccal_holiday(date: _dt.date) -> Optional[str]:
    """Return 休 / 班 for a date, or None when unknown."""
    global _ccal_warned
    if not _HAS_CCAL:
        if not _ccal_warned:
            _warn_once("chinese-calendar not installed; 休/班 badges disabled")
            _ccal_warned = True
        return None
    bounds = _ccal_year_bounds()
    if bounds:
        lo, hi = bounds
        if date.year < lo or date.year > hi:
            return None
    try:
        on_holiday, _name = _cc.get_holiday_detail(date)
    except Exception:
        return None
    if on_holiday:
        return "休"
    try:
        if _cc.is_workday(date) and date.isoweekday() >= 6:
            return "班"
    except Exception:
        return None
    return None


def holiday_badge(
    date: _dt.date, override: Optional[dict[str, str]] = None
) -> Optional[str]:
    """休 / 班 badge; the manual override JSON takes precedence."""
    if override and str(date) in override:
        return override[str(date)]
    return _ccal_holiday(date)


@lru_cache(maxsize=2048)
def _lunar_for(date: _dt.date) -> Optional[tuple[Any, Any]]:
    """Build (Solar, Lunar) for a date; cached so one day costs one build."""
    if not _HAS_LUNAR:
        return None
    try:
        solar = Solar.fromYmd(date.year, date.month, date.day)
        return solar, solar.getLunar()
    except Exception:
        return None


def lunar_text(date: _dt.date) -> str:
    """Short lunar label for a month cell; the lunar month name on 初一."""
    return _lunar_text_cached(date)


@lru_cache(maxsize=2048)
def _lunar_text_cached(date: _dt.date) -> str:
    global _lunar_warned
    pair = _lunar_for(date)
    if pair is None:
        if not _lunar_warned:
            _warn_once("lunar_python not installed; lunar labels disabled")
            _lunar_warned = True
        return ""
    _lunar, lunar = pair
    try:
        day_cn = lunar.getDayInChinese()
        if day_cn == "初一":
            return lunar.getMonthInChinese() + "月"
        return day_cn
    except Exception:
        return ""


def day_header_text(date: _dt.date) -> str:
    """Full lunar header for the day view, e.g. 丙午年八月廿一."""
    return _day_header_cached(date)


@lru_cache(maxsize=2048)
def _day_header_cached(date: _dt.date) -> str:
    global _lunar_warned
    pair = _lunar_for(date)
    if pair is None:
        if not _lunar_warned:
            _warn_once("lunar_python not installed; lunar labels disabled")
            _lunar_warned = True
        return ""
    _lunar, lunar = pair
    try:
        return f"{lunar.getYearInGanZhi()}年{lunar.getMonthInChinese()}月{lunar.getDayInChinese()}"
    except Exception:
        return ""


def lunar_md(date: _dt.date) -> str:
    """Lunar month + day without ganzhi, e.g. 九月十八 (for the mini window)."""
    return _lunar_md_cached(date)


@lru_cache(maxsize=2048)
def _lunar_md_cached(date: _dt.date) -> str:
    global _lunar_warned
    pair = _lunar_for(date)
    if pair is None:
        if not _lunar_warned:
            _warn_once("lunar_python not installed; lunar labels disabled")
            _lunar_warned = True
        return ""
    _lunar, lunar = pair
    try:
        return f"{lunar.getMonthInChinese()}月{lunar.getDayInChinese()}"
    except Exception:
        return ""


def weekday_cn(date: _dt.date) -> str:
    """周一..周日 label."""
    return "周" + "一二三四五六日"[date.weekday() % 7]


def month_cn(month: int) -> str:
    """一月..十二月 label."""
    return [
        "一月", "二月", "三月", "四月", "五月", "六月",
        "七月", "八月", "九月", "十月", "十一月", "十二月",
    ][month - 1]


def festivals_and_terms(date: _dt.date) -> list[str]:
    """Festival / solar-term names falling exactly on this day."""
    return list(_festivals_cached(date))


@lru_cache(maxsize=2048)
def _festivals_cached(date: _dt.date) -> tuple[str, ...]:
    """Cached festival/solar-term lookup; builds the Lunar object once per day."""
    names: list[str] = []
    pair = _lunar_for(date)
    if pair is None:
        return ()
    solar, lunar = pair
    try:
        names.extend(lunar.getFestivals())
        names.extend(solar.getFestivals())
        table = lunar.getJieQiTable()
        if table is not None:
            for key, sol in table.items():
                try:
                    if (
                        sol.getYear() == date.year
                        and sol.getMonth() == date.month
                        and sol.getDay() == date.day
                    ):
                        names.append(str(key))
                except Exception:
                    continue
    except Exception:
        pass
    return tuple(names)
