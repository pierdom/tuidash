from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from datetime import time as dt_time
from typing import Any

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static
from textual import work

from .. import config, ics
from ..scroll import SCROLL_INTERVAL, current_tick, scroll_window
from .base import DashWidget


@dataclass
class _Source:
    color: str
    priority: int = 0
    events: list[ics.CalEvent] = field(default_factory=list)


@dataclass
class _Slot:
    day: date
    events: list[tuple[str, ics.CalEvent]]
    is_overflow: bool = False


def _events_for_day(sources: list[_Source], day: date) -> list[tuple[str, ics.CalEvent]]:
    result: list[tuple[int, str, ics.CalEvent]] = []
    for src in sources:
        for ev in src.events:
            end = ev.end_date or ev.date
            if ev.date <= day <= end:
                result.append((src.priority, src.color, ev))
    result.sort(key=lambda t: (t[2].start_time is not None, t[2].start_time or dt_time.min, t[0]))
    return [(color, ev) for _, color, ev in result]


def _build_slots(
    sources: list[_Source],
    days: list[date],
    avail_h: int,
) -> list[_Slot]:
    """Distribute days into at most 4 column slots, overflowing busy days into extra slots."""
    slots: list[_Slot] = []
    chunk_size = max(1, avail_h)
    for day in days:
        if len(slots) >= 4:
            break
        pairs = _events_for_day(sources, day)
        if not pairs:
            slots.append(_Slot(day=day, events=[]))
            continue
        for chunk_start in range(0, len(pairs), chunk_size):
            if len(slots) >= 4:
                break
            slots.append(_Slot(
                day=day,
                events=pairs[chunk_start:chunk_start + chunk_size],
                is_overflow=(chunk_start > 0),
            ))
    return slots


def _render_events_mobile(
    day_events: list[tuple[date, list[tuple[str, ics.CalEvent]]]],
    today: date,
    col_w: int,
    tick: int,
) -> Text:
    """Vertical layout: each day as a labelled block, stacked top-to-bottom."""
    t = Text()
    for di, (day, pairs) in enumerate(day_events):
        if di > 0:
            t.append("\n")
            t.append("─" * col_w, style="dim")
            t.append("\n")
        delta = (day - today).days
        if delta == 0:
            label = "Today"
        elif delta == 1:
            label = "Tomorrow"
        else:
            label = day.strftime("%A")
        t.append(label, style="bold")
        t.append("  " + day.strftime("%d %b"), style="dim")

        if not pairs:
            t.append("\n")
            t.append("—", style="dim")
            continue

        all_day = [(c, ev) for c, ev in pairs if ev.start_time is None]
        timed   = [(c, ev) for c, ev in pairs if ev.start_time is not None]

        for ei, (color, ev) in enumerate(all_day):
            prefix = "● "
            available = max(3, col_w - len(prefix))
            phase = (di * 31 + ei * 17) % 60
            t.append("\n")
            t.append(prefix, style="dim")
            t.append(scroll_window(ev.summary, available, tick, phase), style=color)

        for ei, (color, ev) in enumerate(timed):
            t_str = ev.start_time.strftime("%H:%M")  # type: ignore[union-attr]
            if ev.end_time:
                t_str += "–" + ev.end_time.strftime("%H:%M")
            prefix = t_str + " "
            available = max(3, col_w - len(prefix))
            phase = (di * 31 + (len(all_day) + ei) * 17) % 60
            t.append("\n")
            t.append(prefix, style="dim")
            t.append(scroll_window(ev.summary, available, tick, phase), style=color)

    return t


def _render_events(
    slots: list[_Slot],
    today: date,
    col_w: int,
    tick: int,
) -> Table:
    t = Table.grid(expand=True, padding=(0, 1))
    for _ in slots:
        t.add_column(ratio=1)

    # Header row
    headers: list[Text] = []
    for slot in slots:
        if slot.is_overflow:
            h = Text()
            h.append("  ↳", style="dim")
            headers.append(h)
        else:
            day = slot.day
            delta = (day - today).days
            if delta == 0:
                label = "Today"
            elif delta == 1:
                label = "Tomorrow"
            else:
                label = day.strftime("%A")
            h = Text()
            h.append(label, style="bold")
            h.append("  " + day.strftime("%d %b"), style="dim")
            headers.append(h)
    t.add_row(*headers)

    # Content column per slot
    col_texts: list[Text] = []
    for ci, slot in enumerate(slots):
        pairs = slot.events
        col = Text()

        if not pairs:
            col.append("—", style="dim")
            col_texts.append(col)
            continue

        all_day = [(c, ev) for c, ev in pairs if ev.start_time is None]
        timed   = [(c, ev) for c, ev in pairs if ev.start_time is not None]

        first = True
        for ei, (color, ev) in enumerate(all_day):
            if not first:
                col.append("\n")
            first = False
            prefix = "● "
            available = max(3, col_w - len(prefix))
            phase = (ci * 31 + ei * 17) % 60
            col.append(prefix, style="dim")
            col.append(scroll_window(ev.summary, available, tick, phase), style=color)

        for ei, (color, ev) in enumerate(timed):
            if not first:
                col.append("\n")
            first = False
            t_str = ev.start_time.strftime("%H:%M")  # type: ignore[union-attr]
            if ev.end_time:
                t_str += "–" + ev.end_time.strftime("%H:%M")
            prefix = t_str + " "
            available = max(3, col_w - len(prefix))
            phase = (ci * 31 + (len(all_day) + ei) * 17) % 60
            col.append(prefix, style="dim")
            col.append(scroll_window(ev.summary, available, tick, phase), style=color)

        col_texts.append(col)

    t.add_row(*col_texts)
    return t


