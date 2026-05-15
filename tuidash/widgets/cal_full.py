from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta, time as dt_time
from typing import Any

from rich import box as rich_box
from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.timer import Timer
from textual.widgets import Static
from textual import work

from .. import config, ics
from ..scroll import SCROLL_INTERVAL, current_tick, scroll_window
from .base import DashWidget


@dataclass
class _Event:
    summary: str
    color: str       # "" if no color configured
    priority: int    # 0=holiday, 1=family, 2=personal, 3=work
    start_time: dt_time | None = None


def _day_style(d: date, today: date, events: list[_Event]) -> str:
    is_weekend = d.weekday() >= 5
    top = next((e for e in events if e.color), None)
    if top:
        return f"bold {top.color} reverse" if d == today else f"bold {top.color}"
    if d == today:
        return "bold reverse"
    if is_weekend:
        return "bright_black"
    return ""


def _make_day_cell(
    d: date,
    today: date,
    events: list[_Event],
    cell_height: int,
    cell_width: int,
    in_month: bool = True,
    tick: int = 0,
) -> Text:
    t = Text()
    if in_month:
        t.append(f"{d.day:>{cell_width}}", style=_day_style(d, today, events))
    else:
        t.append(f"{d.day:>{cell_width}}", style="bright_black")

    max_ev = cell_height - 1
    shown = min(len(events), max_ev)
    for ev_idx, ev in enumerate(events[:max_ev]):
        time_pfx = f"{ev.start_time.hour}:{ev.start_time.minute:02d} " if ev.start_time else ""
        avail = max(1, cell_width - len(time_pfx))
        phase = (d.day * 37 + ev_idx * 13) % 200
        name = scroll_window(ev.summary, avail, tick, phase)
        t.append("\n")
        if in_month:
            ev_style = f"dim {ev.color}" if ev.color else "dim"
            if time_pfx:
                t.append(time_pfx, style="dim bright_black")
            t.append(name, style=ev_style)
        else:
            t.append(time_pfx + name, style="bright_black")

    t.append("\n" * (cell_height - 1 - shown))
    return t


_WE_BG = "on color(237)"  # subtle dark-gray tint for Sat/Sun columns


def _shift_month(d: date, offset: int) -> date:
    """Return the first day of the month `offset` months from `d`."""
    total = d.year * 12 + (d.month - 1) + offset
    year, m = divmod(total, 12)
    return date(year, m + 1, 1)


