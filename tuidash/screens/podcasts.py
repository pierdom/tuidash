from __future__ import annotations

from textual.app import ComposeResult

from . import BasePage
from ..widgets.podcasts import PodcastsWidget


class PodcastsPage(BasePage):
    """Podcast feed viewer and player — page 4."""

    DEFAULT_CSS = """
    PodcastsPage {
        height: 100%;
    }
    PodcastsPage PodcastsWidget {
        height: 100%;
    }
    """

    def compose(self) -> ComposeResult:
        yield PodcastsWidget()

    def on_mount(self) -> None:
        self.query_one(PodcastsWidget).border_title = "  Podcasts"

    def set_refresh_interval(self, value: int) -> None:
        try:
            self.query_one(PodcastsWidget).set_refresh_interval(value)
        except Exception:
            pass

    def refresh_all(self) -> None:
        try:
            self.query_one(PodcastsWidget)._load()
        except Exception:
            pass
