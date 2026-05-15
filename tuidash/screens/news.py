from __future__ import annotations

from textual.app import ComposeResult
from textual.widgets import Label

from . import BasePage


class NewsPage(BasePage):
    """Expanded news reader — page 2 (coming soon)."""

    DEFAULT_CSS = """
    NewsPage {
        height: 100%;
        align: center middle;
    }
    #news-placeholder {
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("News — coming soon", id="news-placeholder")
