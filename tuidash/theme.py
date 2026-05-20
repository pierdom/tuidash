from __future__ import annotations

# ── tuidash colour theme ───────────────────────────────────────────────────────
# Edit this file to restyle the entire dashboard.
# All CSS strings and Rich text styles import their colours from here.

# Core palette
NEON_PRIMARY = "#00d4aa"   # neon mint  — border titles, clock, nav arrows, accents
NEON_BORDER  = "#005f4e"   # dark teal  — widget / button borders
NEON_BG      = "#0d2018"   # very dark teal — header & footer background

# Progress bar gradient zones (neon_bar in base.py)
BAR_LOW  = "bright_green"  # 0 – 60 % of bar width
BAR_MID  = "yellow"        # 60 – 80 % of bar width
BAR_HIGH = "bright_red"    # 80 – 100 % of bar width

# Performance heatmap (Ghostfolio stat cells)
PERF_GREAT    = "bright_green"  # > +10 %
PERF_GOOD     = "green"         #   0 to +10 %
PERF_FLAT     = "cyan"          #  −5 to   0 %
PERF_BAD      = "yellow"        # −10 to  −5 %
PERF_POOR     = "red"           # −20 to −10 %
PERF_TERRIBLE = "bright_red"    # < −20 %
