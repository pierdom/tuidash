from __future__ import annotations

from textual.app import ComposeResult

from . import BasePage
from ..widgets.homelab import HomelabWidget


class HomelabPage(BasePage):
    """Full-page homelab monitoring — page 6."""

    DEFAULT_CSS = """
    HomelabPage  { height: 100%; }
    HomelabWidget { height: 100%; }
    """

    def compose(self) -> ComposeResult:
        yield HomelabWidget()

    def on_mount(self) -> None:
        self.query_one(HomelabWidget).border_title = "  Homelab"

    def on_show(self) -> None:
        self.query_one(HomelabWidget).focus()

    def refresh_all(self) -> None:
        try:
            self.query_one(HomelabWidget)._load()
        except Exception:
            pass

    def set_refresh_interval(self, seconds: int) -> None:
        try:
            self.query_one(HomelabWidget).set_refresh_interval(seconds)
        except Exception:
            pass
