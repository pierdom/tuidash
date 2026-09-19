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
    """Top: scarif (central, full width) above the other hosts side-by-side. Bottom: Tailscale + Hetzner."""

    DEFAULT_CSS = """
    HomelabPage               { height: 100%; }
    #homelab-top              { height: 55%; }
    #homelab-scarif           { height: 65%; }
    #homelab-others           { height: 35%; }
    #homelab-bottom           { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        central = config.get("TUIDASH_HOMELAB_CENTRAL", "scarif") or "scarif"
        raw     = config.get("TUIDASH_HOMELAB_HOSTS", "") or ""
        others  = [h.strip() for h in raw.split(",") if h.strip()]

        with Vertical(id="homelab-top"):
            with Horizontal(id="homelab-scarif"):
                yield HomelabHostWidget(host=central, central=True)
            with Horizontal(id="homelab-others"):
                if not others:
                    yield Static("[dim]No additional hosts — set TUIDASH_HOMELAB_HOSTS[/dim]")
                else:
                    for host in others:
                        yield HomelabHostWidget(host=host, central=False)

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
