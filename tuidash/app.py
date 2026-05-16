from __future__ import annotations

import argparse
import os
import re
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
from textual.binding import Binding
from textual.reactive import reactive
from textual.widgets import ContentSwitcher, Footer

from . import config
from .screens import BasePage
from .screens.dashboard import DashboardPage
from .screens.calendar import CalendarPage
from .screens.news import NewsPage
from .screens.podcasts import PodcastsPage
from .widgets.net_header import DashHeader


# Ordered list of pages: (label, widget-id, class).
# Add a new entry here to register a new page in the carousel.
_PAGES: list[tuple[str, str, type]] = [
    ("Dashboard", "page-dashboard",  DashboardPage),
    ("News",      "page-news",       NewsPage),
    ("Calendar",  "page-calendar",   CalendarPage),
    ("Podcasts",  "page-podcasts",   PodcastsPage),
]


class TuidashApp(App):
    """Personal terminal dashboard."""

    TITLE = "tuidash"

    CSS = """
    Screen {
        background: $background;
        layers: base overlay;
    }
    ContentSwitcher {
        height: 1fr;
    }
    """

    privacy:          reactive[bool] = reactive(False)
    refresh_interval: reactive[int]  = reactive(300, always_update=True)
    _privacy_forced:  bool           = False
    _page_idx:        int            = 0

    BINDINGS = [
        ("q",                "quit",             "Quit"),
        ("r",                "refresh",          "Refresh"),
        ("p",                "toggle_privacy",   "Privacy"),
        ("[",                "decrease_refresh", "-60s"),
        ("]",                "increase_refresh", "+60s"),
        Binding("left",      "prev_page",        "Prev page",  priority=True),
        Binding("right",     "next_page",        "Next page",  priority=True),
        Binding("space",     "toggle_playback",  "⏯ Play/Pause", priority=True),
        Binding("comma",     "prev_month",       "Prev month", priority=True),
        Binding("full_stop", "next_month",       "Next month", priority=True),
        *[
            Binding(str(i + 1), f"go_page({i})", f"Page {i + 1}", show=False)
            for i in range(len(_PAGES))
        ],
    ]

    # Add "mobile" class to Screen when terminal width < 90 columns.
    # Textual handles both startup and subsequent resizes automatically.
    HORIZONTAL_BREAKPOINTS = [(0, "mobile"), (90, "wide")]

    async def _shutdown(self) -> None:
        # Terminal is already restored by _process_messages → driver.stop_application_mode()
        # before this method is called. Skip the slow widget-pump drain and exit now.
        # We must close the driver (joins the writer thread) so its queued escape sequences
        # are flushed before os._exit kills the process.
        try:
            from .widgets.podcasts import player as _podcast_player
            _podcast_player.stop()
        except Exception:
            pass
        if self._driver is not None:
            try:
                self._driver.close()
            except Exception:
                pass
        os._exit(0)

    def compose(self) -> ComposeResult:
        yield DashHeader()
        with ContentSwitcher(initial="page-dashboard"):
            for _, page_id, cls in _PAGES:
                yield cls(id=page_id)
        yield Footer()

    def on_mount(self) -> None:
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

        def _is_true(key: str) -> bool:
            return config.get(key, "").strip().lower() in ("1", "true", "yes")

        if _is_true("TUIDASH_PRIVACY_FORCE"):
            self._privacy_forced = True
            self.privacy = True
        elif _is_true("TUIDASH_PRIVACY_DEFAULT"):
            self.privacy = True

    # ── subtitle ──────────────────────────────────────────────────────────────

    def _update_subtitle(self) -> None:
        page_label = _PAGES[self._page_idx][0]
        parts = [f"[{self._page_idx + 1}/{len(_PAGES)}] {page_label}"]
        if self.privacy:
            parts.append("PRIVATE MODE")
        parts.append(f"↻ {self.refresh_interval}s")
        text = "  ".join(parts)
        self.sub_title = text
        try:
            self.query_one(DashHeader).set_subtitle(text)
        except Exception:
            pass

    # ── reactives ─────────────────────────────────────────────────────────────

    def watch_privacy(self, value: bool) -> None:
        self._update_subtitle()
        for page in self.query(BasePage):
            if hasattr(page, "set_privacy"):
                page.set_privacy(value)

    def watch_refresh_interval(self, value: int) -> None:
        self._update_subtitle()
        for page in self.query(BasePage):
            if hasattr(page, "set_refresh_interval"):
                page.set_refresh_interval(value)

    # ── actions ───────────────────────────────────────────────────────────────

    def action_toggle_privacy(self) -> None:
        if not self._privacy_forced:
            self.privacy = not self.privacy

    def action_decrease_refresh(self) -> None:
        self.refresh_interval = max(30, self.refresh_interval - 60)

    def action_increase_refresh(self) -> None:
        self.refresh_interval = min(3600, self.refresh_interval + 60)

    def action_refresh(self) -> None:
        self.notify("Refreshing…", severity="information")
        _, page_id, _ = _PAGES[self._page_idx]
        try:
            page = self.query_one(f"#{page_id}", BasePage)
            if hasattr(page, "refresh_all"):
                page.refresh_all()
        except Exception:
            pass

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action in ("prev_month", "next_month"):
            return _PAGES[self._page_idx][1] == "page-calendar"
        if action == "toggle_playback":
            from .widgets.podcasts import player as _p
            return _p.running or None
        return True

    def action_toggle_playback(self) -> None:
        from .widgets.podcasts import player as _p
        from .widgets.podcasts import PodcastsWidget
        if not _p.running:
            return
        _p.pause_toggle()
        try:
            self.query_one(PodcastsWidget)._set_global_playing(not _p.paused)
        except Exception:
            pass

    def action_prev_month(self) -> None:
        try:
            self.query_one("#page-calendar", CalendarPage).action_prev_month()
        except Exception:
            pass

    def action_next_month(self) -> None:
        try:
            self.query_one("#page-calendar", CalendarPage).action_next_month()
        except Exception:
            pass

    def action_prev_page(self) -> None:
        self._page_idx = (self._page_idx - 1) % len(_PAGES)
        self.query_one(ContentSwitcher).current = _PAGES[self._page_idx][1]
        self._update_subtitle()

    def action_next_page(self) -> None:
        self._page_idx = (self._page_idx + 1) % len(_PAGES)
        self.query_one(ContentSwitcher).current = _PAGES[self._page_idx][1]
        self._update_subtitle()

    def action_go_page(self, idx: int) -> None:
        if 0 <= idx < len(_PAGES):
            self._page_idx = idx
            self.query_one(ContentSwitcher).current = _PAGES[idx][1]
            self._update_subtitle()


