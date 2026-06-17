"""Small, pure helpers shared across parsers and scrapers.

Keeping these dependency-free and side-effect-free makes the parsing layer
trivially unit-testable.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Iterable, Iterator, Optional, Tuple


# --------------------------------------------------------------------------
# String / slug helpers
# --------------------------------------------------------------------------

_slug_strip = re.compile(r"[^a-zA-Z0-9]+")


def slugify(value: str) -> str:
    """HLTV uses ascii, dash-separated slugs in URLs (e.g. 's1mple')."""
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = _slug_strip.sub("-", value).strip("-").lower()
    return value or "x"


# --------------------------------------------------------------------------
# Numeric parsing — HLTV mixes "1,234", "53.4%", "1.15", "+0.07", "-" / "" etc.
# --------------------------------------------------------------------------

_num_re = re.compile(r"-?\d[\d,]*\.?\d*")


def parse_int(text: Optional[str]) -> Optional[int]:
    if text is None:
        return None
    m = _num_re.search(text.replace("−", "-"))  # unicode minus
    if not m:
        return None
    try:
        return int(float(m.group(0).replace(",", "")))
    except ValueError:
        return None


def parse_float(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    m = _num_re.search(text.replace("−", "-"))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_percent(text: Optional[str]) -> Optional[float]:
    """Return a percentage as a fraction in [0, 1] (e.g. '53.4%' -> 0.534)."""
    v = parse_float(text)
    if v is None:
        return None
    return v / 100.0


def first_int(text: Optional[str]) -> Optional[int]:
    """Extract the first integer in arbitrary text (e.g. URLs)."""
    if text is None:
        return None
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


# --------------------------------------------------------------------------
# Date helpers
# --------------------------------------------------------------------------

_HLTV_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%b %d, %Y",       # "Aug 21, 2012"
    "%d %b %Y",        # "21 Aug 2012"
    "%dth of %B %Y",   # rarely used long form
)


def parse_date(text: Optional[str]) -> Optional[date]:
    if not text:
        return None
    text = text.strip()
    # Strip ordinal suffixes ("21st of August 2012" -> "21 of August 2012")
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", text)
    cleaned = cleaned.replace(" of ", " ")
    for fmt in _HLTV_DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


def month_starts(start: date, end: date) -> Iterator[date]:
    """Yield the first day of each month in [start, end] inclusive."""
    cur = date(start.year, start.month, 1)
    while cur <= end:
        yield cur
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)


def iter_periods(
    start: date, end: date, months: int = 6
) -> Iterator[Tuple[date, date]]:
    """Split [start, end] into consecutive (start, end) windows of ``months``.

    HLTV stat pages take ``startDate``/``endDate`` filters. Querying in fixed
    windows lets us build *time-sliced* player profiles (so a player's form in
    2015 is distinguishable from 2021) which is exactly what the meta /
    contribution modelling needs.
    """
    cur = start
    while cur <= end:
        y = cur.year + (cur.month - 1 + months) // 12
        m = (cur.month - 1 + months) % 12 + 1
        nxt = date(y, m, 1)
        window_end = min(end, nxt - timedelta(days=1))
        yield cur, window_end
        cur = nxt


def chunked(seq: Iterable, size: int) -> Iterator[list]:
    batch: list = []
    for item in seq:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
