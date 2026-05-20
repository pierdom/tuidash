from __future__ import annotations

import os
import tomllib
from pathlib import Path

# ── palette loader ────────────────────────────────────────────────────────────
# Looks for  palettes/<name>.toml  next to the project root.
# Override the active palette with:  TUIDASH_PALETTE=<stem>  in the environment.

_PALETTES_DIR = Path(__file__).parent.parent / "palettes"


def _load() -> dict[str, str]:
    name = os.environ.get("TUIDASH_PALETTE", "default")
    path = _PALETTES_DIR / f"{name}.toml"
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


_p = _load()


def _c(key: str, default: str) -> str:
    return str(_p.get(key, default))


# ── Exported constants ────────────────────────────────────────────────────────

NEON_PRIMARY = _c("primary", "#00d4aa")
NEON_BORDER  = _c("border",  "#005f4e")
NEON_BG      = _c("bg",      "#0d2018")

BAR_LOW  = _c("bar_low",  "bright_green")
BAR_MID  = _c("bar_mid",  "yellow")
BAR_HIGH = _c("bar_high", "bright_red")

PERF_GREAT    = _c("perf_great",    "bright_green")
PERF_GOOD     = _c("perf_good",     "green")
PERF_FLAT     = _c("perf_flat",     "cyan")
PERF_BAD      = _c("perf_bad",      "yellow")
PERF_POOR     = _c("perf_poor",     "red")
PERF_TERRIBLE = _c("perf_terrible", "bright_red")
