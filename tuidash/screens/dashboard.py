from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical

from ..widgets.calendar import CalendarWidget
from ..widgets.clock import ClockWidget
from ..widgets.connectivity import ConnectivityWidget
from ..widgets.events import EventsWidget
from ..widgets.ghostfolio import GhostfolioWidget
from ..widgets.hosts import HostsWidget
from ..widgets.weather import WeatherWidget
from . import BasePage


class DashboardPage(BasePage):
    """Main overview dashboard — page 1."""

    DEFAULT_CSS = """
    DashboardPage {
        height: 100%;
        layers: base overlay;
    }

    /* ── rows ── */
    #row-top {
        height: 28%;
    }

    #row-mid {
        height: auto;
    }

    #row-bot {
        height: 1fr;
    }

    /* ── widget sizing ── */
    ClockWidget {
        width: 30;
        margin: 0 1 0 0;
    }

    WeatherWidget {
        width: 2fr;
        margin: 0 1 0 0;
    }

    CalendarWidget {
        width: 1fr;
    }

    GhostfolioWidget {
        width: 50%;
        margin: 0 1 0 0;
    }

    #conn-hosts-col {
        width: 1fr;
        height: auto;
    }

    ConnectivityWidget {
        width: 100%;
        height: auto;
    }

    HostsWidget {
        width: 100%;
        height: auto;
    }

    EventsWidget {
        width: 100%;
    }

    /* ── widget chrome ── */
    DashWidget {
        border-title-color: $accent;
        border-title-style: bold;
        border-subtitle-color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="row-top"):
            yield ClockWidget()
            yield WeatherWidget()
            yield CalendarWidget()
        with Horizontal(id="row-mid"):
            yield GhostfolioWidget()
            with Vertical(id="conn-hosts-col"):
                yield ConnectivityWidget()
                yield HostsWidget()
        with Container(id="row-bot"):
            yield EventsWidget()

    def on_show(self) -> None:
        self.call_after_refresh(self._sync_scroll)

    def _sync_scroll(self) -> None:
        try:
            self.query_one(EventsWidget).reset_scroll()
            self.query_one(HostsWidget).reset_scroll()
        except Exception:
            pass

    def on_mount(self) -> None:
        self.query_one(ClockWidget).border_title        = "  Clock"
        self.query_one(WeatherWidget).border_title      = "  Weather"
        self.query_one(CalendarWidget).border_title     = "  Calendar"
        self.query_one(GhostfolioWidget).border_title   = "  Ghostfolio"
        self.query_one(ConnectivityWidget).border_title = "  Connectivity"
        self.query_one(HostsWidget).border_title        = "  Servers"
        self.query_one(EventsWidget).border_title       = "  Events"

        self.set_privacy(self.app.privacy)
        self.set_refresh_interval(self.app.refresh_interval)

    def set_privacy(self, value: bool) -> None:
        self.query_one(GhostfolioWidget).set_privacy(value)

    def set_refresh_interval(self, value: int) -> None:
        try:
            self.query_one(WeatherWidget).set_refresh_interval(value)
            self.query_one(CalendarWidget).set_refresh_interval(value)
            self.query_one(GhostfolioWidget).set_refresh_interval(value)
            self.query_one(ConnectivityWidget).set_refresh_interval(value)
            self.query_one(HostsWidget).set_refresh_interval(value)
            self.query_one(EventsWidget).set_refresh_interval(value)
        except Exception:
            pass

    def refresh_all(self) -> None:
        try:
            self.query_one(WeatherWidget)._load()
            self.query_one(CalendarWidget)._fetch()
            self.query_one(GhostfolioWidget)._load()
            self.query_one(ConnectivityWidget)._load()
            self.query_one(HostsWidget)._load()
            self.query_one(EventsWidget)._load()
        except Exception:
            pass
