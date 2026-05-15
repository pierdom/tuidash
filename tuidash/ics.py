from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta

import requests


@dataclass(frozen=True)
class CalEvent:
    date: date
    summary: str
    start_time: dt_time | None = None
    end_date: date | None = None  # inclusive last day; None means same as date
    end_time: dt_time | None = None


_cache: dict[str, tuple[float, list[CalEvent]]] = {}
_TTL = 3600.0  # 1 hour


def _unfold(text: str) -> str:
    """Remove RFC 5545 line folding (continuation lines start with a space or tab)."""
    return re.sub(r"\r?\n[ \t]", "", text)


def _parse_date(value: str) -> date | None:
    bare = value.split("T")[0].rstrip("Z")
    try:
        return datetime.strptime(bare, "%Y%m%d").date()
    except ValueError:
        return None


def _parse_time(value: str) -> dt_time | None:
    if "T" not in value:
        return None
    t_part = value.split("T")[1].rstrip("Z")
    try:
        return datetime.strptime(t_part[:6], "%H%M%S").time()
    except ValueError:
        return None


def _parse(text: str) -> list[CalEvent]:
    text = _unfold(text)
    events: list[CalEvent] = []
    for m in re.finditer(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.DOTALL):
        block = m.group(1)
        ds = re.search(r"^DTSTART[^:]*:(\S+)", block, re.MULTILINE)
        de = re.search(r"^DTEND[^:]*:(\S+)",   block, re.MULTILINE)
        sm = re.search(r"^SUMMARY:(.+)",        block, re.MULTILINE)
        if not ds:
            continue
        raw_start = ds.group(1)
        d = _parse_date(raw_start)
        if not d:
            continue
        end_date: date | None = None
        end_time: dt_time | None = None
        if de:
            raw_end = de.group(1)
            parsed_end = _parse_date(raw_end)
            end_time = _parse_time(raw_end)
            if parsed_end:
                if "T" not in raw_end:
                    # All-day DTEND is exclusive per RFC 5545
                    parsed_end = parsed_end - timedelta(days=1)
                if parsed_end > d:
                    end_date = parsed_end
        events.append(CalEvent(
            date=d,
            summary=sm.group(1).strip() if sm else "",
            start_time=_parse_time(raw_start),
            end_date=end_date,
            end_time=end_time,
        ))
    return events


def fetch_events(url: str) -> list[CalEvent]:
    """Fetch and parse an ICS URL, returning all events. Results cached for 1 hour."""
    url = url.replace("webcal://", "https://", 1).replace("webcal+https://", "https://", 1)
    now = time.monotonic()
    if url in _cache:
        ts, cached = _cache[url]
        if now - ts < _TTL:
            return cached
    resp = requests.get(url, timeout=15, headers={"User-Agent": "tuidash/1.0"})
    resp.raise_for_status()
    events = _parse(resp.text)
    _cache[url] = (now, events)
    return events


def holiday_dates(url: str) -> frozenset[date]:
    """Return the set of holiday dates from an ICS calendar URL."""
    return frozenset(e.date for e in fetch_events(url))
