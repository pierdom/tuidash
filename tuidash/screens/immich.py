from __future__ import annotations

from textual.app import ComposeResult
from textual.widgets import Label

from . import BasePage


class ImmichPage(BasePage):
    """Immich photo viewer — page 3 (coming soon)."""

    DEFAULT_CSS = """
    ImmichPage {
        height: 100%;
        align: center middle;
    }
    #immich-placeholder {
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Photos — coming soon", id="immich-placeholder")
