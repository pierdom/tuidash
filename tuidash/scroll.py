from __future__ import annotations

import time as _time

SCROLL_INTERVAL: float = 0.24
PAUSE_L_TICKS: int = round(15 / SCROLL_INTERVAL)  # ≈15 s pause at left end
PAUSE_R_TICKS: int = round(3  / SCROLL_INTERVAL)  # ≈3 s pause at right end


def current_tick() -> int:
    """Absolute tick from monotonic clock — same value for all callers at the same instant."""
    return int(_time.monotonic() / SCROLL_INTERVAL)


def scroll_offset(tick: int, phase: int, overflow: int) -> int:
    """Boomerang scroll offset: left-pause → scroll right → right-pause → scroll left."""
    cycle = PAUSE_L_TICKS + overflow + PAUSE_R_TICKS + overflow
    pos   = (tick + phase) % cycle
    if pos < PAUSE_L_TICKS:
        return 0
    pos -= PAUSE_L_TICKS
    if pos < overflow:
        return pos
    pos -= overflow
    if pos < PAUSE_R_TICKS:
        return overflow
    return overflow - (pos - PAUSE_R_TICKS)


def scroll_window(text: str, width: int, tick: int, phase: int) -> str:
    """Return the visible width-char slice of text for this tick."""
    overflow = len(text) - width
    if overflow <= 0:
        return text
    offset = scroll_offset(tick, phase, overflow)
    return text[offset : offset + width]
