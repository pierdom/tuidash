from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal

from . import BasePage
from ..widgets.ghostfolio_detail import GhostfolioDetailWidget
from ..widgets.relay import RelayWidget


class PortfolioPage(BasePage):
    """Portfolio overview — page 5."""

    DEFAULT_CSS = """
    PortfolioPage {
        height: 100%;
    }
    PortfolioPage Horizontal {
        height: 100%;
    }
    PortfolioPage RelayWidget {
        width: 1fr;
        height: 100%;
        border-title-color: $accent;
        border-title-style: bold;
        border-subtitle-color: $text-muted;
    }
    PortfolioPage GhostfolioDetailWidget {
        width: 1fr;
        height: 100%;
        border-title-color: $accent;
        border-title-style: bold;
        border-subtitle-color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield RelayWidget("financial-analyst", show_title=False)
            yield GhostfolioDetailWidget()

    def on_mount(self) -> None:
        self.query_one(GhostfolioDetailWidget).border_title = "  Portfolio detail"

    def set_privacy(self, value: bool) -> None:
        try:
            self.query_one(GhostfolioDetailWidget).set_privacy(value)
        except Exception:
            pass

    def set_refresh_interval(self, value: int) -> None:
        for w in (RelayWidget, GhostfolioDetailWidget):
            try:
                self.query_one(w).set_refresh_interval(value)
            except Exception:
                pass

    def refresh_all(self) -> None:
        try:
            self.query_one(RelayWidget)._load()
        except Exception:
            pass
        try:
            self.query_one(GhostfolioDetailWidget)._load()
        except Exception:
            pass