class EventsWidget(DashWidget):
    """6-day calendar event view across all configured ICS sources (4-column layout)."""

    data: reactive[list[_Source] | None] = reactive(None, always_update=True)

    DEFAULT_CSS = """
    EventsWidget { height: 100%; }
    #events-body { height: 100%; padding: 0 1; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._sources: list[tuple[str, str, int]] = []  # (url, color, priority)
        self._data_timer:   Timer | None = None
        self._scroll_timer: Timer | None = None
        self._tick: int = 0
        self._scroll_epoch: int = 0

    def compose(self) -> ComposeResult:
        yield Static("[dim]Loading…[/dim]", id="events-body")

    def on_mount(self) -> None:
        holiday_url  = config.get("TUIDASH_HOLIDAY_CALENDAR")
        family_url   = config.get("TUIDASH_FAMILY_ICS")
        personal_url = config.get("TUIDASH_PERSONAL_ICS")
        work_url     = config.get("TUIDASH_WORK_ICS")

        family_color   = config.get("TUIDASH_FAMILY_COLOR")   or "yellow"
        personal_color = config.get("TUIDASH_PERSONAL_COLOR") or "teal"
        work_color     = config.get("TUIDASH_WORK_COLOR")     or "green"

        if holiday_url:
            self._sources.append((holiday_url, "red", 0))
        if family_url:
            self._sources.append((family_url, family_color, 1))
        if work_url:
            self._sources.append((work_url, work_color, 2))
        if personal_url:
            self._sources.append((personal_url, personal_color, 3))

        if not self._sources:
            self.query_one("#events-body", Static).update(
                "[dim]No calendars configured[/dim]"
            )
            return

        self._load()
        self._scroll_timer = self.set_interval(SCROLL_INTERVAL, self._advance_scroll)

    def _avail_h(self) -> int:
        return max(1, (self.content_size.height or 10) - 1)

    def _col_w(self, n_cols: int) -> int:
        total = self.content_size.width or 80
        return max(10, (total - n_cols * 2) // n_cols)

    def _advance_scroll(self) -> None:
        self._tick = current_tick() - self._scroll_epoch
        if self.data is not None:
            self._redraw()

    def reset_scroll(self) -> None:
        self._scroll_epoch = current_tick()
        self._tick = 0
        if self.data is not None:
            self._redraw()

    def _redraw(self) -> None:
        if self.data is None:
            return
        today = date.today()
        days = [today + timedelta(days=i) for i in range(6)]

        mobile = self.screen.has_class("mobile")
        if mobile:
            col_w = max(10, (self.content_size.width or 60) - 4)
            day_events = [(d, _events_for_day(self.data, d)) for d in days]
            renderable = _render_events_mobile(day_events, today, col_w, self._tick)
        else:
            slots = _build_slots(self.data, days, self._avail_h())
            renderable = _render_events(slots, today, self._col_w(len(slots)), self._tick)

        self.query_one("#events-body", Static).update(renderable)
        self.border_subtitle = f"{days[0].strftime('%a %d %b')} – {days[-1].strftime('%a %d %b')}"

    def set_refresh_interval(self, seconds: int) -> None:
        if self._data_timer is not None:
            self._data_timer.stop()
        self._data_timer = self.set_interval(float(seconds), self._load)

    @work(thread=True)
    def _load(self) -> None:
        if not self._sources:
            return
        results = [_Source(color=color, priority=prio) for _, color, prio in self._sources]
        with ThreadPoolExecutor(max_workers=len(self._sources)) as pool:
            future_to_idx = {
                pool.submit(ics.fetch_events, url): i
                for i, (url, _color, _prio) in enumerate(self._sources)
            }
        for f, idx in future_to_idx.items():
            try:
                results[idx].events = f.result()
            except Exception:
                pass
        self.app.call_from_thread(self._show_data, results)

    def _show_data(self, sources: list[_Source]) -> None:
        self.data = sources

    def watch_data(self, sources: list[_Source] | None) -> None:
        if sources is None:
            return
        self._redraw()

    def pause_animations(self) -> None:
        if self._scroll_timer is not None:
            self._scroll_timer.pause()

    def resume_animations(self) -> None:
        if self._scroll_timer is not None:
            self._scroll_timer.resume()
