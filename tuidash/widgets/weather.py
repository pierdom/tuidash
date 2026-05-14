from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import requests
from rich.align import Align
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static
from textual import work

from .. import config
from .base import DashWidget


# ── WMO weather code tables ────────────────────────────────────────────────────

_DESC: dict[int, str] = {
    0: "Clear",        1: "Sunny",         2: "Cloudy",
    3: "Overcast",    45: "Fog",           48: "Icy fog",
   51: "Drizzle",     53: "Drizzle",       55: "Drizzle",
   61: "Lt. rain",    63: "Rain",          65: "Hvy rain",
   71: "Lt. snow",    73: "Snow",          75: "Hvy snow",
   77: "Snow",        80: "Showers",       81: "Showers",
   82: "Showers",     85: "Snowfall",      86: "Snowfall",
   95: "Thunder",     96: "Th.+hail",      99: "Th.+hail",
}

_ICON: dict[int, tuple[str, str]] = {
    0:  ("☀", "bright_yellow"),
    1:  ("☀", "bright_yellow"),
    2:  ("☁", "yellow"),           # partly cloudy: cloud in yellow (vs white for overcast)
    3:  ("☁", "white"),
    45: ("≋", "grey62"),
    48: ("≋", "grey62"),
    51: ("☂", "cyan"),             # ☂ U+2602 replaces ☔ U+2614 (Wide → Ambiguous)
    53: ("☂", "cyan"),
    55: ("☂", "deep_sky_blue1"),
    61: ("☂", "deep_sky_blue1"),
    63: ("☂", "deep_sky_blue1"),
    65: ("☂", "bold deep_sky_blue1"),
    71: ("❄", "bright_white"),
    73: ("❄", "bright_white"),
    75: ("❄", "bold bright_white"),
    77: ("❄", "bright_white"),
    80: ("☂", "cyan"),
    81: ("☂", "deep_sky_blue1"),
    82: ("☂", "bold deep_sky_blue1"),
    85: ("❄", "bright_white"),
    86: ("❄", "bold bright_white"),
    95: ("↯", "bright_yellow"),    # ↯ U+21AF replaces ⚡ U+26A1 (Wide → Narrow)
    96: ("↯", "bright_yellow"),
    99: ("↯", "bright_yellow"),
}


def _wx_icon(code: int) -> Text:
    char, style = _ICON.get(code, ("?", "dim"))
    return Text(char, style=style)

# Half-block pixel art (▀/▄/█) — 12×8 pixel grids, 2 rows → 1 display line
# Char codes: '.' transparent, 'Y' sun, 'W' white cloud, 'G' dark cloud,
#             'b' rain, 's' snow, 'L' lightning, 'f' fog

_C: dict[str, str | None] = {
    ".": None,
    "Y": "gold1",
    "W": "grey82",
    "G": "grey46",
    "b": "deep_sky_blue1",
    "s": "bright_white",
    "L": "bright_yellow",
    "f": "grey62",
}

_PIXELS: dict[str, list[str]] = {
    "clear": [
        "....YYYY....",
        "...YYYYYY...",
        "..YYYYYYYY..",
        ".YYYYYYYYYY.",
        ".YYYYYYYYYY.",
        "..YYYYYYYY..",
        "...YYYYYY...",
        "....YYYY....",
    ],
    "partly": [
        "........YYYY",
        ".......YYYYY",
        "WWWWWWYYYYYY",
        "WWWWWWWWYYYY",
        "WWWWWWWWWWW.",
        "WWWWWWWWWWW.",
        ".WWWWWWWWW..",
        "............",
    ],
    "cloudy": [
        "............",
        "...WWWWW....",
        ".WWWWWWWWW..",
        "WWWWWWWWWWW.",
        "WWWWWWWWWWW.",
        ".WWWWWWWWW..",
        "............",
        "............",
    ],
    "fog": [
        "............",
        ".ffffffff...",
        "............",
        "..ffffffff..",
        "............",
        ".ffffffff...",
        "............",
        "............",
    ],
    "drizzle": [
        "...GGGGGG...",
        ".GGGGGGGGG..",
        "GGGGGGGGGGG.",
        ".GGGGGGGGG..",
        "..b...b.....",
        "............",
        "..b...b.....",
        "............",
    ],
    "rain": [
        "..GGGGGGG...",
        ".GGGGGGGGG..",
        "GGGGGGGGGGG.",
        ".GGGGGGGGG..",
        ".bb.bb.bb...",
        ".bb.bb.bb...",
        "bb.bb.bb....",
        "............",
    ],
    "snow": [
        "..GGGGGGG...",
        ".GGGGGGGGG..",
        "GGGGGGGGGGG.",
        ".GGGGGGGGG..",
        ".s.s.s.s.s..",
        "..s.s.s.s...",
        ".s.s.s.s.s..",
        "............",
    ],
    "thunder": [
        "..GGGGGGG...",
        ".GGGGGGGGG..",
        "GGGGGGGGGGG.",
        ".GGGGGGGGG..",
        "....LLL.....",
        ".....LL.....",
        "......L.....",
        "............",
    ],
}

