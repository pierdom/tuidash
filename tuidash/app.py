from __future__ import annotations

import argparse
import asyncio
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

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import ContentSwitcher, Footer

from . import config
from .screens import BasePage
from .widgets.base import DashWidget
from .screens.dashboard import DashboardPage
from .screens.calendar import CalendarPage
from .screens.news import NewsPage
from .screens.podcasts import PodcastsPage
from .screens.portfolio import PortfolioPage
from .widgets.header import DashHeader


# Ordered list of pages: (label, widget-id, class).
# Add a new entry here to register a new page in the carousel.
_PAGES: list[tuple[str, str, type]] = [
    ("Dashboard", "page-dashboard",  DashboardPage),
    ("News",      "page-news",       NewsPage),
    ("Calendar",  "page-calendar",   CalendarPage),
    ("Podcasts",  "page-podcasts",   PodcastsPage),
    ("Portfolio", "page-portfolio",  PortfolioPage),
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

    /* ── mobile mode (narrow terminal / phone browser) ── */

    /* Dashboard page */
    #dashboard-scroll { height: 1fr; overflow-y: hidden; }
    .mobile #row-top { layout: vertical; height: auto; }
    .mobile #row-mid { layout: vertical; height: auto; }
    .mobile #row-bot { height: auto; }
    .mobile ClockWidget    { width: 100%; height: 6; margin: 0; }
    .mobile WeatherWidget  { width: 100%; height: auto; margin: 0; }
    .mobile CalendarWidget { width: 100%; height: auto; }
    .mobile GhostfolioWidget { width: 100%; height: auto; margin: 0; }
    .mobile #conn-hosts-col  { width: 100%; }
    .mobile ConnectivityWidget { height: auto; }
    .mobile HostsWidget        { height: auto; }
    .mobile EventsWidget { height: auto; }
    .mobile #events-body { height: auto; }

    /* News page — vertical 30/70 split, each widget scrolls internally */
    .mobile NewsPage Horizontal { layout: vertical; }
    .mobile NewsPage RelayWidget      { width: 100%; height: 30%; }
    .mobile NewsPage NewsReaderWidget { width: 100%; height: 70%; }

    /* Calendar page */
    .mobile CalendarPage { overflow-y: scroll; scrollbar-size-vertical: 1; }

    /* Podcasts page — inner SC scrolls cards; controls stay pinned at bottom */
    .mobile #podcasts-grid { grid-size: 1; }

    /* Portfolio page — vertical 30/70 split, each widget scrolls internally */
    .mobile PortfolioPage Horizontal { layout: vertical; }
    .mobile PortfolioPage RelayWidget            { width: 100%; height: 30%; }
    .mobile PortfolioPage GhostfolioDetailWidget { width: 100%; height: 70%; }

    /* Scroll-captured widget highlight (mobile pointer lock) */
    .scroll-captured { border: heavy $accent; }
    """

    privacy:          reactive[bool] = reactive(False)
    refresh_interval: reactive[int]  = reactive(300, always_update=True)
    _privacy_forced:  bool           = False
    _privacy_default: bool           = False
    _relock_timer:    Timer | None   = None
    _page_idx:        int                      = 0
    _hover_sc:        ScrollableContainer | None = None

    _RELOCK_SECONDS = 5 * 60

    BINDINGS = [
        ("q",                "quit",             "Quit"),
        ("r",                "refresh",          "Refresh"),
        ("p",                "toggle_privacy",   "Privacy"),
        ("[",                "decrease_refresh", "-60s"),
        ("]",                "increase_refresh", "+60s"),
        Binding("left",      "prev_page",        "Prev page",  priority=True),
        Binding("right",     "next_page",        "Next page",  priority=True),
        Binding("space",     "toggle_playback",  "⏯ Play/Pause", priority=True),
        Binding("comma",     "prev_month",       "Prev month",  priority=True),
        Binding("full_stop", "next_month",       "Next month",  priority=True),
        Binding("pageup",    "scroll_up",        "Scroll up",   show=False, priority=True),
        Binding("pagedown",  "scroll_down",      "Scroll down", show=False, priority=True),
        Binding("up",        "scroll_up_line",   "",            show=False),
        Binding("down",      "scroll_down_line", "",            show=False),
        *[
            Binding(str(i + 1), f"go_page({i})", f"Page {i + 1}", show=False)
            for i in range(len(_PAGES))
        ],
    ]

    # Add "mobile" class to Screen when terminal width < 90 columns.
    # Textual handles both startup and subsequent resizes automatically.
    HORIZONTAL_BREAKPOINTS = [(0, "mobile"), (100, "wide")]

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
            self._privacy_default = True
            self.privacy = True

    # ── subtitle ──────────────────────────────────────────────────────────────

    def _update_subtitle(self) -> None:
        page_label = _PAGES[self._page_idx][0]
        parts = [f"[{self._page_idx + 1}/{len(_PAGES)}] {page_label}"]
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
        try:
            self.query_one(DashHeader).set_privacy(value)
        except Exception:
            pass
        if not value and self._privacy_default and not self._privacy_forced:
            if self._relock_timer is not None:
                self._relock_timer.stop()
            self._relock_timer = self.set_timer(self._RELOCK_SECONDS, self._relock_privacy)
            self.notify("Privacy mode will re-enable in 5 minutes", severity="information", timeout=6)
        elif value and self._relock_timer is not None:
            self._relock_timer.stop()
            self._relock_timer = None
        for page in self.query(BasePage):
            if hasattr(page, "set_privacy"):
                page.set_privacy(value)

    def _relock_privacy(self) -> None:
        self._relock_timer = None
        self.privacy = True
        self.notify("Privacy mode re-enabled", severity="warning", timeout=4)

    def watch_refresh_interval(self, value: int) -> None:
        self._update_subtitle()
        for page in self.query(BasePage):
            if hasattr(page, "set_refresh_interval"):
                page.set_refresh_interval(value)

    # ── scroll helpers ────────────────────────────────────────────────────────

    def on_mouse_move(self, event: events.MouseMove) -> None:
        try:
            widget, _ = self.get_widget_at(event.screen_x, event.screen_y)
        except Exception:
            return
        sc: ScrollableContainer | None = None
        w = widget
        while w is not None:
            if isinstance(w, ScrollableContainer):
                sc = w
                break
            w = w.parent
        if sc is not self._hover_sc:
            self._hover_sc = sc

    def _active_sc(self) -> Widget | None:
        # Focused DashWidget on the current page → use its SC (keyboard-driven scroll)
        focused = self.focused
        if focused is not None:
            w: Widget | None = focused
            while w is not None:
                if isinstance(w, DashWidget):
                    try:
                        return w.query_one(ScrollableContainer)
                    except Exception:
                        break
                w = w.parent  # type: ignore[assignment]
        if self._hover_sc is not None:
            return self._hover_sc
        _, page_id, _ = _PAGES[self._page_idx]
        try:
            page = self.query_one(f"#{page_id}")
            sc_results = page.query(ScrollableContainer)
            if sc_results:
                return sc_results.first()
            # Page itself may be scrollable (e.g. DashboardPage with overflow-y: auto).
            # Use the CSS property rather than allow_vertical_scroll, which depends
            # on virtual_size being known — not guaranteed on the first frame.
            if page.styles.overflow_y in ("auto", "scroll"):
                return page
        except Exception:
            pass
        return None

    def action_scroll_up(self) -> None:
        sc = self._active_sc()
        if sc:
            sc.scroll_page_up(animate=True)

    def action_scroll_down(self) -> None:
        sc = self._active_sc()
        if sc:
            sc.scroll_page_down(animate=True)

    def action_scroll_up_line(self) -> None:
        sc = self._active_sc()
        if sc:
            sc.scroll_up(animate=True)

    def action_scroll_down_line(self) -> None:
        sc = self._active_sc()
        if sc:
            sc.scroll_down(animate=True)

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

    def action_toggle_page_menu(self) -> None:
        from .widgets.header import PageMenu
        try:
            self.screen.query_one(PageMenu).remove()
            return
        except Exception:
            pass
        menu = PageMenu([label for label, _, _ in _PAGES], self._page_idx)
        self.screen.mount(menu)
        menu.focus()

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

    def _switch_page(self, idx: int) -> None:
        self._page_idx = idx
        self._hover_sc = None
        _, page_id, _ = _PAGES[idx]
        self.query_one(ContentSwitcher).current = page_id
        self._update_subtitle()
        try:
            self.query_one(f"#{page_id}", BasePage).scroll_home(animate=False)
        except Exception:
            pass

    def action_prev_page(self) -> None:
        self._switch_page((self._page_idx - 1) % len(_PAGES))

    def action_next_page(self) -> None:
        self._switch_page((self._page_idx + 1) % len(_PAGES))

    def action_go_page(self, idx: int) -> None:
        if 0 <= idx < len(_PAGES):
            self._switch_page(idx)


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


# ── mobile HTTP proxy ─────────────────────────────────────────────────────────
# textual-serve renders the TUI inside xterm.js in the browser.  xterm.js keeps
# a hidden <textarea> focused so it can receive keyboard events; on mobile OSes
# this causes the virtual keyboard to pop up on every touch.  Setting
# inputmode="none" on that element suppresses the keyboard while still allowing
# hardware keyboards to work.  We achieve this by running a thin asyncio TCP
# proxy that intercepts the HTML page response and injects the one-liner fix;
# WebSocket connections (the actual TUI stream) are tunneled transparently.

_MOBILE_INJECT = (
    # Prevent the browser page from showing its own scrollbar alongside the
    # Textual scrollbar rendered inside the xterm.js canvas.
    b'<style>html,body{overflow:hidden!important;height:100%!important;}</style>'
    b'<script>(function(){'
    b'function f(){'
    b'var t=document.querySelector(".xterm-helper-textarea");'
    b'if(!t)return false;'
    b't.setAttribute("inputmode","none");'
    b'return true;}'
    b'if(!f()){var m=new MutationObserver(function(){if(f())m.disconnect();});'
    b'm.observe(document.body,{childList:true,subtree:true});}'
    b'})();</script>'
)


async def _pipe(r, w) -> None:
    try:
        while chunk := await r.read(65536):
            w.write(chunk)
            await w.drain()
    except Exception:
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


async def _proxy_conn(cr, cw, internal_port: int) -> None:
    uw = None
    try:
        line = await cr.readline()
        if not line:
            return
        raw = bytearray(line)
        is_ws = False
        req_cl = 0
        while True:
            h = await cr.readline()
            raw += h
            low = h.lower()
            if b"upgrade" in low and b"websocket" in low:
                is_ws = True
            if low.startswith(b"content-length:"):
                try:
                    req_cl = int(h.split(b":", 1)[1].strip())
                except Exception:
                    pass
            if h in (b"\r\n", b"\n") or not h:
                break

        for attempt in range(30):
            try:
                ur, uw = await asyncio.open_connection("127.0.0.1", internal_port)
                break
            except OSError:
                if attempt == 29:
                    raise
                await asyncio.sleep(0.5)

        uw.write(bytes(raw))
        if req_cl:
            uw.write(await cr.read(req_cl))

        if is_ws:
            await uw.drain()
            await asyncio.gather(_pipe(cr, uw), _pipe(ur, cw))
            return

        await uw.drain()

        resp = bytearray()
        is_html = is_chunked = False
        resp_cl = 0
        while True:
            h = await ur.readline()
            if not h:
                break
            resp += h
            low = h.lower()
            if b"content-type:" in low and b"text/html" in low:
                is_html = True
            if low.startswith(b"content-length:"):
                try:
                    resp_cl = int(h.split(b":", 1)[1].strip())
                except Exception:
                    pass
            if b"transfer-encoding" in low and b"chunked" in low:
                is_chunked = True
            if h in (b"\r\n", b"\n"):
                break

        if is_html and resp_cl and not is_chunked:
            body = await ur.read(resp_cl)
            body = body.replace(b"</head>", _MOBILE_INJECT + b"</head>", 1)
            resp_bytes = re.sub(
                rb"(?i)(content-length:\s*)\d+",
                lambda m: m.group(1) + str(len(body)).encode(),
                bytes(resp),
            )
            cw.write(resp_bytes + body)
        else:
            cw.write(bytes(resp))
            await _pipe(ur, cw)
        await cw.drain()
    except Exception:
        pass
    finally:
        for w in (cw, uw):
            if w:
                try:
                    w.close()
                except Exception:
                    pass


async def _run_proxy(bind_host: str, public_port: int, internal_port: int, proc) -> None:
    server = await asyncio.start_server(
        lambda r, w: _proxy_conn(r, w, internal_port),
        bind_host, public_port,
    )
    async with server:
        while proc.poll() is None:
            await asyncio.sleep(1)


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
            mdns = config.get("TUIDASH_SERVE_MDNS", "").strip().lower() in ("1", "true", "yes")
            if mdns:
                try:
                    raw = socket.gethostname()
                    # Use bare hostname + .local; skip if gethostname already returned an FQDN
                    public_host = raw.split(".")[0] + ".local"
                except Exception:
                    public_host = _detect_serve_ip()
            elif args.host == "0.0.0.0":
                public_host = _detect_serve_ip()
            else:
                public_host = args.host
            public_url = f"http://{public_host}:{args.port}"

        # Pick a free localhost port for the internal textual-serve instance.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
            _s.bind(("127.0.0.1", 0))
            internal_port = _s.getsockname()[1]

        proc = subprocess.Popen([
            textual_bin, "serve", "-c", f"{sys.executable} -m tuidash.app",
            "-h", "127.0.0.1", "-p", str(internal_port), "-u", public_url,
        ])
        try:
            asyncio.run(_run_proxy(args.host, args.port, internal_port, proc))
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        sys.exit(0)

    TuidashApp().run()


if __name__ == "__main__":
    main()
