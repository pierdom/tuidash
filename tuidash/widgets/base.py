from __future__ import annotations

from textual import events
from textual.containers import ScrollableContainer
from textual.widget import Widget


class DashWidget(Widget):
    """Base class for all dashboard widgets."""

    # Subclasses that need their internal ScrollableContainer to remain
    # scrollable on mobile (rather than expanding to full height) set this True.
    _mobile_scrollable: bool = False

    DEFAULT_CSS = """
    DashWidget {
        border: solid $primary-darken-2;
        padding: 0 1;
        border-title-color: $accent;
        border-title-style: bold;
        border-subtitle-color: $text-muted;
    }
    DashWidget:focus {
        border: solid $accent;
    }
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._scroll_captured: bool = False

    # ── mobile scroll management ───────────────────────────────────────────────

    def _sync_scroll_mode(self) -> None:
        """Set inline styles on the widget's ScrollableContainer for mobile vs wide.

        Inline styles override ALL CSS (including DEFAULT_CSS and App CSS), so this
        is more reliable than CSS selector rules when .mobile class depth makes
        descendant matching uncertain.
        """
        try:
            sc = self.query_one(ScrollableContainer)
        except Exception:
            return
        if self.screen.has_class("mobile") and not self._mobile_scrollable:
            sc.styles.height = "auto"
            sc.styles.overflow_y = "hidden"
        else:
            sc.styles.height = "1fr"
            sc.styles.overflow_y = "auto"

    def on_mount(self) -> None:
        # Textual calls on_mount for every class in the MRO, so this runs
        # even when a subclass defines its own on_mount without calling super().
        self.call_after_refresh(self._sync_scroll_mode)

    def on_resize(self, event: events.Resize) -> None:
        self._sync_scroll_mode()

    # ── mobile pointer capture ─────────────────────────────────────────────────

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