_PIXEL_KEY: dict[int, str] = {}
for _c in [0, 1]:                _PIXEL_KEY[_c] = "clear"
_PIXEL_KEY[2]                     = "partly"
_PIXEL_KEY[3]                     = "cloudy"
for _c in [45, 48]:               _PIXEL_KEY[_c] = "fog"
for _c in [51, 53, 55, 80]:       _PIXEL_KEY[_c] = "drizzle"
for _c in [61, 63, 65, 81, 82]:   _PIXEL_KEY[_c] = "rain"
for _c in [71, 73, 75, 77, 85, 86]: _PIXEL_KEY[_c] = "snow"
for _c in [95, 96, 99]:           _PIXEL_KEY[_c] = "thunder"


def _render_pixels(key: str) -> Text:
    rows = _PIXELS.get(key, _PIXELS["cloudy"])
    text = Text()
    for i in range(0, len(rows), 2):
        top_row = rows[i]
        bot_row = rows[i + 1] if i + 1 < len(rows) else "." * len(top_row)
        for tc, bc in zip(top_row, bot_row):
            fg = _C.get(tc)
            bg = _C.get(bc)
            if fg is None and bg is None:
                text.append(" ")
            elif fg is None:
                text.append("▄", style=bg)
            elif bg is None:
                text.append("▀", style=fg)
            elif fg == bg:
                text.append("█", style=fg)
            else:
                text.append("▀", style=f"{fg} on {bg}")
        text.append("\n")
    return text


# ── data model ─────────────────────────────────────────────────────────────────

@dataclass
class ForecastDay:
    date: date
    condition: int
    t_min: float
    t_max: float


@dataclass
class WeatherData:
    location: str
    temp: float
    feels_like: float
    condition: int
    wind_speed: float
    unit_temp: str       # "°C" or "°F"
    unit_wind: str       # "km/h" or "mph"
    forecast: list[ForecastDay] = field(default_factory=list)


