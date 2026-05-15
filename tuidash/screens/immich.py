from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header, Label


class ImmichScreen(Screen):
    """Immich photo viewer — page 3 (coming soon)."""

    CSS = """
    ImmichScreen {
        align: center middle;
    }
    #immich-placeholder {
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Photos — coming soon", id="immich-placeholder")
        yield Footer()
