from __future__ import annotations

from calendar import monthrange
from datetime import date
from typing import Any

from rich.align import Align
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.timer import Timer
from textual.widgets import Static
from textual import work

from .. import config, ics
from .base import DashWidget


def _render_month(
    today: date,
    holidays: frozenset[date],
    family_dates: frozenset[date] = frozenset(),
    family_color: str = "",
    personal_dates: frozenset[date] = frozenset(),
    personal_color: str = "",
    work_dates: frozenset[date] = frozenset(),
    work_color: str = "",
) -> Align:
    year, month = today.year, today.month
    _, num_days = monthrange(year, month)
    first_wd = date(year, month, 1).weekday()  # 0=Mon … 6=Sun

    # week-number col (5) + 7 day cols (3 each) = 26 chars total
    grid = Table.grid(padding=(0, 0))
    grid.add_column(width=5, justify="left")
    for _ in range(7):
        grid.add_column(width=3, justify="right")

    _wd = "bold white on color(24)"   # weekday pill: dark blue
    _we = "bold white on color(88)"   # weekend pill: dark red
    grid.add_row(
        Text("     "),
        Text(" M ", style=_wd), Text(" T ", style=_wd), Text(" W ", style=_wd),
        Text(" T ", style=_wd), Text(" F ", style=_wd),
        Text(" S ", style=_we), Text(" S ", style=_we),
    )

    cells: list[Text] = [Text("   ")] * first_wd

    for day_n in range(1, num_days + 1):
        d = date(year, month, day_n)
        is_today    = d == today
        is_holiday  = d in holidays
        is_personal = d in personal_dates
        is_family   = d in family_dates
        is_work     = d in work_dates
        is_weekend  = d.weekday() >= 5

        if is_today:
            if is_holiday:
                style = "bold red reverse"
            elif is_family and family_color:
                style = f"bold {family_color} reverse"
            elif is_personal and personal_color:
                style = f"bold {personal_color} reverse"
            elif is_work and work_color:
                style = f"bold {work_color} reverse"
            else:
                style = "bold reverse"
        elif is_holiday:
            style = "bold red"
        elif is_family and family_color:
            style = f"bold {family_color}"
        elif is_personal and personal_color:
            style = f"bold {personal_color}"
        elif is_work and work_color:
            style = f"bold {work_color}"
        elif is_weekend:
            style = "bright_black"
        else:
            style = ""

        cells.append(Text(str(day_n), style=style, justify="right"))

    while len(cells) % 7:
        cells.append(Text("   "))

    for i in range(0, len(cells), 7):
        first_valid = max(i, first_wd)
        if first_valid < first_wd + num_days:
            wn = Text()
            wn.append(
                f"[{date(year, month, first_valid - first_wd + 1).isocalendar()[1]:02d}]",
                style="dim",
            )
            wn.append(" ")
        else:
            wn = Text("     ")
        grid.add_row(wn, *cells[i : i + 7])

    return Align.center(grid)


class CalendarWidget(DashWidget):
    """Monthly calendar with weekend, public-holiday, and family-event highlighting."""

    DEFAULT_CSS = """
    CalendarWidget { height: auto; }
    #cal-body { height: auto; content-align: center top; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._holidays: frozenset[date]      = frozenset()
        self._family_dates: frozenset[date]  = frozenset()
        self._family_color: str              = ""
        self._personal_dates: frozenset[date] = frozenset()
        self._personal_color: str             = ""
        self._work_dates: frozenset[date]     = frozenset()
        self._work_color: str                 = ""
        self._url: str | None                 = None
        self._family_url: str | None          = None
        self._personal_url: str | None        = None
        self._work_url: str | None            = None
        self._fetch_timer: Timer | None           = None
        self._family_fetch_timer: Timer | None    = None
        self._personal_fetch_timer: Timer | None  = None
        self._work_fetch_timer: Timer | None      = None

    def compose(self) -> ComposeResult:
        yield Static("", id="cal-body")

    def on_mount(self) -> None:
        self._url = config.get("TUIDASH_HOLIDAY_CALENDAR")
        if self._url:
            self._fetch()
            self._fetch_timer = self.set_interval(3600.0, self._fetch)

        self._family_url   = config.get("TUIDASH_FAMILY_ICS")
        self._family_color = config.get("TUIDASH_FAMILY_COLOR") or ""
        if self._family_url:
            self._fetch_family()

        self._personal_url   = config.get("TUIDASH_PERSONAL_ICS")
        self._personal_color = config.get("TUIDASH_PERSONAL_COLOR") or ""
        if self._personal_url:
            self._fetch_personal()

        self._work_url   = config.get("TUIDASH_WORK_ICS")
        self._work_color = config.get("TUIDASH_WORK_COLOR") or ""
        if self._work_url:
            self._fetch_work()

        self._update_calendar()
        self.set_interval(60.0, self._update_calendar)

    def set_refresh_interval(self, seconds: int) -> None:
        if self._fetch_timer is not None:
            self._fetch_timer.stop()
        if self._url:
            self._fetch_timer = self.set_interval(float(seconds), self._fetch)

        if self._family_fetch_timer is not None:
            self._family_fetch_timer.stop()
        if self._family_url:
            self._family_fetch_timer = self.set_interval(float(seconds), self._fetch_family)

        if self._personal_fetch_timer is not None:
            self._personal_fetch_timer.stop()
        if self._personal_url:
            self._personal_fetch_timer = self.set_interval(float(seconds), self._fetch_personal)

        if self._work_fetch_timer is not None:
            self._work_fetch_timer.stop()
        if self._work_url:
            self._work_fetch_timer = self.set_interval(float(seconds), self._fetch_work)

    @work(thread=True)
    def _fetch(self) -> None:
        try:
            holidays = ics.holiday_dates(self._url)
            self.app.call_from_thread(self._on_holidays, holidays)
        except Exception:
            pass  # network or parse error — keep existing holidays

    @work(thread=True)
    def _fetch_family(self) -> None:
        try:
            dates = ics.holiday_dates(self._family_url)
            self.app.call_from_thread(self._on_family_dates, dates)
        except Exception:
            pass

    def _on_holidays(self, holidays: frozenset[date]) -> None:
        self._holidays = holidays
        self._update_calendar()

    @work(thread=True)
    def _fetch_personal(self) -> None:
        try:
            dates = ics.holiday_dates(self._personal_url)
            self.app.call_from_thread(self._on_personal_dates, dates)
        except Exception:
            pass

    def _on_family_dates(self, dates: frozenset[date]) -> None:
        self._family_dates = dates
        self._update_calendar()

    @work(thread=True)
    def _fetch_work(self) -> None:
        try:
            dates = ics.holiday_dates(self._work_url)
            self.app.call_from_thread(self._on_work_dates, dates)
        except Exception:
            pass

    def _on_personal_dates(self, dates: frozenset[date]) -> None:
        self._personal_dates = dates
        self._update_calendar()

    def _on_work_dates(self, dates: frozenset[date]) -> None:
        self._work_dates = dates
        self._update_calendar()

    def _update_calendar(self) -> None:
        today = date.today()
        self.query_one("#cal-body", Static).update(
            _render_month(today, self._holidays, self._family_dates, self._family_color,
                          self._personal_dates, self._personal_color,
                          self._work_dates, self._work_color)
        )
        self.border_subtitle = today.strftime("%B %Y")
