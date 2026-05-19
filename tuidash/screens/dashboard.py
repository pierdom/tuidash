from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, ScrollableContainer, Vertical

from ..widgets.calendar import CalendarWidget
from ..widgets.clock import ClockWidget
from ..widgets.connectivity import ConnectivityWidget
from ..widgets.events import EventsWidget
from ..widgets.ghostfolio import GhostfolioWidget
from ..widgets.hosts import HostsWidget
from ..widgets.news_ticker import NewsTickerWidget
from ..widgets.weather import WeatherWidget
from . import BasePage


class DashboardPage(BasePage):
    """Main overview dashboard — page 1."""

    DEFAULT_CSS = """
    DashboardPage {
        height: 100%;
        layers: base overlay;
    }

    /* ── scrollbar size applied at parse time (bypasses .mobile CSS timing) ── */
    #dashboard-scroll {
        scrollbar-size-vertical: 1;
    }

    /* ── rows ── */
    #row-top {
        height: auto;
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
    """

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="dashboard-scroll") as sc:
            sc.can_focus = False
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
        yield NewsTickerWidget()

    def on_show(self) -> None:
        self.call_after_refresh(self._sync_scroll)

    def on_resize(self) -> None:
        self._sync_scroll_mode()

    def _sync_scroll_mode(self) -> None:
        try:
            sc = self.query_one("#dashboard-scroll", ScrollableContainer)
        except Exception:
            return
        if self.screen.has_class("mobile"):
            sc.styles.overflow_y = "scroll"
            sc.show_vertical_scrollbar = True
        else:
            sc.styles.overflow_y = "hidden"
            sc.show_vertical_scrollbar = False

    def _sync_scroll(self) -> None:
        self._sync_scroll_mode()
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
        self.query_one(EventsWidget).border_title        = "  Events"
        self.query_one(NewsTickerWidget).border_title   = "  News"

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
            self.query_one(NewsTickerWidget).set_refresh_interval(value)
        except Exception:
            pass

    def refresh_all(self) -> None:
        try:
            self.query_one(WeatherWidget)._load()
            self.query_one(CalendarWidget)._load()
            self.query_one(GhostfolioWidget)._load()
            self.query_one(ConnectivityWidget)._load()
            self.query_one(HostsWidget)._load()
            self.query_one(EventsWidget)._load()
            self.query_one(NewsTickerWidget)._load()
        except Exception:
            pass