def _render_full_month(
    display_date: date,
    today: date,
    events_by_date: dict[date, list[_Event]],
    content_width: int,
    content_height: int,
    tick: int = 0,
) -> Group:
    year, month = display_date.year, display_date.month
    _, num_days = monthrange(year, month)
    first_wd = date(year, month, 1).weekday()

    num_weeks = (first_wd + num_days + 6) // 7

    # Layout constants
    WN_WIDTH = 4
    PADDING  = 1   # chars per side
    # Total non-content cols: WN + 8 cols × 2-pad + 7 dividers (│)
    day_col_w = max(6, (content_width - WN_WIDTH - 8 * PADDING * 2 - 7) // 7)
    # Fixed lines: 1 header row + 1 header-sep + (num_weeks-1) row-seps + 1 closing line = num_weeks + 2
    avail_h  = content_height - num_weeks - 2
    ch_base  = max(2, avail_h // num_weeks)
    ch_extra = max(0, avail_h % num_weeks)  # first ch_extra rows get ch_base+1

    grid = Table(
        box=rich_box.SQUARE,
        show_edge=False,
        show_header=True,
        show_lines=True,
        expand=True,
        padding=(0, PADDING),
    )
    grid.add_column("", width=WN_WIDTH, no_wrap=True)
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for i, name in enumerate(day_names):
        is_we = i >= 5
        grid.add_column(
            name,
            ratio=1,
            no_wrap=True,
            style=_WE_BG if is_we else "",
            header_style=f"bold bright_black {_WE_BG}" if is_we else "bold",
        )

    # Build day cells — pad with real adjacent-month dates (rendered in gray)
    month_start = date(year, month, 1)
    cells: list[date] = [month_start - timedelta(days=first_wd - i) for i in range(first_wd)]
    for day_n in range(1, num_days + 1):
        cells.append(date(year, month, day_n))
    while len(cells) % 7:
        cells.append(cells[-1] + timedelta(days=1))

    for wi_idx, wi in enumerate(range(0, len(cells), 7)):
        ch   = ch_base + (1 if wi_idx < ch_extra else 0)
        week = cells[wi : wi + 7]
        first_curr = next((d for d in week if d.month == month), None)

        wn = Text()
        if first_curr:
            wn.append(f"W{first_curr.isocalendar()[1]:02d}", style="dim")
        wn.append("\n" * (ch - 1))

        row_cells = [wn] + [
            _make_day_cell(d, today, events_by_date.get(d, []), ch, day_col_w, d.month == month, tick)
            for d in week
        ]
        grid.add_row(*row_cells)

    # Build a closing line that exactly mirrors the internal separator rows:
    # WN segment of ─, then (┴ + day segment) × 7.
    # Rich distributes ratio=1 columns as: base = avail//7, first (avail%7)
    # columns get base+1.  We replicate that to keep ┴ markers aligned.
    wn_w   = WN_WIDTH + 2 * PADDING           # = 6
    avail  = content_width - wn_w - 7         # 7 × divider chars (┴)
    base   = avail // 7
    extra  = avail % 7
    closing = Text()
    closing.append("─" * wn_w)
    for i in range(7):
        closing.append("┴")
        closing.append("─" * (base + (1 if i < extra else 0)))

    return Group(grid, closing)


class CalFullWidget(DashWidget):
    """Full-page monthly calendar with event names from all ICS sources."""

    can_focus = True

    DEFAULT_CSS = """
    CalFullWidget { height: 100%; }
    #calfull-body { height: 100%; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._events_by_date: dict[date, list[_Event]] = {}
        self._holiday_url: str | None = None
        self._family_url: str | None = None
        self._personal_url: str | None = None
        self._work_url: str | None = None
        self._family_color: str = ""
        self._personal_color: str = ""
        self._work_color: str = ""
        self._data_timer: Timer | None = None
        self._tick: int = 0
        self._scroll_epoch: int = 0
        self._month_offset: int = 0

    def compose(self) -> ComposeResult:
        yield Static("", id="calfull-body")

    def on_mount(self) -> None:
        self._holiday_url  = config.get("TUIDASH_HOLIDAY_CALENDAR")
        self._family_url   = config.get("TUIDASH_FAMILY_ICS")
        self._family_color = config.get("TUIDASH_FAMILY_COLOR") or ""
        self._personal_url   = config.get("TUIDASH_PERSONAL_ICS")
        self._personal_color = config.get("TUIDASH_PERSONAL_COLOR") or ""
        self._work_url   = config.get("TUIDASH_WORK_ICS")
        self._work_color = config.get("TUIDASH_WORK_COLOR") or ""
        self._load()
        self._data_timer = self.set_interval(3600.0, self._load)
        self.set_interval(60.0, self._redraw)  # re-render for today highlight at midnight
        self.set_interval(SCROLL_INTERVAL, self._advance_scroll)

    def on_show(self) -> None:
        self.call_after_refresh(self.reset_scroll)

    def on_resize(self) -> None:
        self._redraw()

    def prev_month(self) -> None:
        self._month_offset -= 1
        self.reset_scroll()

    def next_month(self) -> None:
        self._month_offset += 1
        self.reset_scroll()

    def _advance_scroll(self) -> None:
        self._tick = current_tick() - self._scroll_epoch
        self._redraw()

    def reset_scroll(self) -> None:
        self._scroll_epoch = current_tick()
        self._tick = 0
        self._redraw()

    def set_refresh_interval(self, seconds: int) -> None:
        if self._data_timer is not None:
            self._data_timer.stop()
        self._data_timer = self.set_interval(float(seconds), self._load)

    @work(thread=True)
    def _load(self) -> None:
        sources = [
            (self._holiday_url,  "red",                 0),
            (self._family_url,   self._family_color,    1),
            (self._work_url,     self._work_color,      2),
            (self._personal_url, self._personal_color,  3),
        ]

        def _fetch(args: tuple) -> list[tuple[date, _Event]]:
            url, color, priority = args
            if not url:
                return []
            try:
                result = []
                for ev in ics.fetch_events(url):
                    end = ev.end_date or ev.date
                    d = ev.date
                    while d <= end:
                        st = ev.start_time if d == ev.date else None
                        result.append((d, _Event(ev.summary, color, priority, st)))
                        d += timedelta(days=1)
                return result
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=4) as pool:
            all_results = [f.result() for f in [pool.submit(_fetch, s) for s in sources]]

        events_by_date: dict[date, list[_Event]] = defaultdict(list)
        for results in all_results:
            for d, ev in results:
                events_by_date[d].append(ev)
        for d in events_by_date:
            events_by_date[d].sort(key=lambda e: (e.priority, e.start_time is not None, e.start_time or dt_time.min))

        self.app.call_from_thread(self._show_data, dict(events_by_date))

    def _show_data(self, events_by_date: dict[date, list[_Event]]) -> None:
        self._events_by_date = events_by_date
        self._redraw()

    def _redraw(self) -> None:
        today   = date.today()
        display = _shift_month(today, self._month_offset)
        w = self.content_size.width or 120
        h = self.content_size.height or 40
        self.query_one("#calfull-body", Static).update(
            _render_full_month(display, today, self._events_by_date, w, h, self._tick)
        )
        self.border_subtitle = display.strftime("%B %Y")