# ── Open-Meteo client ──────────────────────────────────────────────────────────

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def _resolve_location(location: str) -> tuple[float, float]:
    if "," in location:
        lat_s, lon_s = location.split(",", 1)
        try:
            return float(lat_s.strip()), float(lon_s.strip())
        except ValueError:
            pass  # not coordinates — fall through to geocoding
    resp = requests.get(
        _GEOCODE_URL,
        params={"name": location, "count": 1, "language": "en", "format": "json"},
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        raise RuntimeError(f"Location not found: {location!r}")
    r = results[0]
    return r["latitude"], r["longitude"]


def _fetch_weather(location: str, units: str) -> WeatherData:
    lat, lon = _resolve_location(location)
    metric = units.lower() != "imperial"
    resp = requests.get(
        _FORECAST_URL,
        params={
            "latitude":         lat,
            "longitude":        lon,
            "current":          "temperature_2m,apparent_temperature,weathercode,windspeed_10m",
            "daily":            "weathercode,temperature_2m_max,temperature_2m_min",
            "temperature_unit": "celsius" if metric else "fahrenheit",
            "wind_speed_unit":  "kmh" if metric else "mph",
            "forecast_days":    7,
            "timezone":         "auto",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    cur   = data["current"]
    daily = data["daily"]

    forecast = [
        ForecastDay(
            date=date.fromisoformat(daily["time"][i]),
            condition=int(daily["weathercode"][i]),
            t_min=daily["temperature_2m_min"][i],
            t_max=daily["temperature_2m_max"][i],
        )
        for i in range(0, min(6, len(daily["time"])))
    ]

    return WeatherData(
        location=location,
        temp=cur["temperature_2m"],
        feels_like=cur["apparent_temperature"],
        condition=int(cur["weathercode"]),
        wind_speed=cur["windspeed_10m"],
        unit_temp="°C" if metric else "°F",
        unit_wind="km/h" if metric else "mph",
        forecast=forecast,
    )


# ── rendering ──────────────────────────────────────────────────────────────────

_BAR_W = 16  # horizontal bar width in characters


def _bar_color(temp_c: float) -> str:
    if temp_c >= 30: return "red"
    if temp_c >= 24: return "yellow"
    if temp_c >= 16: return "green"
    if temp_c >= 8:  return "cyan"
    return "blue"


def _h_bar(t_min: float, t_max: float, g_min: float, g_max: float, metric: bool) -> Text:
    t_rng = max(g_max - g_min, 1.0)
    lo = max(0, min(_BAR_W - 1, round((t_min - g_min) / t_rng * _BAR_W)))
    hi = max(lo + 1, min(_BAR_W, round((t_max - g_min) / t_rng * _BAR_W)))
    mid_c = (t_min + t_max) / 2
    if not metric:
        mid_c = (mid_c - 32) * 5 / 9
    color = _bar_color(mid_c)
    bar = Text()
    if lo:
        bar.append("░" * lo, style="dim")
    bar.append("█" * (hi - lo), style=color)
    if hi < _BAR_W:
        bar.append("░" * (_BAR_W - hi), style="dim")
    return bar


def _render_weather(d: WeatherData) -> Table:
    art_key = _PIXEL_KEY.get(d.condition, "cloudy")

    # ── left: pixel art + current reading ────────────────────────────────
    left = _render_pixels(art_key)
    left.append(f"{d.temp:.0f}{d.unit_temp}", style="bold white")
    desc = _DESC.get(d.condition, "")
    left.append(f"  {desc}\n" if desc else "\n", style="dim")
    left.append(f"Feels {d.feels_like:.0f}{d.unit_temp}  ↗ {d.wind_speed:.0f} {d.unit_wind}", style="dim")

    # ── right: 3-day forecast (today / +1 / +2) as horizontal range bars ──
    fc = d.forecast[:6]
    metric = d.unit_temp == "°C"
    if not fc:
        right: Any = Text("—")
    else:
        g_min = min(f.t_min for f in fc)
        g_max = max(f.t_max for f in fc)

        t = Table.grid(padding=(0, 0))
        t.add_column(width=5,      justify="left")    # "Today" / "Mon" / "Tue"
        t.add_column(width=1,      justify="left")    # weather icon (all exactly 1 terminal cell)
        t.add_column(width=5,      justify="right")   # t_min (right-justify adds 1-char buffer)
        t.add_column(width=_BAR_W)                    # horizontal bar
        t.add_column(width=4,      justify="left")    # t_max (0 gap after bar)

        for i, f in enumerate(fc):
            label = "Today" if i == 0 else f.date.strftime("%a")
            t.add_row(
                label,
                _wx_icon(f.condition),
                f"{f.t_min:.0f}{d.unit_temp}",
                _h_bar(f.t_min, f.t_max, g_min, g_max, metric),
                f"{f.t_max:.0f}{d.unit_temp}",
            )

        right = t

    # ── two-column layout ─────────────────────────────────────────────────
    layout = Table.grid(padding=(0, 1))
    layout.add_column(width=14, no_wrap=True)
    layout.add_column()
    layout.add_row(left, right)
    return Align.center(layout)


# ── widget ─────────────────────────────────────────────────────────────────────

class WeatherWidget(DashWidget):
    """Current conditions + 5-day forecast powered by Open-Meteo."""

    data: reactive[WeatherData | None] = reactive(None, always_update=True)

    DEFAULT_CSS = """
    WeatherWidget { height: 100%; }
    #wx-body { height: 100%; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._location = ""
        self._units = "metric"
        self._err: str | None = None
        self._data_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Static("[dim]Loading…[/dim]", id="wx-body")

    def on_mount(self) -> None:
        try:
            self._location = config.require("TUIDASH_WEATHER_LOCATION")
        except RuntimeError as exc:
            self._show_error(str(exc))
            return
        self._units = config.get("TUIDASH_WEATHER_UNITS", "metric") or "metric"
        self._load()

    def set_refresh_interval(self, seconds: int) -> None:
        if self._data_timer is not None:
            self._data_timer.stop()
        self._data_timer = self.set_interval(seconds, self._load)

    @work(thread=True)
    def _load(self) -> None:
        try:
            data = _fetch_weather(self._location, self._units)
            self.app.call_from_thread(self._show_data, data)
        except Exception as exc:
            self.app.call_from_thread(self._show_error, str(exc))

    def _show_data(self, data: WeatherData) -> None:
        self._err = None
        self.data = data

    def _show_error(self, msg: str) -> None:
        self._err = msg
        self.query_one("#wx-body", Static).update(f"[red]Error:[/red] {msg}")

    def watch_data(self, data: WeatherData | None) -> None:
        if data is None or self._err:
            return
        self.border_subtitle = self._location
        self.query_one("#wx-body", Static).update(_render_weather(data))
