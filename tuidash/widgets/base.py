from __future__ import annotations

from textual import events
from textual.containers import ScrollableContainer
from textual.reactive import reactive
from textual.widget import Widget


class DashWidget(Widget):
    """Base class for all dashboard widgets with title and loading state."""

    title: str = ""
    error: reactive[str | None] = reactive(None)

    DEFAULT_CSS = """
    DashWidget {
        border: solid $primary-darken-2;
        padding: 0 1;
    }
    DashWidget:focus {
        border: solid $accent;
    }
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._scroll_captured: bool = False

    def on_click(self, event: events.Click) -> None:
        """Border-tap → capture scroll to this widget. Any tap while captured → release."""
        try:
            sc = self.query_one(ScrollableContainer)
        except Exception:
            return
        if self._scroll_captured:
            sc.release_mouse()
            self._scroll_captured = False
            self.remove_class("scroll-captured")
            event.stop()
        elif event.widget is self:
            # Click originated on the border (no child under the pointer)
            sc.capture_mouse()
            self._scroll_captured = True
            self.add_class("scroll-captured")
            event.stop()
