from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header

from ..widgets.calendar import CalendarWidget
from ..widgets.clock import ClockWidget
from ..widgets.connectivity import ConnectivityWidget
from ..widgets.ghostfolio import GhostfolioWidget
from ..widgets.hosts import HostsWidget
from ..widgets.rss import RssWidget
from ..widgets.weather import WeatherWidget


class DashboardScreen(Screen):
    """Main overview dashboard — page 1."""

    CSS = """
    Screen {
        background: $background;
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

    RssWidget {
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
        yield Header(show_clock=True)
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
            yield RssWidget()
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(ClockWidget).border_title        = "  Clock"
        self.query_one(WeatherWidget).border_title      = "  Weather"
        self.query_one(CalendarWidget).border_title     = "  Calendar"
        self.query_one(GhostfolioWidget).border_title   = "  Ghostfolio"
        self.query_one(ConnectivityWidget).border_title = "  Connectivity"
        self.query_one(HostsWidget).border_title        = "  Servers"
        self.query_one(RssWidget).border_title          = "  News"

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
            self.query_one(RssWidget).set_refresh_interval(value)
        except Exception:
            pass

    def refresh_all(self) -> None:
        try:
            self.query_one(WeatherWidget)._load()
            self.query_one(CalendarWidget)._fetch()
            self.query_one(GhostfolioWidget)._load()
            self.query_one(ConnectivityWidget)._load()
            self.query_one(HostsWidget)._load()
            self.query_one(RssWidget)._load()
        except Exception:
            pass