def _local_ips() -> list[str]:
    """Return all non-loopback IPv4 addresses on this machine."""
    try:
        # Linux / Docker
        out = subprocess.check_output(
            ["hostname", "-I"], text=True, stderr=subprocess.DEVNULL, timeout=2
        )
        ips = [ip for ip in out.split() if not ip.startswith("127.")]
        if ips:
            return ips
    except Exception:
        pass
    try:
        # macOS / BSD
        out = subprocess.check_output(
            ["ifconfig"], text=True, stderr=subprocess.DEVNULL, timeout=2
        )
        return [
            m.group(1)
            for m in re.finditer(r"\binet (\d+\.\d+\.\d+\.\d+)\b", out)
            if not m.group(1).startswith("127.")
        ]
    except Exception:
        return []


def _detect_serve_ip() -> str:
    """Return the best local IP for the serve public URL.

    Priority: Tailscale (100.x.x.x) → LAN (192.168.x.x) → other private → localhost.
    Avoids picking a VPN tunnel IP when better options exist.
    """
    # Tailscale: connecting to Magic DNS gives the Tailscale interface IP
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("100.100.100.100", 80))
            ip = s.getsockname()[0]
        if ip.startswith("100."):
            return ip
    except Exception:
        pass

    all_ips = _local_ips()
    for prefix in ("192.168.", "10.", "172."):
        for ip in all_ips:
            if ip.startswith(prefix):
                return ip

    return "localhost"


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
        public_url = config.get("TUIDASH_SERVE_URL") or None
        if not public_url:
            public_host = args.host
            if public_host == "0.0.0.0":
                public_host = _detect_serve_ip()
            public_url = f"http://{public_host}:{args.port}"
        sys.exit(subprocess.run(
            [textual_bin, "serve", "-c", f"{sys.executable} -m tuidash.app",
             "-h", args.host, "-p", str(args.port), "-u", public_url]
        ).returncode)

    TuidashApp().run()


if __name__ == "__main__":
    main()
