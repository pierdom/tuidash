from __future__ import annotations

from textual.widget import Widget

from ..widgets.base import DashWidget


class BasePage(Widget):
    """Shared base for all dashboard pages."""

    DEFAULT_CSS = """
    BasePage { height: 100%; }
    """

    def on_show(self) -> None:
        for w in self.query(DashWidget):
            w.resume_animations()

    def on_hide(self) -> None:
        for w in self.query(DashWidget):
            w.pause_animations()
