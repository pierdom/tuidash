from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding

from . import BasePage
from ..widgets.cal_full import CalFullWidget


class CalendarPage(BasePage):
    """Full-page monthly calendar with events — page 3."""

    BINDINGS = [
        Binding("comma",      "prev_month", "Prev month"),
        Binding("full_stop",  "next_month", "Next month"),
    ]

    DEFAULT_CSS = """
    CalendarPage {
        height: 100%;
    }
    CalFullWidget {
        height: 100%;
        border-title-color: $accent;
        border-title-style: bold;
        border-subtitle-color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        yield CalFullWidget()

    def on_mount(self) -> None:
        self.query_one(CalFullWidget).border_title = "  Calendar"

    def on_show(self) -> None:
        self.query_one(CalFullWidget).focus()

    def set_refresh_interval(self, value: int) -> None:
        try:
            self.query_one(CalFullWidget).set_refresh_interval(value)
        except Exception:
            pass

    def action_prev_month(self) -> None:
        try:
            self.query_one(CalFullWidget).prev_month()
        except Exception:
            pass

    def action_next_month(self) -> None:
        try:
            self.query_one(CalFullWidget).next_month()
        except Exception:
            pass

    def refresh_all(self) -> None:
        try:
            self.query_one(CalFullWidget)._load()
        except Exception:
            pass
