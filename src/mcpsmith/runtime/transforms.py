"""Value transforms a plan can name on tool arguments (input) and output fields (output)."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from .http import ApiError

INPUT_NAMES = frozenset(
    {
        "date->unix",
        "date->unix_end",
        "jalali->unix",
        "jalali->unix_end",
        "jalali->date",
        "date->jalali",
        "toman->rial",
        "int->str",
    }
)
OUTPUT_NAMES = frozenset(
    {"unix->date", "unix->datetime", "date->jalali", "jalali->date", "rial->toman", "minutes->hh:mm"}
)
NAMES = INPUT_NAMES | OUTPUT_NAMES

# Persian (U+06F0-06F9) and Arabic-Indic (U+0660-0669) digits to Latin
_DIGITS = {ord(c): str(i) for i in range(10) for c in (chr(0x6F0 + i), chr(0x660 + i))}
_CUMULATIVE_DAYS = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
_YEARS = (1300, 1500)  # Jalali years the arithmetic is trusted for
_NOWRUZ_1404 = date(2025, 3, 21)


def _jalali_day_number(jy: int, jm: int, jd: int) -> int:
    """Days on a continuous scale; only differences are meaningful (33-year cycle arithmetic)."""
    jy += 1595
    month_days = (jm - 1) * 31 if jm < 7 else (jm - 7) * 30 + 186
    return 365 * jy + (jy // 33) * 8 + ((jy % 33) + 3) // 4 + jd + month_days


def _to_jalali(d: date) -> tuple[int, int, int]:
    gy = d.year + 1 if d.month > 2 else d.year
    days = 355666 + 365 * d.year + (gy + 3) // 4 - (gy + 99) // 100 + (gy + 399) // 400 + d.day
    days += _CUMULATIVE_DAYS[d.month - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def _from_jalali(jy: int, jm: int, jd: int) -> date:
    return _NOWRUZ_1404 + timedelta(days=_jalali_day_number(jy, jm, jd) - _jalali_day_number(1404, 1, 1))


def _parse_date(value: Any) -> date:
    text = str(value).strip().translate(_DIGITS)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    raise ApiError(f"Bad date {value!r}: write it as YYYY-MM-DD, e.g. 2026-10-20.")


def _parse_jalali(value: Any) -> date:
    text = str(value).strip().translate(_DIGITS)
    m = re.fullmatch(r"(\d{4})/(\d{1,2})/(\d{1,2})", text)
    if m:
        jy, jm, jd = map(int, m.groups())
        if not _YEARS[0] <= jy <= _YEARS[1]:
            raise ApiError(f"Jalali year {jy} is out of range: use {_YEARS[0]} to {_YEARS[1]}.")
        if 1 <= jm <= 12 and 1 <= jd <= 31:
            d = _from_jalali(jy, jm, jd)
            if _to_jalali(d) == (jy, jm, jd):  # rejects 31 in months 7-12 and 30 Esfand in a common year
                return d
    raise ApiError(f"Bad Jalali date {value!r}: write it as YYYY/MM/DD, e.g. 1405/07/28.")


def _jalali_text(d: date) -> str:
    jy, jm, jd = _to_jalali(d)
    if not _YEARS[0] <= jy <= _YEARS[1]:
        raise ApiError(f"Date {d} is out of range: Jalali years {_YEARS[0]} to {_YEARS[1]} are supported.")
    return f"{jy}/{jm:02d}/{jd:02d}"


def _zone(tz: str) -> tzinfo:
    if not tz:
        return datetime.now().astimezone().tzinfo  # type: ignore[return-value]
    try:
        return ZoneInfo(tz)
    except Exception as e:
        raise ApiError(f"Unknown time zone {tz!r}: use an IANA name such as Asia/Tehran.") from e


def _unix(d: date, tz: str, end: bool = False) -> int:
    start = int(datetime(d.year, d.month, d.day, tzinfo=_zone(tz)).timestamp())
    return start + 86399 if end else start


def _number(value: Any) -> float:
    try:
        return float(str(value).translate(_DIGITS).replace(",", ""))
    except ValueError:
        raise ApiError(f"Bad number {value!r}: pass a plain number, e.g. 125000.") from None


def _whole(x: float) -> int | float:
    return int(x) if x == int(x) else x


def _from_unix(value: Any, tz: str) -> datetime:
    return datetime.fromtimestamp(_number(value), _zone(tz))


def _minutes(value: Any) -> str:
    hours, minutes = divmod(int(_number(value)), 60)
    return f"{hours:02d}:{minutes:02d}"


_IN: dict[str, Callable[[Any, str], Any]] = {
    "date->unix": lambda v, tz: _unix(_parse_date(v), tz),
    "date->unix_end": lambda v, tz: _unix(_parse_date(v), tz, end=True),
    "jalali->unix": lambda v, tz: _unix(_parse_jalali(v), tz),
    "jalali->unix_end": lambda v, tz: _unix(_parse_jalali(v), tz, end=True),
    "jalali->date": lambda v, tz: _parse_jalali(v).isoformat(),
    "date->jalali": lambda v, tz: _jalali_text(_parse_date(v)),
    "toman->rial": lambda v, tz: _whole(_number(v) * 10),
    "int->str": lambda v, tz: str(v),
}
_OUT: dict[str, Callable[[Any, str], Any]] = {
    "unix->date": lambda v, tz: _from_unix(v, tz).date().isoformat(),
    "unix->datetime": lambda v, tz: _from_unix(v, tz).strftime("%Y-%m-%d %H:%M"),
    "date->jalali": lambda v, tz: _jalali_text(_parse_date(str(v)[:10])),
    "jalali->date": lambda v, tz: _parse_jalali(v).isoformat(),
    "rial->toman": lambda v, tz: _whole(_number(v) / 10),
    "minutes->hh:mm": lambda v, tz: _minutes(v),
}


def transform_in(name: str, value: Any, tz: str = "") -> Any:
    """Turn what the model passed into the API's wire format. None passes through."""
    if name not in _IN:
        raise ApiError(f"Unknown transform {name!r}; input transforms: {', '.join(sorted(_IN))}.")
    return None if value is None else _IN[name](value, tz)


def transform_out(name: str, value: Any, tz: str = "") -> Any:
    """Turn an API output value into what the model should read. None passes through."""
    if name not in _OUT:
        raise ApiError(f"Unknown transform {name!r}; output transforms: {', '.join(sorted(_OUT))}.")
    return None if value is None else _OUT[name](value, tz)
