from __future__ import annotations

from textual.app import ComposeResult

from . import BasePage
from ..widgets.news_reader import NewsReaderWidget


class NewsPage(BasePage):
    """Expanded news reader — page 2."""

    DEFAULT_CSS = """
    NewsPage {
        height: 100%;
    }
    NewsReaderWidget {
        height: 100%;
        border-title-color: $accent;
        border-title-style: bold;
        border-subtitle-color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        yield NewsReaderWidget()

    def on_mount(self) -> None:
        self.query_one(NewsReaderWidget).border_title = "  News"

    def set_refresh_interval(self, value: int) -> None:
        try:
            self.query_one(NewsReaderWidget).set_refresh_interval(value)
        except Exception:
            pass

    def refresh_all(self) -> None:
        try:
            self.query_one(NewsReaderWidget)._load()
        except Exception:
            pass
