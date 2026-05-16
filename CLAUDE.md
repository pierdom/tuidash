# tuidash — Claude Code guide

Personal terminal dashboard built with [Textual](https://textual.textualize.io/) + [Rich](https://rich.readthedocs.io/).

---

## Running the project

```bash
uv run tuidash                        # terminal
uv run tuidash --serve                # browser at http://localhost:8080
uv run tuidash --serve --port 9000    # custom port
uv run python -m tuidash.app          # alternative (terminal only)
```

`--serve` invokes `textual serve -c "{venv_python} -m tuidash.app" -h HOST -p PORT -u PUBLIC_URL` via subprocess. The `textual` binary comes from the `textual-dev` dependency.

```bash
# Docker (serves on http://localhost:8080)
docker compose up --build
docker compose up -d      # detached
```

All dependencies are managed with `uv`. Never use `pip` directly.

---

## Project layout

```
Dockerfile             # python:3.13-slim + uv, serves on port 8080
docker-compose.yml     # mounts .env, maps port 8080, sets TUIDASH_SERVE_URL=http://localhost:8080
tuidash/
├── app.py              # TuidashApp — navigation, global reactives, config loading, serve entry point
├── config.py           # Thin wrapper around python-dotenv (get / require)
├── ics.py              # ICS calendar parser (events)
├── screens/
│   ├── dashboard.py    # Page 1 — overview dashboard (all widgets)
│   ├── news.py         # Page 2 — RelayWidget (left) + NewsReaderWidget (right), side by side
│   └── calendar.py     # Page 3 — Full-page monthly calendar with events
└── widgets/
    ├── base.py         # DashWidget — base class for all widgets
    ├── clock.py        # Pixel-art half-block clock
    ├── calendar.py     # Monthly calendar with holiday/family/personal/work highlighting
    ├── weather.py      # Open-Meteo weather + forecast
    ├── ghostfolio.py   # Ghostfolio portfolio tracker + live ticker
    ├── connectivity.py # Ping / DNS / speed-test connectivity checks
    ├── hosts.py        # Server monitoring via ping + Glances (CPU, MEM, Docker)
    ├── events.py       # 4-day calendar event list (today + 3 days) from ICS feeds
    ├── news_ticker.py  # Single-row continuous news ticker (last 6 h, all RSS sources)
    ├── news_reader.py  # Full-page news reader used on page 2
    ├── relay.py        # Generic relay server feed widget (SSE + REST, per-topic)
    ├── net_header.py   # Custom header bar: network status + title + clock
    └── rss.py          # RSS feed-fetching library (FeedData, _fetch_feed, _parse_dt)
```

`main.py` in the repo root is an unused stub — the real entry point is `tuidash.app:main`.

---

## Architecture

### App layout (CSS-driven)

```
TuidashApp (App)                    ← navigation, global reactives, config
├── Header
├── ContentSwitcher                 ← shows one page at a time (CSS display toggle, no remounting)
│   ├── DashboardPage (BasePage)    ← page 1 — always mounted
│   │   ├── #row-top  28%   │ ClockWidget(30) │ WeatherWidget(2fr) │ CalendarWidget(1fr) │
│   │   ├── #row-mid  auto  │ GhostfolioWidget(50%) │ Vertical: ConnectivityWidget + HostsWidget │
│   │   ├── #row-bot  1fr   │ EventsWidget(100%)                                            │
│   │   └── (sibling)  3    │ NewsTickerWidget(100%) — full-width, 1-row ticker             │
│   ├── NewsPage (BasePage)         ← page 2 — always mounted
│   │   └── Horizontal              │ RelayWidget("news", 1fr) │ NewsReaderWidget(1fr) │
│   └── CalendarPage (BasePage)     ← page 3 — always mounted
└── Footer
```

`#row-mid` and the three widgets it contains (Ghostfolio, Connectivity, Servers) use `height: auto` — they shrink to their content with no blank rows.

`NewsTickerWidget` is a **sibling of `#row-bot`** at the `DashboardPage` level (not nested inside it). This lets `EventsWidget` keep `height: 100%` inside `#row-bot`, while `#row-bot`'s `1fr` naturally leaves 3 rows at the bottom for the ticker.

### Multi-page navigation

Pages are defined in `_PAGES` in `app.py` as an ordered list of `(label, widget-id, class)` tuples. Add a new entry there to register a new page — no other changes needed.

- All pages are mounted once at startup and kept in the DOM; `ContentSwitcher` hides/shows them via CSS `display`, so navigation is instant with no data reload
- `←` / `→` arrow keys cycle through pages (wraps); bindings are on the App so they work everywhere
- The app `sub_title` shows `[n/total] PageName  ↻ Xs` and `PRIVATE MODE` when active
- Pages extend `BasePage` (`screens/__init__.py`). Pages that support privacy implement `set_privacy(value: bool)`; pages that support refresh implement `set_refresh_interval(seconds: int)` and `refresh_all()`. The App iterates `self.query(BasePage)` to propagate reactive changes to all pages; `action_refresh` targets only the currently visible page by ID.

Widget border titles set in `dashboard.on_mount()`:
- Clock, Weather, Calendar, Ghostfolio, Connectivity, **Servers** (HostsWidget), Events, **News** (NewsTickerWidget)

`RelayWidget` sets its own `border_title` in `on_mount()` as `"  Relay ({topic})"` — no external assignment needed.

### Widget contract

Every widget:
1. Inherits `DashWidget` (which inherits `textual.widget.Widget`)
2. Declares its own `DEFAULT_CSS` (height: 100%; child body height: 100%)
3. Sets `border_title` in `app.on_mount()`; sets `border_subtitle` itself when data arrives
4. Has a `set_refresh_interval(seconds: int)` method — called by the app when the global interval changes or the user presses `[` / `]`
5. Has a `_load()` method decorated with `@work(thread=True)` that fetches data and calls `self.app.call_from_thread(self._show_data, data)`

All data widgets (including `CalendarWidget`) are wired into both `watch_refresh_interval` and `action_refresh`.

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

**Important:** `with ThreadPoolExecutor(...) as pool:` calls `shutdown(wait=True)` on `__exit__`, so all futures are complete after the block ends. Calling `.result()` after the `with` block is safe and intentional.

Textual stops all `set_interval` timers automatically on widget unmount — no manual `on_unmount` cleanup needed.

### Shutdown pattern

`TuidashApp` overrides `_shutdown()` to exit instantly instead of waiting for in-flight HTTP requests:

```python
async def _shutdown(self) -> None:
    if self._driver is not None:
        try:
            self._driver.close()  # joins writer thread → flushes queued escape sequences
        except Exception:
            pass
    os._exit(0)
```

**Why:** Textual's `@work(thread=True)` workers run via `asyncio`'s default executor (`run_in_executor`). On quit, `_shutdown()` calls `_close_all()` which waits for every widget's message pump to drain — but pumps can't close until in-flight workers stop posting messages, which can take as long as the longest HTTP timeout. `os._exit(0)` bypasses this entirely.

**Why it's safe:** The terminal is restored by `driver.stop_application_mode()` inside `_process_messages()` *before* `_shutdown()` is called. We only need to join the writer thread (`driver.close()`) so the queued alt-screen-off escape sequences are actually flushed to the terminal before the process dies.

Widgets with long-lived background threads (e.g. `RelayWidget`'s SSE listener) should still implement `on_unmount` to set a stop event and close any open response, so those threads exit cleanly if Textual ever manages to drain the pumps (e.g. in test mode).

### Global reactives (app.py)

| Reactive | Type | Purpose |
|---|---|---|
| `privacy` | `bool` | Masks sensitive values with `•••••` in Ghostfolio |
| `refresh_interval` | `int` | Seconds between auto-refreshes (30–3600, default 300) |
| `_privacy_forced` | `bool` | Set by `TUIDASH_PRIVACY_FORCE`; makes the `p` toggle a no-op |

`watch_privacy` and `watch_refresh_interval` propagate changes to individual widgets. Use `always_update=True` on reactives that need to fire on every assignment (even same value).

### Keybindings

| Key | Action |
|---|---|
| `q` | Quit |
| `r` | Manual refresh (delegates to active screen's `refresh_all()`) |
| `p` | Toggle privacy mode (no-op when `_privacy_forced` is True) |
| `[` | Decrease refresh interval by 60 s |
| `]` | Increase refresh interval by 60 s |
| `←` | Previous page (wraps) |
| `→` | Next page (wraps) |
| `,` | Previous month (Calendar page only) |
| `.` | Next month (Calendar page only) |

### Footer binding architecture

**All bindings live in `TuidashApp.BINDINGS`.** This is the single source of truth for the footer display order. Page-specific bindings are never declared on screens or widgets — doing so would cause the footer to reorder when that page is focused, because Textual builds the active bindings dict by walking the focus chain from innermost widget outward.

**Page-specific bindings** (e.g. `,`/`.` for the Calendar page) are included in `App.BINDINGS` and gated with `check_action`:

```python
def check_action(self, action: str, parameters: tuple) -> bool | None:
    if action in ("prev_month", "next_month"):
        return _PAGES[self._page_idx][1] == "page-calendar"
    return True
```

Returning `False` hides the binding from the footer and disables it. The action handler on the App then delegates to the relevant page widget.

**Navigation bindings** (`←`/`→` and `,`/`.`) use `priority=True` so they fire and display even when a descendant widget claims the same key (e.g. a `ScrollableContainer`'s scroll bindings).

**Display-only `ScrollableContainer`s** must have `can_focus = False` (set in `compose()`). If they are focusable, they grab initial focus at startup and their scroll bindings appear first in the binding dict; when the App's `priority=True` bindings later replace those entries, they inherit the wrong (first) position in the footer rather than their definition-order position.

```python
def compose(self) -> ComposeResult:
    with ScrollableContainer() as sc:
        sc.can_focus = False
        yield Static(...)
```

---

## Serving over HTTP (`--serve`)

`tuidash --serve [--host HOST] [--port PORT]` passes a `-u PUBLIC_URL` flag to `textual serve`. The public URL is embedded in the HTML served to the browser as the WebSocket endpoint — it must be reachable by the client, not just the server.

### Public URL detection priority

1. `TUIDASH_SERVE_URL` env var — explicit override, always wins. **Required in Docker.**
2. Tailscale IP (`100.x.x.x`) — detected by connecting a UDP socket to `100.100.100.100` (Tailscale Magic DNS). If the socket's local address starts with `100.`, that's the Tailscale interface.
3. LAN IP (`192.168.x.x`) — enumerated from `hostname -I` (Linux) or `ifconfig` (macOS/BSD).
4. Private IP (`10.x.x.x`) — covers many private VPN ranges (e.g. ProtonVPN).
5. Other private IPs (`172.x.x.x`).
6. `localhost` — last resort.

**Why Tailscale first, not a UDP probe to 8.8.8.8:** A probe to `8.8.8.8` picks whichever interface routes to the internet. If a VPN like ProtonVPN is active, that route goes through the VPN tunnel (e.g. `10.x.x.x`), which other Tailscale clients cannot reach. Connecting to Tailscale's own Magic DNS (`100.100.100.100`) instead gives the Tailscale interface IP regardless of the default route.

**Why not 0.0.0.0:** Chrome 94+ blocks WebSocket connections to `0.0.0.0` — the browser shows a "Textual App placeholder" instead of the dashboard.

**Docker:** The container gets a bridge IP (e.g. `172.20.0.2`) which is unreachable from the host. `docker-compose.yml` hardcodes `TUIDASH_SERVE_URL=http://localhost:8080` so the browser connects to the host's mapped port instead.

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
- Keep computed properties on the dataclass (e.g., `@property def ok(self)`)
- Use `|` union syntax (not `Optional`), and `X | None` (not `Union[X, None]`)

### Rich renderables

| Need | Use |
|---|---|
| Inline styled text | `Text` with `.append(str, style=…)` |
| Layout-only grid | `Table.grid(expand=True, padding=(0, N))` with `ratio=1` or fixed columns |
| Multiple renderables stacked | `Group(r1, r2, …)` |
| Centred content | `Align.center(renderable)` |
| Horizontal divider | `Rule(style="dim")` |
| Progress bar | `ProgressBar(total=100, completed=pct, complete_style="green")` |
| Half-block pixel art | `▀` / `▄` / `█` via `zip(top_row, bot_row)` |

Never pass raw markup strings to `Static.update()` — always use a Rich renderable.

### CSS

- Keep all CSS in `DEFAULT_CSS` on the widget class or in the app `CSS` string
- Width: use `width: Nfr` (fractional) or `width: N` (fixed chars) or `width: N%`

### Error handling

- In `_load`, wrap everything in `try/except Exception` and call `self._show_error(str(exc))` on failure
- `_show_error` should update the body Static with `[red]Error:[/red] {msg}` and set `self._err`
- `watch_data` should bail early if `self._err` is set
- For optional features (e.g., speed section), hide the section entirely when the config is absent

### Comments

Write no comments unless the **why** is non-obvious. Section separators (`# ── label ───`) are acceptable to break up long files.

### Marquee / ticker scrolling

**Boomerang (RssWidget, HostsWidget):**
```
_SCROLL_INTERVAL = 0.24   # seconds per step
_PAUSE_L_TICKS   = round(15 / _SCROLL_INTERVAL)   # ≈15 s pause at left end
_PAUSE_R_TICKS   = round(3  / _SCROLL_INTERVAL)   # ≈3 s pause at right end

4-phase cycle: pause-left → scroll-right → pause-right → scroll-left
```

**Continuous left-scroll (GhostfolioWidget ticker):**
```
_TICKER_INTERVAL = 0.125   # seconds per step (≈8 chars/sec)
offset = tick % full_len   # wraps seamlessly using doubled segment list
```

### Theme colours

Avoid `"blue"` as a Rich style — it renders as purple/violet in dark themes like `tokyo-night`. Use `""` (default text colour) for neutral running containers.

---

## Environment variables

All variables are prefixed `TUIDASH_`. Copy `.env.example` to `.env` to configure.

| Variable | Default | Description |
|---|---|---|
| `TUIDASH_SERVE_URL` | auto-detected | Public URL for `--serve` WebSocket (required in Docker) |
| `TUIDASH_THEME` | `textual-dark` | Textual theme name |
| `TUIDASH_REFRESH` | `300` | Auto-refresh interval in seconds |
| `TUIDASH_PRIVACY_DEFAULT` | `false` | Start in privacy mode; `p` toggle still works |
| `TUIDASH_PRIVACY_FORCE` | `false` | Force privacy mode on startup; disables `p` toggle |
| `TUIDASH_WEATHER_LOCATION` | — | City name or `lat,lon` |
| `TUIDASH_WEATHER_UNITS` | `metric` | `metric` (°C/km/h) or `imperial` (°F/mph) |
| `TUIDASH_GHOSTFOLIO_URL` | — | Base URL of Ghostfolio instance |
| `TUIDASH_GHOSTFOLIO_TOKEN` | — | Ghostfolio anonymous access token |
| `TUIDASH_GHOSTFOLIO_GOAL` | `1000000` | Portfolio goal for the progress bar (uses Ghostfolio base currency) |
| `TUIDASH_HOLIDAY_CALENDAR` | — | ICS URL for public holidays |
| `TUIDASH_FAMILY_ICS` | — | ICS URL for family calendar events |
| `TUIDASH_FAMILY_COLOR` | `yellow` | Rich color name for family event days |
| `TUIDASH_PERSONAL_ICS` | — | ICS URL for personal calendar events |
| `TUIDASH_PERSONAL_COLOR` | `teal` | Rich color name for personal event days |
| `TUIDASH_WORK_ICS` | — | ICS URL for work calendar events |
| `TUIDASH_WORK_COLOR` | `green` | Rich color name for work event days |
| `TUIDASH_RSS_FEEDS` | — | Comma-separated RSS feed URLs |
| `TUIDASH_NEWS_PICTURES` | `false` | Show article thumbnails in the News page; `true` enables image downloads |
| `TUIDASH_HOSTS` | — | Comma-separated Glances URLs (widget title: "Servers") |
| `TUIDASH_REACHABILITY_IPS` | `1.1.1.1,8.8.8.8,192.168.1.1` | IPs to ping |
| `TUIDASH_RESOLVE_HOSTS` | `google.com,amazon.com,facebook.com` | Hosts to DNS-resolve |
| `TUIDASH_DNS_RESOLVER` | system resolver | Custom DNS server IP for DNS checks (raw UDP on port 53) |
| `TUIDASH_NETSPEED_DOWN` | `600` | Max Mbps for download bar scaling |
| `TUIDASH_NETSPEED_UP` | `600` | Max Mbps for upload bar scaling |
| `TUIDASH_SPEEDTESTTRACKER_URL` | — | Speedtest Tracker URL; hides speed section if unset |
| `TUIDASH_SPEEDTESTTRACKER_TOKEN` | — | Bearer token for Speedtest Tracker API |
| `TUIDASH_RELAY_URL` | — | Base URL of the relay server instance (required for RelayWidget) |
| `TUIDASH_RELAY_TOKEN` | — | Bearer token for the relay API (required for RelayWidget) |

Missing values for widget-specific vars show an inline error — they do not crash the app.

Config is loaded from `~/.config/tuidash/.env` first, then the project-local `.env`.

---

## Widget notes

### GhostfolioWidget

- Fetches 6 endpoints in parallel: portfolio performance ×3, holdings, orders, user settings
- Base currency comes from Ghostfolio user settings (`/api/v1/user` → `settings.baseCurrency`), not inferred from holdings
- Goal label is compact: `1M`, `500K`, `2.5M`, etc.
- Live ticker at the bottom shows today's % change per equity, colour-coded: `bright_green` (>2%), `green` (0–2%), `yellow` (flat ±0.05%), `red` (0–−2%), `bright_red` (<−2%)
- Ticker prev-close is cached per symbol keyed by calendar date — the full market history fetch (~540 KB/symbol) only happens once per day; subsequent refreshes compute the change from `marketPrice` in the holdings response vs the cached prev-close

### ConnectivityWidget

- `TUIDASH_DNS_RESOLVER` sends a raw UDP DNS A-record query via `struct` + `socket`, bypassing the system resolver — no external library needed
- Speed section is hidden entirely when `TUIDASH_SPEEDTESTTRACKER_URL` is unset

### HostsWidget (border title: "Servers")

- `_name_from_url` returns the first hostname label for FQDN hosts (e.g. `myserver` from `myserver.local`); returns the full IP string for bare IP addresses (e.g. `192.168.1.1`, not `192`)
- Glances API: tries v4 (`/api/4/`) first, falls back to v3 (`/api/3/`)
- Container colours: `green` (healthy), `red` (unhealthy), `dim` (not running), `""` default (running, no healthcheck)

### CalendarWidget

- Supports up to four ICS feeds: public holidays (`TUIDASH_HOLIDAY_CALENDAR`), family (`TUIDASH_FAMILY_ICS`), personal (`TUIDASH_PERSONAL_ICS`), work (`TUIDASH_WORK_ICS`)
- Day highlight priority: today > holiday (red) > family > personal > work > weekend; each custom calendar has its own configurable Rich color
- All ICS feeds refresh at the same rate as `TUIDASH_REFRESH` (wired to `set_refresh_interval`)
- Calendar grid updates every 60 s regardless of refresh interval (no network dependency)
- Manual `r` triggers `_load()`, which re-fetches all four ICS feeds in parallel (holiday, family, personal, work)

### EventsWidget

- Shows upcoming events from the three user ICS feeds (family/personal/work) in a 4-day view: today + next 3 days
- Each day column scrolls long event names using boomerang scroll (same `scroll_window` helper as `RssWidget`)
- Uses `TUIDASH_FAMILY_ICS`, `TUIDASH_PERSONAL_ICS`, `TUIDASH_WORK_ICS` and their corresponding `_COLOR` vars
- Occupies `#row-bot` (full width, `1fr` height) on the dashboard

### NewsTickerWidget

- Reads the same `TUIDASH_RSS_FEEDS` as `rss.py`; imports `FeedData`, `_fetch_feed`, `_parse_dt` from `rss.py`
- Filters to articles published in the last 6 hours; shows nothing if no recent articles
- Continuous left-scroll: `tick % full_len` offset over a doubled segment list — same technique as Ghostfolio ticker
- Format per item: `◆   SourceName  Headline title`; source name is `bold {color}`, title is `{color}`
- `height: 3` (border + 1 content row); sits as a sibling of `#row-bot` at the `DashboardPage` level

### RelayWidget

- Generic Markdown feed widget backed by a self-hosted relay server (see [pierdom/relay](https://github.com/pierdom/relay))
- Instantiated with a `topic` (tag name) and an optional `title`; border title defaults to `Relay ({topic})`
- Multiple instances with different topics can coexist on the same page
- **Live updates via SSE**: connects to `GET /events?tag={topic}`, parses the stream with `iter_content` (so blank-line event delimiters are never swallowed), and calls `call_from_thread` on each `post` event
- **Seed + fallback via REST**: `GET /posts?tag={topic}&limit=20` on mount and on each periodic refresh tick; response shape is `{"items": [...], "total": N, ...}`
- Both paths merge through `_merge_posts` (dedup by `id`, sorted newest-first) on the main thread
- SSE reconnects with exponential backoff (2 s → 60 s cap); sends `Last-Event-ID` header on reconnect to replay missed posts
- Missing `TUIDASH_RELAY_URL` or `TUIDASH_RELAY_TOKEN`: `_load()` shows an inline error; `_listen()` exits immediately without retrying
- Currently placed on **NewsPage** (page 2) as a `1fr`-wide left panel beside `NewsReaderWidget`

---

## Adding a new widget

1. Create `tuidash/widgets/mywidget.py`, subclassing `DashWidget`
2. Define data model as `@dataclass`
3. Implement `_load()` with `@work(thread=True)`; call `call_from_thread` on completion
4. Implement `watch_data()` to call the render function and update the Static
5. Implement `set_refresh_interval(seconds: int)` — stop old timer, start new one
6. Import and add to `dashboard.py` (or the relevant screen):
   - `compose()` — yield the widget
   - CSS sizing in `DEFAULT_CSS`
   - `border_title` in `on_mount()`
   - `query_one(MyWidget)._load()` in `refresh_all()`
   - `query_one(MyWidget).set_refresh_interval(value)` in `set_refresh_interval()`
7. Add new `TUIDASH_*` env vars to `.env.example` and the table above

---

## Dependencies

| Package | Purpose |
|---|---|
| `textual>=8.2.5` | TUI framework (layout, reactivity, async workers) |
| `textual-dev>=1.7.0` | Provides the `textual serve` binary used by `--serve` |
| `requests>=2.33.1` | HTTP client (weather, Ghostfolio, Speedtest Tracker, Glances, RSS) |
| `python-dotenv>=1.2.2` | `.env` file loading |
| `pillow>=11.0.0` | Half-block pixel art thumbnails in the News page reader |

Ping, DNS, and IP detection use only the stdlib.
