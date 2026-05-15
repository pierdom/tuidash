from __future__ import annotations

from textual.widget import Widget


class BasePage(Widget):
    """Shared base for all dashboard pages."""

    DEFAULT_CSS = """
    BasePage { height: 100%; }
    """
