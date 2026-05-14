import argparse
import os
import selectors
import socket
import subprocess
import sys
from pathlib import Path

# kqueue (macOS default) cannot monitor pipe fds, but textual-serve spawns
# the app with stdin/stdout as pipes.  SelectSelector works everywhere.
if os.environ.get("TEXTUAL_DRIVER"):
    selectors.DefaultSelector = selectors.SelectSelector  # type: ignore[attr-defined]

from textual.app import App, ComposeResult
from textual.reactive import reactive
from textual.widgets import Header, Footer
from textual.containers import Container, Horizontal, Vertical

from . import config
from .widgets.clock import ClockWidget
from .widgets.weather import WeatherWidget
from .widgets.calendar import CalendarWidget
from .widgets.ghostfolio import GhostfolioWidget
from .widgets.connectivity import ConnectivityWidget
from .widgets.hosts import HostsWidget
from .widgets.rss import RssWidget


class TuidashApp(App):
    """Personal terminal dashboard."""

    TITLE = "tuidash"

    privacy:          reactive[bool] = reactive(False)
    refresh_interval: reactive[int]  = reactive(300, always_update=True)

    CSS = """
    Screen {
        background: $background;
        layers: base overlay;
    }

    /* ── rows ── */
    #row-top {
        height: 28%;
        margin: 0 0 1 0;
    }

    #row-mid {
        height: 44%;
        margin: 0 0 1 0;
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
    }

    ConnectivityWidget {
        width: 100%;
        height: auto;
    }

    HostsWidget {
        width: 100%;
        height: 1fr;
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

    BINDINGS = [
        ("q", "quit",             "Quit"),
        ("r", "refresh",          "Refresh"),
        ("p", "toggle_privacy",   "Privacy"),
        ("[", "decrease_refresh", "-60s"),
        ("]", "increase_refresh", "+60s"),
    ]

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
        self.query_one(ClockWidget).border_title     = "  Clock"
        self.query_one(WeatherWidget).border_title   = "  Weather"
        self.query_one(CalendarWidget).border_title  = "  Calendar"
        self.query_one(GhostfolioWidget).border_title = "  Ghostfolio"
        self.query_one(ConnectivityWidget).border_title = "  Connectivity"
        self.query_one(HostsWidget).border_title     = "  Hosts"
        self.query_one(RssWidget).border_title       = "  News"

        theme_name = config.get("TUIDASH_THEME")
        if theme_name:
            if theme_name in self.available_themes:
                self.theme = theme_name
            else:
                valid = ", ".join(sorted(self.available_themes))
                self.notify(
                    f"Unknown theme {theme_name!r}. Valid themes: {valid}",
                    severity="warning",
                    timeout=8,
                )

        raw = config.get("TUIDASH_REFRESH", "300") or "300"
        try:
            interval = max(30, int(raw))
        except ValueError:
            interval = 300
        self.refresh_interval = interval

    # ── subtitle ──────────────────────────────────────────────────────────────

    def _update_subtitle(self) -> None:
        parts = []
        if self.privacy:
            parts.append("PRIVATE MODE")
        parts.append(f"↻ {self.refresh_interval}s")
        self.sub_title = "  ".join(parts)

    # ── reactives ─────────────────────────────────────────────────────────────

    def watch_privacy(self, value: bool) -> None:
        self._update_subtitle()
        self.query_one(GhostfolioWidget).set_privacy(value)

    def watch_refresh_interval(self, value: int) -> None:
        self._update_subtitle()
        try:
            self.query_one(WeatherWidget).set_refresh_interval(value)
            self.query_one(GhostfolioWidget).set_refresh_interval(value)
            self.query_one(ConnectivityWidget).set_refresh_interval(value)
            self.query_one(HostsWidget).set_refresh_interval(value)
            self.query_one(RssWidget).set_refresh_interval(value)
        except Exception:
            pass

    # ── actions ───────────────────────────────────────────────────────────────

    def action_toggle_privacy(self) -> None:
        self.privacy = not self.privacy

    def action_decrease_refresh(self) -> None:
        self.refresh_interval = max(30, self.refresh_interval - 60)

    def action_increase_refresh(self) -> None:
        self.refresh_interval = min(3600, self.refresh_interval + 60)

    def action_refresh(self) -> None:
        self.notify("Refreshing…", severity="information")
        try:
            self.query_one(WeatherWidget)._load()
            self.query_one(GhostfolioWidget)._load()
            self.query_one(ConnectivityWidget)._load()
            self.query_one(HostsWidget)._load()
            self.query_one(RssWidget)._load()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(prog="tuidash", description="Personal terminal dashboard")
    parser.add_argument("--serve", action="store_true", help="Serve the dashboard over HTTP")
    parser.add_argument("--host", default="0.0.0.0", metavar="HOST", help="Host to bind to (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, metavar="PORT", help="Port to listen on (default: 8080)")
    args = parser.parse_args()

    if args.serve:
        textual_bin = Path(sys.executable).parent / "textual"
        # When binding to 0.0.0.0, the public URL must use the real LAN IP so
        # that remote browsers (tablet, etc.) receive a reachable WebSocket URL.
        public_host = args.host
        if public_host == "0.0.0.0":
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.connect(("8.8.8.8", 80))
                    public_host = s.getsockname()[0]
            except Exception:
                public_host = "localhost"
        public_url = f"http://{public_host}:{args.port}"
        sys.exit(subprocess.run(
            [textual_bin, "serve", "-c", f"{sys.executable} -m tuidash.app",
             "-h", args.host, "-p", str(args.port), "-u", public_url]
        ).returncode)

    TuidashApp().run()


if __name__ == "__main__":
    main()
