from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from . import BasePage
from .. import config
from ..widgets.hetzner import HetznerWidget
from ..widgets.homelab import FleetStatusWidget, HomelabHostWidget
from ..widgets.tailscale import TailscaleWidget


class HomelabPage(BasePage):
    """Top: scarif, bespin, endor stacked vertically — scarif sizes to its own
    content (auto, capped), and of the remainder bespin outranks endor (2fr vs
    1fr), so whichever card has more to show gets first claim on extra room.
    #homelab-top itself is 1fr (not a fixed %), and strip/bottom are auto, so
    the whole page's real estate goes to whatever actually needs it rather
    than leaving a dead gap below Hetzner. Middle: fleet-wide
    connectivity/Docker/speedtest strip. Bottom: Tailscale + Hetzner.
    """

    DEFAULT_CSS = """
    HomelabPage                  { height: 100%; }
    #homelab-top                 { height: 1fr; }
    #homelab-scarif              { height: auto; }
    .homelab-other-host-primary  { height: 2fr; }
    #homelab-strip               { height: auto; }
    #homelab-bottom              { height: auto; }
    """

    def compose(self) -> ComposeResult:
        central = config.get("TUIDASH_HOMELAB_CENTRAL", "scarif") or "scarif"
        raw     = config.get("TUIDASH_HOMELAB_HOSTS", "") or ""
        others  = [h.strip() for h in raw.split(",") if h.strip()]

        with Vertical(id="homelab-top"):
            yield HomelabHostWidget(host=central, central=True, id="homelab-scarif")
            if not others:
                yield Static("[dim]No additional hosts — set TUIDASH_HOMELAB_HOSTS[/dim]")
            else:
                for i, host in enumerate(others):
                    classes = "homelab-other-host" + (" homelab-other-host-primary" if i == 0 else "")
                    yield HomelabHostWidget(host=host, central=False, classes=classes)

        with Vertical(id="homelab-strip"):
            yield FleetStatusWidget()

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
        for cls in (FleetStatusWidget, TailscaleWidget, HetznerWidget):
            try:
                self.query_one(cls)._load()
            except Exception:
                pass

    def set_refresh_interval(self, seconds: int) -> None:
        for w in self.query(HomelabHostWidget):
            w.set_refresh_interval(seconds)
        for cls in (FleetStatusWidget, TailscaleWidget, HetznerWidget):
            try:
                self.query_one(cls).set_refresh_interval(seconds)
            except Exception:
                pass
