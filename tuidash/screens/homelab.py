from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from . import BasePage
from .. import config
from ..widgets.hetzner import HetznerWidget
from ..widgets.homelab import HomelabHostWidget
from ..widgets.tailscale import TailscaleWidget


class HomelabPage(BasePage):
    """Top 25%: host widgets side-by-side. Bottom 75%: Tailscale + Hetzner."""

    DEFAULT_CSS = """
    HomelabPage               { height: 100%; }
    #homelab-top              { height: 40%; }
    #homelab-bottom           { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        raw  = config.get("TUIDASH_HOSTS", "") or ""
        urls = [u.strip() for u in raw.split(",") if u.strip()]

        with Horizontal(id="homelab-top"):
            if not urls:
                yield Static("[dim]No hosts configured — set TUIDASH_HOSTS[/dim]")
            else:
                for url in urls:
                    yield HomelabHostWidget(url=url)

        with Vertical(id="homelab-bottom"):
            yield TailscaleWidget()
            yield HetznerWidget()

    def on_show(self) -> None:
        for w in self.query(HomelabHostWidget):
            try:
                w.query_one("#host-scroll").focus()
                break
            except Exception:
                pass

    def refresh_all(self) -> None:
        for w in self.query(HomelabHostWidget):
            w._load()
        for cls in (TailscaleWidget, HetznerWidget):
            try:
                self.query_one(cls)._load()
            except Exception:
                pass

    def set_refresh_interval(self, seconds: int) -> None:
        for w in self.query(HomelabHostWidget):
            w.set_refresh_interval(seconds)
        for cls in (TailscaleWidget, HetznerWidget):
            try:
                self.query_one(cls).set_refresh_interval(seconds)
            except Exception:
                pass
