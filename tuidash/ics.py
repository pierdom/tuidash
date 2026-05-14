from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, datetime

import requests


@dataclass(frozen=True)
class CalEvent:
    date: date
    summary: str


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


def _parse(text: str) -> list[CalEvent]:
    text = _unfold(text)
    events: list[CalEvent] = []
    for m in re.finditer(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.DOTALL):
        block = m.group(1)
        ds = re.search(r"^DTSTART[^:]*:(\S+)", block, re.MULTILINE)
        sm = re.search(r"^SUMMARY:(.+)", block, re.MULTILINE)
        if ds:
            d = _parse_date(ds.group(1))
            if d:
                events.append(CalEvent(
                    date=d,
                    summary=sm.group(1).strip() if sm else "",
                ))
    return events


def fetch_events(url: str) -> list[CalEvent]:
    """Fetch and parse an ICS URL, returning all events. Results cached for 1 hour."""
    now = time.monotonic()
    if url in _cache:
        ts, cached = _cache[url]
        if now - ts < _TTL:
            return cached
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    events = _parse(resp.text)
    _cache[url] = (now, events)
    return events


def holiday_dates(url: str) -> frozenset[date]:
    """Return the set of holiday dates from an ICS calendar URL."""
    return frozenset(e.date for e in fetch_events(url))
