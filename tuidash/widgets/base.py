from __future__ import annotations

from rich.text import Text
from textual import events
from textual.containers import ScrollableContainer
from textual.widget import Widget

from ..theme import BAR_HIGH, BAR_LOW, BAR_MID, NEON_BORDER, NEON_PRIMARY


def neon_bar(pct: float | None, width: int = 10) -> Text:
    """Blocky btop-style progress bar with fixed-position gradient zones.

    Zones: 0–60% of bar = BAR_LOW, 60–80% = BAR_MID, 80–100% = BAR_HIGH.
    """
    if pct is None:
        return Text("─" * width, style="dim")
    p      = max(0.0, min(100.0, float(pct)))
    filled = max(0, round(p / 100 * width))
    g_end  = round(width * 0.60)
    y_end  = round(width * 0.80)
    t      = Text()
    pos    = 0
    g = min(filled, g_end)
    if g > 0:
        t.append("█" * g, style=BAR_LOW)
        pos += g
    y = min(filled - pos, max(0, y_end - g_end))
    if y > 0:
        t.append("█" * y, style=BAR_MID)
        pos += y
    if pos < filled:
        t.append("█" * (filled - pos), style=BAR_HIGH)
    if filled < width:
        t.append("░" * (width - filled), style="dim")
    return t


class DashWidget(Widget):
    """Base class for all dashboard widgets."""

    # Subclasses that need their internal ScrollableContainer to remain
    # scrollable on mobile (rather than expanding to full height) set this True.
    _mobile_scrollable: bool = False

    DEFAULT_CSS = f"""
    DashWidget {{
        border: round {NEON_BORDER};
        padding: 0 1;
        border-title-color: {NEON_PRIMARY};
        border-title-style: bold;
        border-subtitle-color: $text-muted;
    }}
    DashWidget:focus {{
        border: round {NEON_PRIMARY};
    }}
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
