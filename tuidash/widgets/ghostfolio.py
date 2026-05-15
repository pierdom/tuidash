from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import requests
from rich.console import Group
from rich.progress_bar import ProgressBar
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static
from textual import work

from .. import config
from .base import DashWidget


_TICKER_INTERVAL = 0.125  # seconds per scroll step (≈8 chars/sec)
_TICKER_SEP      = "   ◆   "


# ── data model ────────────────────────────────────────────────────────────────

@dataclass
class PerfStats:
    pct: float   # percentage, e.g. 1.23 = +1.23%
    abs: float   # absolute change in base currency


@dataclass
class Holding:
    name: str
    symbol: str
    value: float
    currency: str
    perf_pct: float   # total return %


@dataclass
class TickerItem:
    symbol: str
    name: str
    change_pct: float   # today's daily % change
    price: float | None = None
    currency: str = ""


@dataclass
class PortfolioData:
    total_value: float
    currency: str
    base_currency: str
    goal: float
    today: PerfStats
    one_year: PerfStats
    mtd: PerfStats
    gainers: list[Holding]     = field(default_factory=list)
    losers: list[Holding]      = field(default_factory=list)
    ticker: list[TickerItem]   = field(default_factory=list)
    tx_7d: int       = 0
    tx_7d_vol: float = 0.0
    tx_30d: int      = 0
    tx_30d_vol: float = 0.0


# ── Ghostfolio client ──────────────────────────────────────────────────────────

