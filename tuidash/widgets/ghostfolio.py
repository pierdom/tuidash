from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import requests
from rich.align import Align
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
class PortfolioData:
    total_value: float
    currency: str
    today: PerfStats
    one_year: PerfStats
    mtd: PerfStats
    gainers: list[Holding] = field(default_factory=list)
    losers: list[Holding]  = field(default_factory=list)
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


    def fetch(self) -> PortfolioData:
        # ── parallel API calls ─────────────────────────────────────────────
        with ThreadPoolExecutor(max_workers=5) as pool:
            f_1d       = pool.submit(self._get, "/api/v2/portfolio/performance", range="1d")
            f_mtd      = pool.submit(self._get, "/api/v2/portfolio/performance", range="mtd")
            f_1y       = pool.submit(self._get, "/api/v2/portfolio/performance", range="1y")
            f_holdings = pool.submit(self._get, "/api/v1/portfolio/holdings")
            f_orders   = pool.submit(self._get, "/api/v1/order")

        chart_1d  = f_1d.result().get("chart", [])
        chart_1y  = f_1y.result().get("chart", [])
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

        # ── total value ───────────────────────────────────────────────────
        total_value = chart_1y[-1].get("netWorth", 0.0) if chart_1y else 0.0

        # ── today (1d endpoint; fallback to last 1y move outside hours) ──
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

        # ── holdings: sort by total return ────────────────────────────────
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

        # ── transaction counts + volumes ──────────────────────────────────
        today_d   = date.today()
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

        return PortfolioData(
            total_value=total_value,
            currency=currency,
            today=perf_today,
            one_year=perf_1y,
            mtd=perf_mtd,
            gainers=gainers,
            losers=losers,
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


_GOAL = 1_000_000


def _render_portfolio(d: PortfolioData, privacy: bool = False) -> Group:
    cur = d.currency
    symbols = {"USD": "$", "EUR": "€", "GBP": "£", "CHF": "Fr "}
    sym = symbols.get(cur, f"{cur} ")

    # ── net worth + dot + progress bar (single line) ─────────────────────
    progress_pct = min(d.total_value / _GOAL * 100, 100.0)
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
        Text(f"→ {sym}{_MASK}" if privacy else f"→ {sym}1M", style="dim"),
    )

    # ── single-row stats: Today | MTD | 1 Year ───────────────────────────
    grid = Table.grid(expand=True, padding=(0, 2))
    for _ in range(3):
        grid.add_column(ratio=1)
    grid.add_row(
        _stat_cell("Today",  d.today,    cur, privacy),
        _stat_cell("MTD",    d.mtd,      cur, privacy),
        _stat_cell("1 Year", d.one_year, cur, privacy),
    )

    # ── gainers / losers + transactions ───────────────────────────────────
    def _side(title: str, items: list[Holding]) -> Table:
        t = Table.grid(padding=(0, 0))
        t.add_column()
        t.add_row(Text(title, style="bold dim"))
        for h in items[:2]:
            t.add_row(_holding_line(h))
        return t

    def _tx_side() -> Table:
        symbols = {"USD": "$", "EUR": "€", "GBP": "£", "CHF": "Fr "}
        sym = symbols.get(d.currency, f"{d.currency} ")

        def _tx_row(count: int, label: str, vol: float) -> Text:
            row = Text()
            row.append(f"{label}", style="")
            row.append(f"  {count}", style="dim")
            row.append(f"  {_MASK if privacy else f'{sym}{vol:,.0f}'}", style="dim")
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

    return Group(header, Text(""), grid, Rule(style="dim"), gl)


# ── widget ─────────────────────────────────────────────────────────────────────

class GhostfolioWidget(DashWidget):
    """Portfolio overview powered by Ghostfolio."""

    data: reactive[PortfolioData | None] = reactive(None, always_update=True)

    DEFAULT_CSS = """
    GhostfolioWidget { height: 100%; }
    #gf-body { height: 100%; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client: GhostfolioClient | None = None
        self._err: str | None = None
        self._privacy: bool = False
        self._data_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Static("[dim]Loading…[/dim]", id="gf-body")

    def on_mount(self) -> None:
        try:
            url   = config.require("TUIDASH_GHOSTFOLIO_URL")
            token = config.require("TUIDASH_GHOSTFOLIO_TOKEN")
            self._client = GhostfolioClient(url, token)
        except RuntimeError as exc:
            self._show_error(str(exc))
            return
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

    def watch_data(self, data: PortfolioData | None) -> None:
        if data is None or self._err:
            return
        self.query_one("#gf-body", Static).update(_render_portfolio(data, self._privacy))
