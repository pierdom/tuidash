# CLAUDE.md

Personal terminal dashboard built with [Textual](https://textual.textualize.io/) + [Rich](https://rich.readthedocs.io/).

---

## Dev setup

```bash
uv sync          # install all dependencies into the managed venv
```

No tests and no linting config in `pyproject.toml`. Run the app directly to verify changes.

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
docker pull ghcr.io/pierdom/tuidash:latest
docker compose up -d
```

All dependencies are managed with `uv`. Never use `pip` directly.

---

## Project layout

```
Dockerfile             # python:3.13-slim + uv, serves on port 8080
docker-compose.yml     # mounts .env, maps port 8080, sets TUIDASH_SERVE_URL=http://localhost:8080
palettes/              # 10 bundled palettes; drop custom files here
tuidash/
├── app.py              # TuidashApp — navigation, global reactives, config loading, serve entry point
├── tuidash.png         # Bundled logo; resized to 64×64 and served as /favicon.ico in --serve mode
├── config.py           # Thin wrapper around python-dotenv (get / require)
├── ics.py              # ICS calendar parser (events)
├── scroll.py           # Shared boomerang-scroll helper (scroll_offset, scroll_window, current_tick)
├── theme.py            # Colour palette loader — reads palettes/<name>.toml, exports named constants + build_textual_theme()
├── influx.py           # InfluxDB v1 query API client (query/last) — backs the Homelab page
├── podcast_progress.py # ProgressStore — episode playback state persisted to ~/.local/share/tuidash/podcast_progress.json
├── screens/
│   ├── dashboard.py    # Page 1 — overview dashboard (all widgets)
│   ├── calendar.py     # Page 2 — Full-page monthly calendar with events
│   ├── news.py         # Page 3 — RelayWidget (left) + NewsReaderWidget (right), side by side
│   ├── podcasts.py     # Page 4 — Podcast feed viewer and player
│   ├── portfolio.py    # Page 5 — RelayWidget (left) + GhostfolioDetailWidget (right), side by side
│   └── homelab.py      # Page 6 — HomelabHostWidget + FleetStatusWidget + TailscaleWidget + HetznerWidget
└── widgets/
    ├── base.py         # DashWidget — base class for all widgets; also exports neon_bar()
    ├── clock.py        # Pixel-art half-block clock
    ├── calendar.py     # Monthly calendar with holiday/family/personal/work highlighting
    ├── cal_full.py     # Full-page monthly calendar with ICS event highlighting (Calendar page)
    ├── weather.py      # Open-Meteo weather + forecast
    ├── ghostfolio.py   # Ghostfolio portfolio tracker + live ticker
    ├── connectivity.py # Ping / DNS / speed-test connectivity checks
    ├── hosts.py        # Server monitoring via ping + Glances (CPU, MEM, Docker)
    ├── events.py       # 6-day calendar event list (today + 5 days); 4 cols, 5 above 128, 6 above 144 chars
    ├── news_ticker.py  # Single-row continuous news ticker (last 6 h, all RSS sources)
    ├── news_reader.py  # Full-page news reader used on page 2
    ├── relay.py        # Generic relay server feed widget (SSE + REST, per-topic)
    ├── podcasts.py     # Podcast feed viewer + mpv player (PodcastIndex API)
    ├── header.py       # App header bar: nav buttons (‹/›), net status, title (tap → page menu), play status, privacy lock (◉/○), clock
    ├── homelab.py      # HomelabHostWidget (per-host InfluxDB card) + FleetStatusWidget (fleet-wide connectivity/Docker/speed strip)
    ├── tailscale.py    # TailscaleWidget — device list from Tailscale API, 2 columns when there's room
    ├── hetzner.py      # HetznerWidget — server + storage list from Hetzner Cloud API
    ├── ghostfolio_detail.py  # GhostfolioDetailWidget — full portfolio breakdown + monthly activity
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
│   │   ├── #row-top  28%   │ ClockWidget(30) │ CalendarWidget(1fr) │ WeatherWidget(2fr) │
│   │   ├── #row-mid  auto  │ GhostfolioWidget(50%) │ Vertical: ConnectivityWidget + HostsWidget │
│   │   ├── #row-bot  1fr   │ EventsWidget(100%)                                            │
│   │   └── (sibling)  3    │ NewsTickerWidget(100%) — full-width, 1-row ticker             │
│   ├── NewsPage (BasePage)         ← page 2 — always mounted
│   │   └── Horizontal              │ RelayWidget("news", 1fr) │ NewsReaderWidget(1fr) │
│   ├── CalendarPage (BasePage)     ← page 3 — always mounted
│   ├── PodcastsPage (BasePage)     ← page 4 — always mounted
│   ├── PortfolioPage (BasePage)    ← page 5 — always mounted
│   │   └── Horizontal              │ RelayWidget("financial-analyst", 1fr) │ GhostfolioDetailWidget(1fr) │
│   └── HomelabPage (BasePage)      ← page 6 — always mounted
└── Footer
```

`#row-mid` and its three child widgets use `height: auto` — they shrink to content with no blank rows.

`NewsTickerWidget` is a **sibling of `#row-bot`** at the `DashboardPage` level (not nested inside it). This lets `EventsWidget` keep `height: 100%` inside `#row-bot`, while `#row-bot`'s `1fr` naturally leaves 3 rows at the bottom for the ticker.

### Multi-page navigation

Pages are defined in `_PAGES` in `app.py` as an ordered list of `(label, widget-id, class)` tuples. Add a new entry there to register a new page — no other changes needed.

- All pages are mounted once at startup and kept in the DOM; `ContentSwitcher` hides/shows via CSS `display`, so navigation is instant with no data reload
- Pages extend `BasePage` (`screens/__init__.py`). Pages that support privacy implement `set_privacy(value: bool)`; pages that support refresh implement `set_refresh_interval(seconds: int)` and `refresh_all()`. The App iterates `self.query(BasePage)` to propagate reactive changes; `action_refresh` targets only the currently visible page by ID.

Border chrome (`border-title-color`, `border-title-style`, `border-subtitle-color`) is defined once in `DashWidget.DEFAULT_CSS` and inherited by all widgets.

`DashWidget.DEFAULT_CSS` defines `DashWidget:focus, DashWidget:focus-within { border: round {ACCENT}; }` — border switches to accent colour when the widget or any descendant holds keyboard focus, providing TAB-navigation visual feedback.

`RelayWidget` sets its own `border_title` in `on_mount()` as `"  {self._title}"` — no external assignment needed.

### Widget contract

Every widget:
1. Inherits `DashWidget` (which inherits `textual.widget.Widget`)
2. Declares its own `DEFAULT_CSS` (height: 100%; child body height: 100%)
3. Sets `border_title` in the screen's `on_mount()`; sets `border_subtitle` itself when data arrives
4. Has a `set_refresh_interval(seconds: int)` method
5. Has a `_load()` method decorated with `@work(thread=True)` that fetches data and calls `self.app.call_from_thread(self._show_data, data)`

### Threading pattern

```python
@work(thread=True)
def _load(self) -> None:
    data = fetch_something()
    self.app.call_from_thread(self._show_data, data)

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
    try:
        sys.stdout.write("\033[?25h\033[?1049l\033[0m")  # show cursor, leave alt-screen, reset colors
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(0)
```

**Why `os._exit(0)`:** Textual's default `_shutdown()` waits for all widget message pumps to drain, which blocks until every in-flight `@work(thread=True)` worker finishes — up to the longest HTTP timeout. `os._exit(0)` bypasses this. It's safe because the terminal is already restored by `driver.stop_application_mode()` before `_shutdown()` is called; we only need `driver.close()` to flush the writer thread's queued escape sequences. The explicit `\033[0m` + `flush()` prevents custom background colours from bleeding into the shell prompt.

Widgets with long-lived background threads (e.g. `RelayWidget`'s SSE listener) should implement `on_unmount` to signal those threads to stop.

### Global reactives (app.py)

| Reactive | Type | Purpose |
|---|---|---|
| `privacy` | `bool` | Masks sensitive values with `•••••` in Ghostfolio |
| `refresh_interval` | `int` | Seconds between auto-refreshes (30–3600, default 300) |
| `_privacy_forced` | `bool` | Set by `TUIDASH_PRIVACY_FORCE`; makes the `p` toggle a no-op |
| `_privacy_default` | `bool` | Set by `TUIDASH_PRIVACY_DEFAULT`; enables auto-relock after 5 min when privacy is toggled off |
| `_relock_timer` | `Timer \| None` | Pending auto-relock timer; cancelled if privacy is re-enabled before it fires |

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
| `1`–`6` | Jump directly to page 1–6 (hidden from footer) |
| `Space` | Play/pause podcast (only active while mpv is running) |
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

**Exception — Homelab page widgets** (`HomelabHostWidget`, `TailscaleWidget`): their `ScrollableContainer`s are intentionally focusable to allow TAB-cycling and keyboard scrolling. This is safe because the Homelab page is never the initial focused page (it is page 6), so their bindings don't corrupt the footer order at startup.

### Mobile mode

**Mechanism:** `HORIZONTAL_BREAKPOINTS = [(0, "mobile"), (100, "wide")]` — Textual adds the `.mobile` CSS class to the `Screen` when width < 100. All mobile overrides in `app.py` use `.mobile` selector prefixes. No code branching needed; CSS handles everything.

**DashHeader tap navigation** (relevant for phone browsers via `--serve`):
- `‹` / `›` buttons — prev/next page
- Title tap — opens a `PageMenu` dropdown (`OptionList` overlay); current page highlighted
- Privacy lock (`◉`/`○`) tap — toggles privacy mode
- **Auto-relock:** if `TUIDASH_PRIVACY_DEFAULT=true` and user disables privacy, re-enables after 5 minutes

**Mobile layouts by widget/page:**

| Widget / page | Mobile change |
|---|---|
| `DashboardPage #row-top` | `layout: vertical` — `#clock-cal-pair` (Clock + Calendar side-by-side, `width: 100%`) stacks above WeatherWidget |
| `DashboardPage #row-mid` | `layout: vertical; height: auto` |
| `DashboardPage #row-bot` | `height: auto` |
| `ClockWidget` | `width: 1fr; height: 100%` — fills `#clock-cal-pair` height, matching CalendarWidget |
| `CalendarWidget` | `width: 1fr` |
| `WeatherWidget`, `GhostfolioWidget` | `width: 100%` |
| `#conn-hosts-col` | `width: 100%` |
| `EventsWidget` | `height: auto`; vertical day stacking with `─` separators between days |
| `CalFullWidget` (Calendar page) | Shows colored square indicators (■) per calendar instead of event text |
| `NewsPage` | `RelayWidget` + `NewsReaderWidget` stack vertically, 30/70 height split |
| `PodcastsPage #podcasts-grid` | `grid-size: 1` — single-column card list |
| `PortfolioPage` | `RelayWidget` + `GhostfolioDetailWidget` stack vertically, 30/70 height split |
| `HomelabPage #homelab-top` | `layout: vertical; height: auto` — host widgets stack vertically, each `height: 5` |

### Known pending issues — Dashboard mobile scroll

**Issue 1 — Scroll doesn't work on first visit.** The `.mobile` CSS class is applied during the first resize event (after `_on_mount`), so `show_vertical_scrollbar` starts `False` and Textual's `_scroll_to` bails out. Navigating away and back fixes it. The workaround (`_sync_scroll_mode` called from `on_resize` and `on_show → call_after_refresh`) sets `overflow_y = "scroll"` and `show_vertical_scrollbar = True` as inline Python styles, but the symptom persists.

**Issue 2 — Double scrollbar.** `#dashboard-scroll` shows two adjacent vertical lines in mobile mode. Suspected cause: `scrollbar-size-vertical` defaulting to 2 chars at mount before the `DEFAULT_CSS` override (`scrollbar-size-vertical: 1`) takes effect.

---

## Serving over HTTP (`--serve`)

`tuidash --serve [--host HOST] [--port PORT]` passes a `-u PUBLIC_URL` flag to `textual serve`. The public URL is embedded in the HTML served to the browser as the WebSocket endpoint — it must be reachable by the client, not just the server.

### Public URL detection priority

1. `TUIDASH_SERVE_URL` env var — explicit override, always wins. **Required in Docker.**
2. Tailscale IP (`100.x.x.x`) — detected by connecting a UDP socket to `100.100.100.100` (Tailscale Magic DNS).
3. LAN IP (`192.168.x.x`) — enumerated from `hostname -I` (Linux) or `ifconfig` (macOS/BSD).
4. Private IP (`10.x.x.x`) — covers many private VPN ranges.
5. Other private IPs (`172.x.x.x`).
6. `localhost` — last resort.

**Why Tailscale first, not a UDP probe to 8.8.8.8:** A probe to `8.8.8.8` picks whichever interface routes to the internet. If a VPN like ProtonVPN is active, that route goes through the VPN tunnel (e.g. `10.x.x.x`), which other Tailscale clients cannot reach. Connecting to Tailscale's own Magic DNS (`100.100.100.100`) instead gives the Tailscale interface IP regardless of the default route.

**Why not 0.0.0.0:** Chrome 94+ blocks WebSocket connections to `0.0.0.0` — the browser shows a "Textual App placeholder" instead of the dashboard.

**Docker:** The container gets a bridge IP unreachable from the host. `docker-compose.yml` hardcodes `TUIDASH_SERVE_URL=http://localhost:8080` so the browser connects to the host's mapped port instead.

### Favicon

The proxy intercepts `GET /favicon*` and serves `tuidash.png` resized to 64×64 via Pillow. It also injects `<link rel="icon" …>` into the HTML `<head>` via `_HEAD_INJECT`. If `tuidash.png` is missing or Pillow fails, the link tag is omitted and the browser falls back to its default.

---

## Coding conventions

### Imports

Order: `from __future__` → stdlib → third-party → local

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
| Blocky progress bar (fixed width) | `neon_bar(pct, width)` from `widgets/base.py` — gradient `█`/`░` bar using `BAR_LOW` (0–60%), `BAR_MID` (60–80%), `BAR_HIGH` (80–100%) palette colours |
| Fluid-width progress bar | Custom renderable implementing `__rich_console__` + `__rich_measure__`; use `options.max_width` inside `__rich_console__` and return `Measurement(1, options.max_width)`. Place in a `Table.grid` column with `ratio=1` so Rich supplies the exact remaining width at render time — avoids all manual offset arithmetic. |
| Half-block pixel art | `▀` / `▄` / `█` via `zip(top_row, bot_row)` |

Never pass raw markup strings to `Static.update()` — always use a Rich renderable.

### CSS

- Keep all CSS in `DEFAULT_CSS` on the widget class or in the app `CSS` string
- Width: use `width: Nfr` (fractional) or `width: N` (fixed chars) or `width: N%`
- When `DEFAULT_CSS` or `CSS` interpolates theme colours, make it an **f-string** and escape all literal `{`/`}` as `{{`/`}}`

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

All palette colours are centralised in `tuidash/theme.py`. Import named constants from there — never hardcode hex colours or Rich colour names in widget files:

```python
from ..theme import ACCENT, BORDER, HEADER_BG, BAR_LOW, BAR_MID, BAR_HIGH, BAR_BG
from ..theme import PERF_GREAT, PERF_GOOD, PERF_FLAT, PERF_BAD, PERF_POOR, PERF_TERRIBLE
```

`TUIDASH_PALETTE` accepts either a stem name (looks up `palettes/<name>.toml`) or an **absolute path** to any `.toml` file.

When a widget's `DEFAULT_CSS` needs a theme colour, convert to an **f-string** and escape all literal CSS braces as `{{`/`}}`:

```python
DEFAULT_CSS = f"""
MyWidget {{
    border: round {BORDER};
    border-title-color: {ACCENT};
}}
"""
```

Scrollbar colours are set globally in `App.CSS` using a `Widget { ... }` rule (not `Screen`). `Screen` only overrides the screen's own scrollbar; child `ScrollableContainer` widgets resolve their colour from `Widget.DEFAULT_CSS`. A `Widget` rule in App CSS sits above DEFAULT_CSS in Textual's cascade and covers all scrollable descendants.

Footer keyboard shortcut colours use Textual v8 component classes: `FooterKey .footer-key--key` and `FooterKey .footer-key--description` (the old `Footer > .footer--key` selector from ≤v7 no longer applies).

`OptionList`/`PageMenu` border: `OptionList.DEFAULT_CSS` sets `OptionList:focus { border: tall $border; }` which fires immediately on mount. Always override **both** the rest-state and the `:focus` state in `DEFAULT_CSS` with the palette `BORDER` colour; otherwise the Textual `$border` theme colour bleeds through.

### Palette ↔ Textual theme bridge

`theme.py` exports `build_textual_theme()`, which constructs a Textual `Theme` from the active palette constants. `TuidashApp.on_mount` registers it and sets it as the active Textual theme so built-in Textual widgets automatically inherit palette colours.

| Textual variable | Palette constant |
|---|---|
| `$primary` / `$accent` | `ACCENT` |
| `$background` | `HEADER_BG` |
| `$surface` / `$panel` | `BORDER` |
| `$warning` | `PERF_BAD` |
| `$error` | `PERF_TERRIBLE` |
| `$success` | `PERF_GREAT` |

`PERF_*` values are only forwarded when they are hex strings — bare Rich colour names are skipped, letting Textual fall back to its own defaults rather than crashing.

`TUIDASH_THEME` overrides the palette-derived theme after it is registered.

**Intentional non-palette colours:** weather condition icon colours and temperature gradient colours are hardcoded because they carry universal semantic meaning. ICS calendar colours (`TUIDASH_FAMILY_COLOR` etc.) are user-configurable Rich colour names.

Avoid `"blue"` as a Rich style — it renders as purple/violet in dark themes like `tokyo-night`.

---

## Environment variables

All variables are prefixed `TUIDASH_`. Copy `.env.example` to `.env` to configure.

| Variable | Default | Description |
|---|---|---|
| `TUIDASH_SERVE_URL` | auto-detected | Public URL for `--serve` WebSocket (required in Docker) |
| `TUIDASH_SERVE_MDNS` | `false` | Use `hostname.local` as the public URL for `--serve` (mDNS/Bonjour) |
| `TUIDASH_THEME` | — | Textual theme override; replaces the palette-derived theme if set |
| `TUIDASH_PALETTE` | `default` | Stem of a `.toml` file inside `palettes/`, or an absolute path to any `.toml` file |
| `TUIDASH_TRANSPARENT` | `false` | Use the terminal's default background (ANSI 49) for the base canvas so terminal transparency shows through; no effect over `--serve` |
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
| `TUIDASH_PERSONAL_COLOR` | `cyan` | Rich color name for personal event days |
| `TUIDASH_WORK_ICS` | — | ICS URL for work calendar events |
| `TUIDASH_WORK_COLOR` | `green` | Rich color name for work event days |
| `TUIDASH_RSS_FEEDS` | — | Comma-separated RSS feed URLs |
| `TUIDASH_NEWS_PICTURES` | `false` | Show article thumbnails in the News page; `true` enables image downloads |
| `TUIDASH_HOSTS` | — | Comma-separated Glances URLs (widget title: "Servers"; Dashboard page only — unrelated to the Homelab page below). `TUIDASH_HOMELAB_CENTRAL` is always prepended before these, sourced from InfluxDB instead of Glances. |
| `TUIDASH_INFLUXDB_URL` | — | InfluxDB base URL for the Homelab page (v1 query API, e.g. `http://192.168.1.101:8086`) |
| `TUIDASH_INFLUXDB_TOKEN` | — | InfluxDB API token — read-only on the bucket below is sufficient |
| `TUIDASH_INFLUXDB_ORG` | `geonlab` | InfluxDB org |
| `TUIDASH_INFLUXDB_BUCKET` | `homelab` | InfluxDB bucket queried by `tuidash/influx.py` |
| `TUIDASH_HOMELAB_CENTRAL` | `scarif` | The featured host card on the Homelab page — gets ZFS pools, backup freshness, and containers merged across every `scarif-*` Portainer environment |
| `TUIDASH_HOMELAB_HOSTS` | — | Comma-separated InfluxDB host tags for the other Homelab-page host cards (not URLs) |
| `TUIDASH_PORTAINER_HOST` | — | Portainer base URL (e.g. `https://portainer.example.com`) — container status for host cards + `FleetStatusWidget`, queried live, not via InfluxDB |
| `TUIDASH_PORTAINER_TOKEN` | — | Portainer API key (user settings → Access tokens), sent as `X-API-Key` |
| `TUIDASH_REACHABILITY_IPS` | `1.1.1.1,8.8.8.8,192.168.1.1` | IPs to ping |
| `TUIDASH_RESOLVE_HOSTS` | `google.com,amazon.com,facebook.com` | Hosts to DNS-resolve |
| `TUIDASH_DNS_RESOLVER` | system resolver | Custom DNS server IP for DNS checks (raw UDP on port 53) |
| `TUIDASH_NETSPEED_DOWN` | `600` | Max Mbps for download bar scaling |
| `TUIDASH_NETSPEED_UP` | `600` | Max Mbps for upload bar scaling |
| `TUIDASH_SPEEDTESTTRACKER_URL` | — | Speedtest Tracker URL; hides speed section if unset |
| `TUIDASH_SPEEDTESTTRACKER_TOKEN` | — | Bearer token for Speedtest Tracker API |
| `TUIDASH_RELAY_URL` | — | Base URL of the relay server instance (required for RelayWidget) |
| `TUIDASH_RELAY_TOKEN` | — | Bearer token for the relay API (required for RelayWidget) |
| `TUIDASH_PODCASTINDEX_KEY` | — | API key from https://api.podcastindex.org/ |
| `TUIDASH_PODCASTINDEX_SECRET` | — | API secret from https://api.podcastindex.org/ |
| `TUIDASH_PODCASTINDEX_IDS` | — | Comma-separated PodcastIndex feed IDs to display |
| `TUIDASH_TAILSCALE_KEY` | — | API key from https://login.tailscale.com/admin/settings/keys (must be `tskey-api-…`) |

Missing values for widget-specific vars show an inline error — they do not crash the app. Config is loaded from `~/.config/tuidash/.env` first, then the project-local `.env`.

---

## Widget notes

### GhostfolioWidget

- Base currency comes from `/api/v1/user` → `settings.baseCurrency`, not inferred from holdings
- Goal progress bar uses `_FluidNeonBar` — a custom Rich renderable placed in a `Table.grid` column with `ratio=1` that calls `neon_bar(pct, options.max_width)` inside `__rich_console__`, filling the exact remaining space without manual width arithmetic
- Ticker prev-close is cached per symbol keyed by calendar date — the full market history fetch (~540 KB/symbol) only happens once per day; subsequent refreshes compute the change from `marketPrice` in the holdings response vs the cached prev-close
- Performance `_perf_gradient_color(pct)` maps to `PERF_*` constants: `PERF_GREAT` (>+10%), `PERF_GOOD` (0–+10%), `PERF_FLAT` (−5–0%), `PERF_BAD` (−10–−5%), `PERF_POOR` (−20–−10%), `PERF_TERRIBLE` (<−20%)
- Live ticker `_ticker_color(pct)` → `PERF_FLAT` (±0.05%), `PERF_GREAT` (>2%), `PERF_GOOD` (0–2%), `PERF_POOR` (0–−2%), `PERF_TERRIBLE` (<−2%)

### WeatherWidget

- Forecast temperature bars use a 7-stop RGB gradient (`_TEMP_STOPS`: −5 °C deep blue → 0 °C sky blue → 8 °C cyan → 16 °C green → 24 °C yellow → 30 °C orange → 38 °C red), linearly interpolating between stops per bar position. Unfilled positions use `BAR_BG` (palette-aware).
- Temperature and weather condition icon colours are hardcoded hex (not palette-driven) because they carry universal semantic meaning; only `BAR_BG` follows the palette.
- **Night mode:** `sunrise` and `sunset` are fetched as `daily` parameters from Open-Meteo (`timezone: auto` → local time). When the current time is before sunrise or after sunset and the WMO condition is 0 or 1 (clear/sunny), the pixel art switches from the sun to a crescent moon (`"moon"` key in `_PIXELS`).

### GhostfolioDetailWidget

- Net worth progress bar uses `_FluidBar` — same `Table.grid ratio=1` pattern as `_FluidNeonBar`; renders `ACCENT`-coloured `█`/`░` blocks at `options.max_width`
- `_resize_pending` + `on_resize` trigger a redraw on the next ticker tick when the widget resizes, so width-dependent content re-renders at the new width

### ConnectivityWidget

- `TUIDASH_DNS_RESOLVER` sends a raw UDP DNS A-record query via `struct` + `socket`, bypassing the system resolver — no external library needed
- Speed section is hidden entirely when `TUIDASH_SPEEDTESTTRACKER_URL` is unset

### HostsWidget (border title: "Servers")

- One row per host, CPU + MEM bar only — the old per-host scrolling container list (removed 2026-09-19) is gone for good; what replaced it is one shared line, see below.
- `_name_from_url` returns the first hostname label for FQDNs (e.g. `myserver` from `myserver.local`); returns the full IP string for bare IP addresses (e.g. `192.168.1.1`, not `192`)
- Two data sources, chosen per host by `HostData.source`: `"glances"` (the `TUIDASH_HOSTS` list — tries Glances API v4 first, falls back to v3) and `"influx"` (`TUIDASH_HOMELAB_CENTRAL`, scarif by default — bare-metal Proxmox, no Glances agent, so CPU/mem is pulled via `_fetch_host_stats` reused straight from `widgets/homelab.py`). Both are still ICMP-pinged directly for reachability/RTT — only the CPU/mem source differs.
- The central host is always prepended first, so with the defaults (`TUIDASH_HOMELAB_CENTRAL=scarif`, `TUIDASH_HOSTS=bespin,endor`) the list renders scarif, bespin, endor in that order.
- Below the host rows, one boomerang-scrolling line (added 2026-09-19) shows a curated `_CRITICAL_SERVICES` shortlist (currently pihole@endor, proxy-manager@bespin, proxy-manager@scarif-proxy, ghostfolio/relay/pocket-id@bespin) — deliberately not "every container" (that's the Homelab page's job), just the ones worth a glance at all times. Sourced live from the Portainer API (`_fetch_portainer_endpoints`/`_fetch_portainer_containers`, reused from `widgets/homelab.py`), one call per unique environment in the list, not one per service. A container name in `_CRITICAL_SERVICES` that doesn't match Portainer's actual name renders as `status="missing"` (red) rather than silently vanishing — caught "npm" vs the real name `proxy-manager` this way during initial setup.
- Labels are `name@env` (`scarif-` prefix stripped, e.g. `proxy-manager@proxy`) since the same service name can legitimately exist in two environments (`proxy-manager` on both bespin and scarif-proxy).

### HomelabPage (`screens/homelab.py`)

- Layout (desktop, reworked 2026-09-20): `#homelab-top` is `1fr`, `#homelab-strip` and `#homelab-bottom` are both `auto` — so Tailscale/Hetzner shrink to their actual content instead of stretching to fill the page (that stretching used to leave a large dead gap below Hetzner on any terminal taller than their content needed), and the host-cards row claims whatever that frees up. Inside `#homelab-top`, scarif/bespin/endor stack vertically; scarif is content-sized (see `HomelabHostWidget` below) and of the remaining two, whichever is listed first in `TUIDASH_HOMELAB_HOSTS` gets the `homelab-other-host-primary` class (`height: 2fr` vs the other's plain `1fr`) — with the defaults (`bespin,endor`) that's bespin outranking endor for whatever room scarif doesn't take. This isn't cosmetic: bespin was silently clipping ~5 of its 15 containers (relay, telegraf, pocket-id, proxy-manager, syncthing) before this, visible only as a faint scrollbar sliver on the card's edge.
- All widgets except `FleetStatusWidget` have `_mobile_scrollable = True` — their inner `ScrollableContainer` stays scrollable in mobile mode; `FleetStatusWidget` has no `ScrollableContainer` (its content is always short enough to fit at `height: auto`)
- TAB / SHIFT+TAB cycles focus across all scrollable containers; the focused widget's border turns accent colour via `DashWidget:focus-within`

### HomelabHostWidget (`widgets/homelab.py`)

- CPU/mem/disk/ZFS/backups come from InfluxDB (`tuidash/influx.py`, bucket `homelab`), not Glances/ping — rebuilt 2026-09-18 once scarif (bare-metal Proxmox, no Glances agent) became the fleet's central host. See relay #319 for how those measurements are produced (Telegraf, Tailscale/speedtest scripts).
- Container status is **not** from InfluxDB — `_fetch_containers` hits the Portainer API live (`TUIDASH_PORTAINER_HOST`/`TOKEN`, added 2026-09-19), because the InfluxDB-side Portainer collector had silently died for hours and nobody noticed (0 unhealthy looked identical to "no data"). One `GET /api/endpoints` call maps environment name → id, then one `GET /api/endpoints/{id}/docker/containers/json?all=true` per environment; health is parsed out of Docker's `Status` string (`"(healthy)"`/`"(unhealthy)"`/`"health: starting"`) since the Portainer JSON doesn't surface it as its own field.
- `reachable` means "has sent a `cpu` point in the last 3 minutes", not ICMP reachability.
- Dynamic height split (added 2026-09-20, after `#homelab-scarif { height: 65% }` kept leaving a visible blank gap when scarif's content was shorter than its fixed share): `HomelabHostWidget` overrides `_sync_scroll_mode` so the **central** card's inner `#host-scroll` gets `height: auto; max-height: 24` instead of the base class's `height: 1fr` — it sizes to its own content (still internally scrollable past 24 rows), on **both desktop and mobile** (an earlier version of this only applied it on desktop, which left scarif silently clipped to mobile's flat 9-row cap — missing `vzdump` and its entire container grid with no visual indication anything was cut off; caught from a live screenshot, not something a headless test would show). `#homelab-scarif { height: auto }` (screen CSS, both modes) lets the outer widget shrink/grow to match. Bespin/endor are matched by the **`homelab-other-host` class**, not the bare `HomelabHostWidget` type — deliberate, so the mobile flat-cap rule (`.mobile .homelab-other-host { height: 9 }`) can't also match and re-clip scarif regardless of CSS specificity edge cases. On desktop bespin/endor keep the base class's `1fr`, growing into whatever scarif doesn't use.
- `central=True` (the `TUIDASH_HOMELAB_CENTRAL` host, scarif by default) additionally shows: the `tank`/`scratch` ZFS pools (reusing the `DiskInfo` bar rendering — same visual treatment as a root filesystem), a backup-freshness block (sanoid snapshot / borg offsite / vzdump, one canary dataset each — full per-dataset detail lives on the Scarif v3 Grafana dashboard, not here), and containers aggregated across every `scarif-*` Portainer environment (prefixed `env/container`, e.g. `apps/searxng`) with the ever-present `portainer_agent` filtered out as noise.
- Non-central hosts (`TUIDASH_HOMELAB_HOSTS`) show just CPU/mem/root-disk gauges and that host's own Portainer environment containers, unprefixed.
- Containers render via `_render_containers_grid`: column count scales with available width (`width // _COL_W`, capped at `_MAX_COLS = 6`), filled left-to-right round-robin — not a fixed 2 columns. Portainer gives no per-container CPU/mem, so there are no bar columns to size around; a badge + name is all each column needs, which is what lets a wide terminal fit scarif's 21 containers in ~3-4 rows instead of 11.
- `_render_host_body(hd, width)` receives `self.size.width` from `_redraw()` so the column layout responds to resize

### FleetStatusWidget (`widgets/homelab.py`)

- A simplified TUI condensation of the Grafana "Homelab" dashboard's Connectivity + Docker services rows, added 2026-09-19 — aggregated fleet-wide instead of per-host.
- Three columns (`Table.grid`, ratio 2:2:3): **Connectivity** — one `●`/`○` dot per host in `_FLEET_HOSTS` (scarif, bespin, endor, malachor) from InfluxDB, laid out as a fixed 2-column grid (not a wrapped `Text` line — wrapping mid-badge looked broken at narrow widths); **Docker** — unhealthy/stopped/running counts summed straight from every endpoint's `Snapshots[0]` in one `GET /api/endpoints` Portainer call (cheap — no per-container fetch, so it can't hit the old bespin 502); **Speed** — latest `speedtest` InfluxDB measurement (`download_bits`/`upload_bits`/`ping`, written natively by speedtest-tracker — queried directly, no HTTP API call), ↓/↑ stacked as separate lines (not side-by-side cells — side-by-side overflowed to `…` when the outer column got squeezed by its neighbours).
- Bar colour on the speed lines is `PERF_GREAT`/`PERF_GOOD`/`PERF_TERRIBLE` (green-family, high-is-good), not `BAR_HIGH`/`MID`/`LOW` (which mean high-is-bad, e.g. CPU load) — a real bug caught during review, since the two palettes share the same "gradient bar" shape but opposite semantics.
- The old "bespin's per-container Portainer call always 502s" issue (relay #319) turned out to be transient/since-fixed, not permanent — confirmed 2026-09-19, bespin's containers now fetch fine like every other environment.

### TailscaleWidget (`widgets/tailscale.py`)

- Devices only — the VIP services table (and its `_tcp_reachable` TCP-probe fetch) was dropped 2026-09-19 to keep the widget shorter, freeing vertical space on the Homelab page for the host cards.
- Non-mobile with >1 device: devices split into two side-by-side columns (first half / second half of the sorted list, not round-robin), each with its own header row — halves the height a 16-device tailnet needs.
- Exit-node devices show a ` ↗` suffix in bold accent colour; name truncated to 13 chars to fit within the 16-char column
- `TsDevice.exit_node` is derived from `advertisedRoutes` containing `"0.0.0.0/0"`
- `on_resize` uses `call_after_refresh(self._redraw)` to ensure `.mobile` class is already applied before checking it

### HetznerWidget (`widgets/hetzner.py`)

- `min_width=0` on the flexible traffic/used column ensures COST/MO is always visible even on very narrow screens
- `HetznerWidget` has no `ScrollableContainer` (intentional — `height: auto` widget expands to content; a SC caused height inflation)
- Total monthly cost shown in `border_subtitle` alongside running/storage counts — no extra vertical space
- **Mobile mode:** `host_w=12` (vs 30 wide), `name_w=10` (vs 16); IP column dropped for servers

### CalendarWidget

- Day highlight priority: today > holiday (red) > family > personal > work > weekend
- Weekend column headers use `dim {ACCENT} on {BORDER}` — palette-aware (previously a hardcoded 256-color index)
- Calendar grid updates every 60 s regardless of refresh interval (no network dependency); ICS feeds refresh at `TUIDASH_REFRESH` rate
- **`CalFullWidget`** weekend columns use `on {BORDER}` as background tint — palette-aware (previously a hardcoded `color(237)` 256-color index)

### RelayWidget

- SSE stream parsed with `iter_content` so blank-line event delimiters are never swallowed
- SSE reconnects with exponential backoff (2 s → 60 s cap); sends `Last-Event-ID` header on reconnect to replay missed posts
- Both SSE and REST paths merge through `_merge_posts` (dedup by `id`, sorted newest-first) on the main thread
- Markdown rendered via `_PaletteMarkdown` — subclass of `rich.markdown.Markdown` that pushes a Rich `Theme` overlay in `__rich_console__`, mapping `markdown.h1`–`h4`, `markdown.h1.border`, `markdown.code`, and `markdown.link` to palette `ACCENT`, replacing Rich's hardcoded yellow/cyan/bright_blue defaults

### PodcastsWidget

- Playback via `_MpvPlayer` — thin wrapper around `mpv --no-video --input-ipc-server=/tmp/tuidash-mpv.sock` (Unix socket IPC for seek/pause without restarting the process)
- Episode playback position stored in `~/.local/share/tuidash/podcast_progress.json` keyed by episode GUID + date; resumes from last position on re-open
- Missing `mpv` binary: error toast shown, all other functionality unaffected
- Episode badges: `● LATEST` (idx 0, newest in feed), `● OLDEST` (last idx, oldest in feed)
- Sub-widgets (`PlaybackBar`, `PodcastCard`) use f-string `DEFAULT_CSS` with `BORDER`/`ACCENT` rather than Textual's `$panel`/`$accent` — required because these widgets are composed before the Textual theme is registered

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
| `pillow>=11.0.0` | Half-block pixel art thumbnails in the News page reader; favicon resizing in `--serve` mode |

Ping, DNS, and IP detection use only the stdlib.

`mpv` (system package) is required for podcast playback. The widget shows an error toast if mpv is not found; all other functionality works without it.