class GhostfolioClient:
    def __init__(self, base_url: str, access_token: str) -> None:
        self._base = base_url.strip().rstrip("/")
        self._access_token = "".join(access_token.split())
        self._jwt: str | None = None
        self._lock = threading.Lock()
        # symbol → (date_str, prev_close) — refreshed once per calendar day
        self._prev_close_cache: dict[str, tuple[str, float]] = {}

    def _auth(self) -> str:
        resp = requests.post(
            f"{self._base}/api/v1/auth/anonymous",
            json={"accessToken": self._access_token},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["authToken"]

    def _headers(self) -> dict[str, str]:
        with self._lock:
            if self._jwt is None:
                self._jwt = self._auth()
        return {"Authorization": f"Bearer {self._jwt}"}

    def _get(self, path: str, **params: Any) -> Any:
        url = f"{self._base}{path}"
        resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
        if resp.status_code == 401:
            with self._lock:
                self._jwt = self._auth()
            resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _last_trading_pair(chart: list[dict]) -> tuple[dict, dict]:
        """Walk backwards to find the last entry where netPerformance actually moved."""
        for i in range(len(chart) - 1, 0, -1):
            if chart[i].get("netPerformance") != chart[i - 1].get("netPerformance"):
                return chart[i], chart[i - 1]
        return chart[-1], chart[-2] if len(chart) >= 2 else chart[-1]

    def _daily_change(self, symbol: str, data_source: str, market_price: float) -> float | None:
        """Return today's % price change. Fetches market history once per day; uses cache after."""
        today = date.today().isoformat()
        cached = self._prev_close_cache.get(symbol)
        if cached and cached[0] == today:
            prev = cached[1]
        else:
            try:
                md   = self._get(f"/api/v1/market-data/{data_source}/{symbol}")
                pts  = md.get("marketData", [])
                if len(pts) < 2:
                    return None
                prev = pts[-2]["marketPrice"]
                self._prev_close_cache[symbol] = (today, prev)
            except Exception:
                return None
        if not prev:
            return None
        return (market_price - prev) / prev * 100

    def _fetch_ticker(self, holdings_raw: list[dict]) -> list[TickerItem]:
        equities = [
            (
                h.get("symbol", ""),
                h.get("dataSource", "YAHOO"),
                h.get("name", h.get("symbol", "?")),
                h.get("currency", ""),
                h.get("marketPrice"),
            )
            for h in holdings_raw
            if h.get("assetClass") not in ("CASH", "LIQUIDITY")
            and h.get("symbol") != h.get("currency")
            and h.get("dataSource", "YAHOO") != "MANUAL"
        ]
        if not equities:
            return []

        with ThreadPoolExecutor(max_workers=len(equities)) as pool:
            changes = list(pool.map(
                lambda e: self._daily_change(e[0], e[1], e[4] or 0.0), equities
            ))

        items = [
            TickerItem(
                symbol=sym,
                name=name,
                change_pct=chg,
                price=price,
                currency=cur,
            )
            for (sym, _, name, cur, price), chg in zip(equities, changes)
            if chg is not None
        ]
        items.sort(key=lambda x: x.symbol)
        return items

    def fetch(self) -> PortfolioData:
        # ── phase 1: portfolio-level parallel calls ───────────────────────────
        with ThreadPoolExecutor(max_workers=6) as pool:
            f_1d       = pool.submit(self._get, "/api/v2/portfolio/performance", range="1d")
            f_mtd      = pool.submit(self._get, "/api/v2/portfolio/performance", range="mtd")
            f_1y       = pool.submit(self._get, "/api/v2/portfolio/performance", range="1y")
            f_holdings = pool.submit(self._get, "/api/v1/portfolio/holdings")
            f_orders   = pool.submit(self._get, "/api/v1/order")
            f_user     = pool.submit(self._get, "/api/v1/user")

        chart_1d     = f_1d.result().get("chart", [])
        chart_1y     = f_1y.result().get("chart", [])
        holdings_raw = f_holdings.result().get("holdings", [])
        if isinstance(holdings_raw, dict):
            holdings_raw = list(holdings_raw.values())
        orders_raw = f_orders.result().get("activities", [])

        def _read_perf(chart: list[dict]) -> PerfStats:
            last = chart[-1] if chart else {}
            return PerfStats(
                pct=last.get("netPerformanceInPercentage", 0.0) * 100,
                abs=last.get("netPerformance", 0.0),
            )

        # ── total value ───────────────────────────────────────────────────────
        total_value = chart_1y[-1].get("netWorth", 0.0) if chart_1y else 0.0

        # ── today (1d endpoint; fallback to last 1y move outside hours) ──────
        perf_today = _read_perf(chart_1d)
        if not chart_1d or perf_today.abs == 0.0:
            t_cur, t_prev = self._last_trading_pair(chart_1y) if len(chart_1y) >= 2 else ({}, {})
            today_abs = t_cur.get("netPerformance", 0.0) - t_prev.get("netPerformance", 0.0)
            r_prev    = 1.0 + t_prev.get("netPerformanceInPercentage", 0.0)
            r_cur     = 1.0 + t_cur.get("netPerformanceInPercentage", 0.0)
            today_pct = (r_cur / r_prev - 1.0) * 100 if r_prev else 0.0
            perf_today = PerfStats(today_pct, today_abs)

        perf_mtd = _read_perf(f_mtd.result().get("chart", []))
        perf_1y  = _read_perf(chart_1y)

        # ── holdings: sort by total return ────────────────────────────────────
        currency = holdings_raw[0].get("currency", "") if holdings_raw else ""
        holdings: list[Holding] = []
        for h in holdings_raw:
            holdings.append(Holding(
                name=h.get("name", h.get("symbol", "?")),
                symbol=h.get("symbol", "?"),
                value=h.get("valueInBaseCurrency", 0.0),
                currency=h.get("currency", currency),
                perf_pct=h.get("netPerformancePercent", 0.0) * 100,
            ))

        equity  = [h for h in holdings if h.symbol != currency]
        gainers = sorted([h for h in equity if h.perf_pct >= 0], key=lambda x: x.perf_pct, reverse=True)[:2]
        losers  = sorted([h for h in equity if h.perf_pct <  0], key=lambda x: x.perf_pct)[:2]

        # ── transaction counts + volumes ──────────────────────────────────────
        today_d    = date.today()
        cutoff_7d  = (today_d - timedelta(days=7)).isoformat()
        cutoff_30d = (today_d - timedelta(days=30)).isoformat()
        tx_7d = tx_30d = 0
        tx_7d_vol = tx_30d_vol = 0.0
        for o in orders_raw:
            d_str = o.get("date", "")[:10]
            vol   = abs(o.get("valueInBaseCurrency", 0.0) or
                        o.get("quantity", 0.0) * o.get("unitPrice", 0.0))
            if cutoff_7d <= d_str <= today_d.isoformat():
                tx_7d     += 1
                tx_7d_vol += vol
            if cutoff_30d <= d_str <= today_d.isoformat():
                tx_30d     += 1
                tx_30d_vol += vol

        # ── base currency from user settings ──────────────────────────────────
        try:
            base_currency = f_user.result().get("settings", {}).get("baseCurrency", currency)
        except Exception:
            base_currency = currency

        # ── goal from config ───────────────────────────────────────────────────
        try:
            goal = float(config.get("TUIDASH_GHOSTFOLIO_GOAL") or "1000000")
        except ValueError:
            goal = 1_000_000

        # ── phase 2: per-symbol daily change for ticker ───────────────────────
        ticker = self._fetch_ticker(holdings_raw)

        return PortfolioData(
            total_value=total_value,
            currency=currency,
            base_currency=base_currency,
            goal=goal,
            today=perf_today,
            one_year=perf_1y,
            mtd=perf_mtd,
            gainers=gainers,
            losers=losers,
            ticker=ticker,
            tx_7d=tx_7d,
            tx_7d_vol=tx_7d_vol,
            tx_30d=tx_30d,
            tx_30d_vol=tx_30d_vol,
        )


# ── Rich helpers ───────────────────────────────────────────────────────────────

def _fmt(value: float, currency: str) -> str:
    symbols = {"USD": "$", "EUR": "€", "GBP": "£", "CHF": "Fr "}
    sym = symbols.get(currency, f"{currency} ")
    sign = "+" if value > 0 else ""
    return f"{sign}{sym}{abs(value):,.2f}"


_MASK = "•••••"


def _stat_cell(label: str, stats: PerfStats, currency: str, privacy: bool = False) -> Text:
    color = "green" if stats.pct >= 0 else "red"
    arrow = "▲" if stats.pct >= 0 else "▼"
    t = Text()
    t.append(f"{label}\n", style="bold white")
    t.append(f"{arrow} {abs(stats.pct):.2f}%\n", style=f"bold {color}")
    t.append(_MASK if privacy else _fmt(stats.abs, currency), style=f"dim {color}")
    return t


def _holding_line(h: Holding) -> Text:
    color = "green" if h.perf_pct >= 0 else "red"
    arrow = "▲" if h.perf_pct >= 0 else "▼"
    t = Text()
    t.append(f"{h.symbol[:8]:<8}", style="bold")
    t.append(f"{arrow}{abs(h.perf_pct):.2f}%", style=color)
    return t


def _fmt_goal(goal: float) -> str:
    if goal >= 1_000_000:
        v = goal / 1_000_000
        return f"{v:.0f}M" if v == int(v) else f"{v:.1f}M"
    if goal >= 1_000:
        v = goal / 1_000
        return f"{v:.0f}K" if v == int(v) else f"{v:.1f}K"
    return f"{goal:.0f}"


def _render_portfolio(d: PortfolioData, privacy: bool = False) -> Group:
    cur = d.base_currency or d.currency
    symbols = {"USD": "$", "EUR": "€", "GBP": "£", "CHF": "Fr "}
    sym = symbols.get(cur, f"{cur} ")

    # ── net worth + progress bar ──────────────────────────────────────────────
    progress_pct = min(d.total_value / d.goal * 100, 100.0) if d.goal else 0.0
    if abs(d.today.pct) <= 0.1:
        dot_color = "yellow"
    elif d.today.pct > 0:
        dot_color = "green"
    else:
        dot_color = "red"
    nw = Text()
    nw.append("● ", style=f"bold {dot_color}")
    nw.append(f"{sym}{_MASK}" if privacy else f"{sym}{d.total_value:,.2f}", style="bold white")

    header = Table.grid(expand=True, padding=(0, 1))
    header.add_column(no_wrap=True)
    header.add_column(ratio=1)
    header.add_column(no_wrap=True)
    header.add_column(no_wrap=True)
    header.add_row(
        nw,
        ProgressBar(total=100, completed=progress_pct, complete_style="green"),
        Text(f"{progress_pct:.1f}%", style="dim"),
        Text(f"→ {sym}{_MASK}" if privacy else f"→ {sym}{_fmt_goal(d.goal)}", style="dim"),
    )

    # ── single-row stats: Today | MTD | 1 Year ───────────────────────────────
    grid = Table.grid(expand=True, padding=(0, 2))
    for _ in range(3):
        grid.add_column(ratio=1)
    grid.add_row(
        _stat_cell("Today",  d.today,    d.base_currency or cur, privacy),
        _stat_cell("MTD",    d.mtd,      d.base_currency or cur, privacy),
        _stat_cell("1 Year", d.one_year, d.base_currency or cur, privacy),
    )

    # ── gainers / losers + transactions ──────────────────────────────────────
    def _side(title: str, items: list[Holding]) -> Table:
        t = Table.grid(padding=(0, 0))
        t.add_column()
        t.add_row(Text(title, style="bold dim"))
        for h in items[:2]:
            t.add_row(_holding_line(h))
        return t

    def _tx_side() -> Table:
        sym_map = {"USD": "$", "EUR": "€", "GBP": "£", "CHF": "Fr "}
        s = sym_map.get(d.currency, f"{d.currency} ")

        def _tx_row(count: int, label: str, vol: float) -> Text:
            row = Text()
            row.append(f"{label}", style="")
            row.append(f"  {count}", style="dim")
            row.append(f"  {_MASK if privacy else f'{s}{vol:,.0f}'}", style="dim")
            return row

        t = Table.grid(padding=(0, 0))
        t.add_column()
        t.add_row(Text("● Trades", style="bold dim"))
        t.add_row(_tx_row(d.tx_7d,  "7d",  d.tx_7d_vol))
        t.add_row(_tx_row(d.tx_30d, "30d", d.tx_30d_vol))
        return t

    gl = Table.grid(expand=True, padding=(0, 2))
    gl.add_column(ratio=1)
    gl.add_column(ratio=1)
    gl.add_column(ratio=1)
    gl.add_row(
        _side("▲ Gainers", d.gainers),
        _side("▼ Losers",  d.losers),
        _tx_side(),
    )

    return Group(header, Rule(style="dim"), grid, Rule(style="dim"), gl)


def _ticker_color(pct: float) -> str:
    if abs(pct) < 0.05:
        return "yellow"
    if pct > 2.0:
        return "bright_green"
    if pct > 0:
        return "green"
    if pct < -2.0:
        return "bright_red"
    return "red"


def _render_ticker(items: list[TickerItem], tick: int, width: int) -> Text:
    if not items:
        return Text()

    segments: list[tuple[str, str]] = []
    for item in items:
        pct   = item.change_pct
        color = _ticker_color(pct)
        arrow = "▲" if pct > 0.05 else ("▼" if pct < -0.05 else "─")

        segments.append((_TICKER_SEP, "dim"))
        segments.append((item.symbol, f"bold {color}"))
        segments.append((" ", ""))
        segments.append((f"{arrow}{abs(pct):.2f}%", color))
        if item.price is not None:
            segments.append((f"  {item.price:.2f}", "dim"))

    segments.append((_TICKER_SEP, "dim"))   # trailing sep → seamless loop

    full_len = sum(len(s) for s, _ in segments)
    if full_len <= width:
        t = Text()
        for seg, style in segments:
            t.append(seg, style=style)
        return t

    offset   = tick % full_len
    t        = Text()
    char_pos = 0
    for seg, style in (segments + segments):   # doubled for wrap-around
        seg_end = char_pos + len(seg)
        vis_s   = max(offset, char_pos)
        vis_e   = min(offset + width, seg_end)
        if vis_s < vis_e:
            t.append(seg[vis_s - char_pos : vis_e - char_pos], style=style)
        char_pos = seg_end
        if char_pos >= offset + width:
            break

    return t


# ── widget ─────────────────────────────────────────────────────────────────────

class GhostfolioWidget(DashWidget):
    """Portfolio overview powered by Ghostfolio."""

    data: reactive[PortfolioData | None] = reactive(None, always_update=True)

    DEFAULT_CSS = """
    GhostfolioWidget { height: auto; }
    #gf-body        { height: auto; }
    #gf-ticker-rule { height: 1; }
    #gf-ticker      { height: 1; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client: GhostfolioClient | None = None
        self._err: str | None = None
        self._privacy: bool = False
        self._data_timer: Timer | None = None
        self._ticker_tick: int = 0
        self._ticker_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Static("[dim]Loading…[/dim]", id="gf-body")
        yield Static(Rule(style="dim"), id="gf-ticker-rule")
        yield Static("", id="gf-ticker")

    def on_mount(self) -> None:
        try:
            url   = config.require("TUIDASH_GHOSTFOLIO_URL")
            token = config.require("TUIDASH_GHOSTFOLIO_TOKEN")
            self._client = GhostfolioClient(url, token)
        except RuntimeError as exc:
            self._show_error(str(exc))
            return
        self._ticker_timer = self.set_interval(_TICKER_INTERVAL, self._advance_ticker)
        self._load()

    def set_refresh_interval(self, seconds: int) -> None:
        if self._data_timer is not None:
            self._data_timer.stop()
        self._data_timer = self.set_interval(seconds, self._load)

    @work(thread=True)
    def _load(self) -> None:
        assert self._client is not None
        try:
            data = self._client.fetch()
            self.app.call_from_thread(self._show_data, data)
        except Exception as exc:
            self.app.call_from_thread(self._show_error, str(exc))

    def _show_data(self, data: PortfolioData) -> None:
        self._err = None
        self.data = data

    def _show_error(self, msg: str) -> None:
        self._err = msg
        self.query_one("#gf-body", Static).update(f"[red]Error:[/red] {msg}")

    def set_privacy(self, value: bool) -> None:
        self._privacy = value
        if self.data is not None and not self._err:
            self.query_one("#gf-body", Static).update(_render_portfolio(self.data, self._privacy))

    def _ticker_width(self) -> int:
        return max(20, self.content_size.width or 80)

    def _advance_ticker(self) -> None:
        self._ticker_tick += 1
        if self.data is not None and self.data.ticker:
            self._redraw_ticker()

    def _redraw_ticker(self) -> None:
        if self.data is None:
            return
        t = _render_ticker(self.data.ticker, self._ticker_tick, self._ticker_width())
        self.query_one("#gf-ticker", Static).update(t)

    def watch_data(self, data: PortfolioData | None) -> None:
        if data is None or self._err:
            return
        self.query_one("#gf-body", Static).update(_render_portfolio(data, self._privacy))
        self._redraw_ticker()
