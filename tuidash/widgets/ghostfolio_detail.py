from __future__ import annotations

from typing import Any

from rich.align import Align
from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static

from .base import DashWidget


class GhostfolioDetailWidget(DashWidget):
    """Extended Ghostfolio view — not yet implemented."""

    DEFAULT_CSS = """
    GhostfolioDetailWidget { height: 100%; }
    #gf-detail-body { height: 100%; padding: 0 1; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    def compose(self) -> ComposeResult:
        yield Static("", id="gf-detail-body")

    def on_mount(self) -> None:
        t = Text("Coming soon", style="dim")
        self.query_one("#gf-detail-body", Static).update(Align.center(t, vertical="middle"))

    def set_refresh_interval(self, seconds: int) -> None:
        pass
