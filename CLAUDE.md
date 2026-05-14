# tuidash — Claude Code guide

Personal terminal dashboard built with [Textual](https://textual.textualize.io/) + [Rich](https://rich.readthedocs.io/).

---

## Running the project

```bash
uv run tuidash          # run the dashboard
uv run python -m tuidash.app   # alternative
```

All dependencies are managed with `uv`. Never use `pip` directly.

---

## Project layout

```
tuidash/
├── app.py              # TuidashApp — layout, keybindings, global reactives
├── config.py           # Thin wrapper around python-dotenv (get / require)
├── ics.py              # ICS calendar parser (holidays)
└── widgets/
    ├── base.py         # DashWidget — base class for all widgets
    ├── clock.py        # Pixel-art half-block clock
    ├── calendar.py     # Monthly calendar with holiday highlighting
    ├── weather.py      # Open-Meteo weather + forecast
    ├── ghostfolio.py   # Ghostfolio portfolio tracker
    ├── connectivity.py # Ping / DNS / speed-test connectivity checks
    ├── rss.py          # RSS feed reader
    └── vps.py          # VPS status (stub)
```

`main.py` in the repo root is an unused stub — the real entry point is `tuidash.app:main`.

---

## Architecture

### App layout (CSS-driven)

```
Header (Textual built-in, shows app title + clock)
├── #row-top  28%  │ ClockWidget(30) │ WeatherWidget(2fr) │ CalendarWidget(1fr) │
├── #row-mid  44%  │ GhostfolioWidget(50%) │ ConnectivityWidget + HostsWidget(1fr) │
└── #row-bot  1fr  │ RssWidget(100%)                                              │
Footer (shows keybindings)
```

### Widget contract

Every widget:
1. Inherits `DashWidget` (which inherits `textual.widget.Widget`)
2. Declares its own `DEFAULT_CSS` (height: 100%; child body height: 100%)
3. Sets `border_title` in `app.on_mount()`; sets `border_subtitle` itself when data arrives
4. Has a `set_refresh_interval(seconds: int)` method — called by the app when the global interval changes or the user presses `[` / `]`
5. Has a `_load()` method decorated with `@work(thread=True)` that fetches data and calls `self.app.call_from_thread(self._show_data, data)`

### Threading pattern

```python
@work(thread=True)
def _load(self) -> None:
    data = fetch_something()                          # blocking I/O, off main thread
    self.app.call_from_thread(self._show_data, data)  # back to main thread

def _show_data(self, data: SomeData) -> None:
    self.data = data                                  # triggers watch_data

def watch_data(self, data: SomeData | None) -> None:
    self.query_one("#body", Static).update(render(data))
```

Use `ThreadPoolExecutor` for parallelising multiple I/O calls within a single `_load`.

### Global reactives (app.py)

| Reactive | Type | Purpose |
|---|---|---|
| `privacy` | `bool` | Masks sensitive values with `•••••` in Ghostfolio |
| `refresh_interval` | `int` | Seconds between auto-refreshes (30–3600, default 300) |

`watch_privacy` and `watch_refresh_interval` propagate changes to individual widgets. Use `always_update=True` on reactives that need to fire on every assignment (even same value).

### Keybindings

| Key | Action |
|---|---|
| `q` | Quit |
| `r` | Manual refresh (calls `_load()` on all data widgets) |
| `p` | Toggle privacy mode |
| `[` | Decrease refresh interval by 60 s |
| `]` | Increase refresh interval by 60 s |

---

## Coding conventions

### Imports (always in this order)

```python
from __future__ import annotations   # always first

# stdlib
import platform
from dataclasses import dataclass, field
from datetime import date, timedelta

# third-party
import requests
from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static
from textual import work

# local
from .. import config
from .base import DashWidget
```

### Data layer

- Use `@dataclass` for all data models; `field(default_factory=list)` for mutable defaults
- Frozen dataclasses for immutable value objects
- Keep computed properties on the dataclass (e.g., `@property def ok(self)`)
- Use `|` union syntax (not `Optional`), and `X | None` (not `Union[X, None]`)

### Rich renderables

| Need | Use |
|---|---|
| Inline styled text | `Text` with `.append(str, style=…)` |
| Layout-only grid | `Table.grid(expand=True, padding=(0, N))` with `ratio=1` or fixed `width=` columns |
| Multiple renderables stacked | `Group(r1, r2, …)` |
| Centred content | `Align.center(renderable)` |
| Horizontal divider | `Rule(style="dim")` |
| Progress bar | `ProgressBar(total=100, completed=pct, complete_style="green")` |
| Half-block pixel art | `▀` (top half lit), `▄` (bottom half lit), `█` (full), via `zip(top_row, bot_row)` |

Never pass raw markup strings to `Static.update()` — always use a Rich renderable.

### CSS

- Keep all CSS in `DEFAULT_CSS` on the widget class or in the app `CSS` string
- `content-align: center middle` for vertically-centred content — ensure the rendered `Text` has **no trailing newline**, or centering will be off
- Width: use `width: Nfr` (fractional) or `width: N` (fixed chars) or `width: N%`

### Error handling

- In `_load`, wrap everything in `try/except Exception` and call `self._show_error(str(exc))` on failure
- `_show_error` should update the body Static with `[red]Error:[/red] {msg}` and set `self._err`
- `watch_data` should bail early if `self._err` is set
- For optional features (e.g., speed section), hide the section entirely when the config is absent rather than showing a placeholder

### Comments

Write no comments unless the **why** is non-obvious. Avoid docstrings. Section separators (`# ── label ───`) are acceptable to break up long files.

---

## Environment variables

All variables are prefixed `TUIDASH_`. Copy `.env.example` to `.env` to configure.

| Variable | Required | Default | Description |
|---|---|---|---|
| `TUIDASH_THEME` | No | `textual-dark` | Textual theme name |
| `TUIDASH_REFRESH` | No | `300` | Auto-refresh interval in seconds |
| `TUIDASH_WEATHER_LOCATION` | Yes* | — | City name or `lat,lon` |
| `TUIDASH_WEATHER_UNITS` | No | `metric` | `metric` or `imperial` |
| `TUIDASH_GHOSTFOLIO_URL` | Yes* | — | Base URL of Ghostfolio instance |
| `TUIDASH_GHOSTFOLIO_TOKEN` | Yes* | — | Ghostfolio access token |
| `TUIDASH_HOLIDAY_CALENDAR` | No | — | ICS URL for public holidays |
| `TUIDASH_RSS_FEEDS` | No | — | Comma-separated RSS feed URLs |
| `TUIDASH_HOSTS` | No | — | Comma-separated Glances URLs |
| `TUIDASH_HETZNER_API_TOKEN` | No | — | Reserved for future VPS widget |
| `TUIDASH_REACHABILITY_IPS` | No | `1.1.1.1,8.8.8.8,192.168.1.1` | IPs to ping |
| `TUIDASH_RESOLVE_HOSTS` | No | `google.com,amazon.com,facebook.com` | Hosts to DNS-resolve |
| `TUIDASH_NETSPEED_DOWN` | No | `600` | Max Mbps for download bar |
| `TUIDASH_NETSPEED_UP` | No | `600` | Max Mbps for upload bar |
| `TUIDASH_SPEEDTESTTRACKER_URL` | No | — | Speedtest Tracker URL; hides speed section if unset |
| `TUIDASH_SPEEDTESTTRACKER_TOKEN` | No | — | Bearer token for Speedtest Tracker API |

*Required for that widget to function; missing values show an inline error, not a crash.

Config is loaded from `~/.config/tuidash/.env` first, then the project-local `.env`.

---

## Adding a new widget

1. Create `tuidash/widgets/mywidget.py`, subclassing `DashWidget`
2. Define data model as `@dataclass`
3. Implement `_load()` with `@work(thread=True)`; call `call_from_thread` on completion
4. Implement `watch_data()` to call the render function and update the Static
5. Implement `set_refresh_interval(seconds: int)` — stop old timer, start new one
6. Import and add to `app.py`: compose, CSS sizing, `border_title` in `on_mount`, and add `query_one(MyWidget)._load()` in `action_refresh`, `query_one(MyWidget).set_refresh_interval(value)` in `watch_refresh_interval`
7. Add new `TUIDASH_*` env vars to `.env.example`

---

## Dependencies

| Package | Purpose |
|---|---|
| `textual>=8.2.5` | TUI framework (layout, reactivity, async workers) |
| `requests>=2.33.1` | HTTP client (weather, Ghostfolio, Speedtest Tracker APIs) |
| `python-dotenv>=1.2.2` | `.env` file loading |

No other runtime dependencies. All other functionality (ping, DNS) uses the stdlib.
