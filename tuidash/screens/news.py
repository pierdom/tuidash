from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header, Label


class NewsScreen(Screen):
    """Expanded news reader — page 2 (coming soon)."""

    CSS = """
    NewsScreen {
        align: center middle;
    }
    #news-placeholder {
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("News — coming soon", id="news-placeholder")
        yield Footer()
