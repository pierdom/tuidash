from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal

from . import BasePage
from ..widgets.news_reader import NewsReaderWidget
from ..widgets.relay import RelayWidget


class NewsPage(BasePage):
    """Expanded news reader — page 2."""

    DEFAULT_CSS = """
    NewsPage {
        height: 100%;
    }
    NewsPage Horizontal {
        height: 100%;
    }
    NewsPage RelayWidget {
        width: 1fr;
        height: 100%;
    }
    NewsPage NewsReaderWidget {
        width: 1fr;
        height: 100%;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield RelayWidget("news-digest")
            yield NewsReaderWidget()

    def on_mount(self) -> None:
        self.query_one(NewsReaderWidget).border_title = "  News"

    def set_refresh_interval(self, value: int) -> None:
        try:
            self.query_one(RelayWidget).set_refresh_interval(value)
            self.query_one(NewsReaderWidget).set_refresh_interval(value)
        except Exception:
            pass

    def refresh_all(self) -> None:
        try:
            self.query_one(RelayWidget)._load()
            self.query_one(NewsReaderWidget)._load()
        except Exception:
            pass
