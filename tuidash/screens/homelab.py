from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import ScrollableContainer
from textual.widgets import Static

from . import BasePage
from .. import config
from ..widgets.homelab import HomelabHostWidget


class HomelabPage(BasePage):
    """Full-page homelab monitoring — one widget per host, vertically scrollable."""

    DEFAULT_CSS = """
    HomelabPage               { height: 100%; }
    #homelab-scroll           { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        raw  = config.get("TUIDASH_HOSTS", "") or ""
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        with ScrollableContainer(id="homelab-scroll"):
            if not urls:
                yield Static("[dim]No hosts configured — set TUIDASH_HOSTS[/dim]")
            else:
                for url in urls:
                    yield HomelabHostWidget(url=url)

    def on_show(self) -> None:
        try:
            self.query_one("#homelab-scroll").focus()
        except Exception:
            pass

    def refresh_all(self) -> None:
        for w in self.query(HomelabHostWidget):
            w._load()

    def set_refresh_interval(self, seconds: int) -> None:
        for w in self.query(HomelabHostWidget):
            w.set_refresh_interval(seconds)
