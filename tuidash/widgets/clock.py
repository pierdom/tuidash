from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static

from .base import DashWidget


_COLOR = "#00d4aa"

# 5-pixel-wide × 8-pixel-tall bitmap font; colon is 2-pixel-wide.
# '█' = lit pixel, ' ' = dark pixel.
# Each pair of pixel rows renders as one display line via half-blocks.
_FONT: dict[str, list[str]] = {
    "0": ["█████", "█   █", "█   █", "█   █", "█   █", "█   █", "█   █", "█████"],
    "1": ["  █  ", "  █  ", "  █  ", "  █  ", "  █  ", "  █  ", "  █  ", "  █  "],
    "2": ["█████", "    █", "    █", "█████", "█    ", "█    ", "█    ", "█████"],
    "3": ["█████", "    █", "    █", "█████", "    █", "    █", "    █", "█████"],
    "4": ["█   █", "█   █", "█   █", "█████", "    █", "    █", "    █", "    █"],
    "5": ["█████", "█    ", "█    ", "█████", "    █", "    █", "    █", "█████"],
    "6": ["█████", "█    ", "█    ", "█████", "█   █", "█   █", "█   █", "█████"],
    "7": ["█████", "    █", "    █", "    █", "    █", "    █", "    █", "    █"],
    "8": ["█████", "█   █", "█   █", "█████", "█   █", "█   █", "█   █", "█████"],
    "9": ["█████", "█   █", "█   █", "█████", "    █", "    █", "    █", "█████"],
    ":": ["  ", "██", "  ", "  ", "  ", "██", "  ", "  "],
}


def _render_clock(now: datetime) -> Text:
    time_str = now.strftime("%H:%M")
    t = Text(justify="center")

    for line_i in range(4):  # 8 pixel rows → 4 display lines
        if line_i > 0:
            t.append("\n")
        for col_i, ch in enumerate(time_str):
            if col_i > 0:
                t.append(" ")
            rows = _FONT[ch]
            top = rows[line_i * 2]
            bot = rows[line_i * 2 + 1]
            for tc, bc in zip(top, bot):
                if tc == "█" and bc == "█":
                    t.append("█", style=_COLOR)
                elif tc == "█":
                    t.append("▀", style=_COLOR)
                elif bc == "█":
                    t.append("▄", style=_COLOR)
                else:
                    t.append(" ")

    return t


class ClockWidget(DashWidget):
    """Pixel-art clock showing time in half-block font and date below."""

    DEFAULT_CSS = """
    ClockWidget { height: 100%; }
    #clock-body { height: 100%; content-align: center middle; text-align: center; }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="clock-body")

    def on_mount(self) -> None:
        self._tick()
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        now = datetime.now()
        self.query_one("#clock-body", Static).update(_render_clock(now))
        self.border_subtitle = now.strftime("%d/%m/%Y  %A")
