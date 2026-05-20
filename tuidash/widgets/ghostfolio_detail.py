from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from rich.console import Group
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import ScrollableContainer
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static
from textual import work

from .. import config
from ..theme import ACCENT, PERF_BAD, PERF_FLAT, PERF_GOOD, PERF_GREAT, PERF_POOR, PERF_TERRIBLE
from .base import DashWidget
from .ghostfolio import (
    GhostfolioClient,
    PerfStats,
    TickerItem,
    _CURRENCY_SYMBOLS,
    _TICKER_INTERVAL,
    _ticker_color,
    _render_ticker,
    _fmt_goal,
)


# ── asset class map ────────────────────────────────────────────────────────────

_ASSET_CLASS: dict[str, tuple[str, str]] = {
    "EQUITY":         ("Equity",      "cyan"),
    "CRYPTOCURRENCY": ("Crypto",      "yellow"),
    "BOND":           ("Bonds",       "bright_white"),
    "FIXED_INCOME":   ("Bonds",       "bright_white"),
    "COMMODITY":      ("Commodities", ""),
    "REAL_ESTATE":    ("Real Estate", "magenta"),
    "CASH":           ("Cash",        "dim"),
    "LIQUIDITY":      ("Cash",        "dim"),
}

_MASK = "•••••"

# Braille dot bit tables — left col rows 0-3 (top→bottom): 1,2,4,64
#                           right col rows 0-3 (top→bottom): 8,16,32,128
_BR_LEFT  = [0x01, 0x02, 0x04, 0x40]
_BR_RIGHT = [0x08, 0x10, 0x20, 0x80]


# ── data models ────────────────────────────────────────────────────────────────

@dataclass
class HoldingDetail:
    symbol: str
    name: str
    value: float
    allocation_pct: float
    total_return_pct: float
    asset_class: str
    today_pct: float | None = None


@dataclass
class AssetAlloc:
    label: str
    pct: float
    color: str


@dataclass
class AccountDetail:
    name: str
    currency: str
    cash: float
    positions: float

    @property
    def total(self) -> float:
        return self.cash + self.positions


@dataclass
class MonthActivity:
    label: str        # e.g. "Apr 2026" or "May 2026 (MTD)"
    trades: int
    perf_pct: float
    perf_abs: float


@dataclass
class DetailData:
    total_value: float
    base_currency: str
    goal: float
    today: PerfStats
    wtd: PerfStats
    mtd: PerfStats
    ytd: PerfStats
    one_year: PerfStats
    holdings: list[HoldingDetail]  = field(default_factory=list)
    allocation: list[AssetAlloc]   = field(default_factory=list)
    ticker: list[TickerItem]       = field(default_factory=list)
    accounts: list[AccountDetail]  = field(default_factory=list)
    activity: list[MonthActivity]  = field(default_factory=list)
    chart_pct: list[float]              = field(default_factory=list)
    chart_dates: list[str]              = field(default_factory=list)
    chart_monthly: list[tuple[str, float]] = field(default_factory=list)


# ── helpers ────────────────────────────────────────────────────────────────────

def _read_perf(chart: list[dict]) -> PerfStats:
    last = chart[-1] if chart else {}
    return PerfStats(
        pct=last.get("netPerformanceInPercentage", 0.0) * 100,
        abs=last.get("netPerformance", 0.0),
    )


def _fmt_delta(value: float, currency: str) -> str:
    sym  = _CURRENCY_SYMBOLS.get(currency, f"{currency} ")
    sign = "+" if value >= 0 else "−"
    v    = abs(value)
    if v >= 1_000_000:
        return f"{sign}{sym}{v / 1_000_000:.1f}M"
    if v >= 10_000:
        return f"{sign}{sym}{v / 1_000:.0f}K"
    if v >= 1_000:
        return f"{sign}{sym}{v / 1_000:.1f}K"
    return f"{sign}{sym}{v:.0f}"


# ── data fetching ──────────────────────────────────────────────────────────────

def _fetch_detail(client: GhostfolioClient) -> DetailData:
    with ThreadPoolExecutor(max_workers=9) as pool:
        f_1d       = pool.submit(client._get, "/api/v2/portfolio/performance", range="1d")
        f_wtd      = pool.submit(client._get, "/api/v2/portfolio/performance", range="wtd")
        f_mtd      = pool.submit(client._get, "/api/v2/portfolio/performance", range="mtd")
        f_ytd      = pool.submit(client._get, "/api/v2/portfolio/performance", range="ytd")
        f_1y       = pool.submit(client._get, "/api/v2/portfolio/performance", range="1y")
        f_holdings = pool.submit(client._get, "/api/v1/portfolio/holdings")
        f_accounts = pool.submit(client._get, "/api/v1/account")
        f_orders   = pool.submit(client._get, "/api/v1/order")
        f_user     = pool.submit(client._get, "/api/v1/user")

    chart_1d = f_1d.result().get("chart", [])
    chart_1y = f_1y.result().get("chart", [])

    today = _read_perf(chart_1d)
    if not chart_1d or today.abs == 0.0:
        if len(chart_1y) >= 2:
            t_cur, t_prev = GhostfolioClient._last_trading_pair(chart_1y)
            today_abs = t_cur.get("netPerformance", 0.0) - t_prev.get("netPerformance", 0.0)
            r_prev    = 1.0 + t_prev.get("netPerformanceInPercentage", 0.0)
            r_cur     = 1.0 + t_cur.get("netPerformanceInPercentage", 0.0)
            today = PerfStats((r_cur / r_prev - 1.0) * 100 if r_prev else 0.0, today_abs)

    wtd      = _read_perf(f_wtd.result().get("chart", []))
    mtd      = _read_perf(f_mtd.result().get("chart", []))
    ytd      = _read_perf(f_ytd.result().get("chart", []))
    one_year = _read_perf(chart_1y)
    total_value = chart_1y[-1].get("netWorth", 0.0) if chart_1y else 0.0

    try:
        base_currency = f_user.result().get("settings", {}).get("baseCurrency", "")
    except Exception:
        base_currency = ""

    try:
        goal = float(config.get("TUIDASH_GHOSTFOLIO_GOAL") or "1000000")
    except ValueError:
        goal = 1_000_000

    accounts: list[AccountDetail] = []
    try:
        for a in f_accounts.result().get("accounts", []):
            if a.get("isExcluded"):
                continue
            cash  = float(a.get("balance") or 0)
            total = float(a.get("valueInBaseCurrency") or a.get("value") or 0)
            pos   = max(0.0, total - cash)
            if total >= 5000:
                accounts.append(AccountDetail(
                    name=a.get("name", "?"),
                    currency=a.get("currency", ""),
                    cash=cash,
                    positions=pos,
                ))
        accounts.sort(key=lambda a: a.total, reverse=True)
    except Exception:
        pass

    raw = f_holdings.result().get("holdings", [])
    if isinstance(raw, dict):
        raw = list(raw.values())

    holdings: list[HoldingDetail] = []
    for h in raw:
        holdings.append(HoldingDetail(
            symbol=h.get("symbol", "?"),
            name=h.get("name", h.get("symbol", "?")),
            value=h.get("valueInBaseCurrency", 0.0),
            allocation_pct=0.0,
            total_return_pct=h.get("netPerformancePercent", 0.0) * 100,
            asset_class=h.get("assetClass", ""),
        ))

    if total_value > 0:
        for h in holdings:
            h.allocation_pct = h.value / total_value * 100
    holdings.sort(key=lambda h: h.value, reverse=True)

    # Asset class breakdown
    alloc_map: dict[str, list] = {}
    for h in holdings:
        label, color = _ASSET_CLASS.get(h.asset_class.upper(), ("Other", ""))
        if label not in alloc_map:
            alloc_map[label] = [0.0, color]
        alloc_map[label][0] += h.allocation_pct
    allocation = [
        AssetAlloc(label=k, pct=v[0], color=v[1])
        for k, v in sorted(alloc_map.items(), key=lambda x: x[1][0], reverse=True)
    ]

    # Daily change per equity + attach to holdings
    equities = [
        (h.get("symbol", ""), h.get("dataSource", "YAHOO"),
         h.get("name", h.get("symbol", "?")), h.get("currency", ""),
         float(h.get("marketPrice") or 0.0))
        for h in raw
        if h.get("assetClass") not in ("CASH", "LIQUIDITY")
        and h.get("symbol") != h.get("currency")
        and h.get("dataSource", "YAHOO") != "MANUAL"
    ]
    ticker: list[TickerItem] = []
    if equities:
        with ThreadPoolExecutor(max_workers=len(equities)) as pool:
            changes = list(pool.map(
                lambda e: client._daily_change(e[0], e[1], e[4]), equities
            ))
        for (sym, _, name, cur, price), chg in zip(equities, changes):
            if chg is not None:
                ticker.append(TickerItem(symbol=sym, name=name, change_pct=chg,
                                         price=price, currency=cur))
    ticker.sort(key=lambda t: t.symbol)

    ticker_map = {t.symbol: t.change_pct for t in ticker}
    for h in holdings:
        h.today_pct = ticker_map.get(h.symbol)

    # ── monthly activity ───────────────────────────────────────────────────────
    from datetime import date as _D
    from calendar import monthrange

    def _month_perf(year: int, month: int) -> PerfStats:
        """Extract performance for a complete month from the 1y chart."""
        import datetime
        start_iso = datetime.date(year, month, 1).isoformat()
        _, last = monthrange(year, month)
        end_iso = datetime.date(year, month, last).isoformat()
        prev_entry: dict = {}
        end_entry: dict = {}
        for entry in chart_1y:
            d = entry.get("date", "")[:10]
            if d < start_iso:
                prev_entry = entry
            elif d <= end_iso:
                end_entry = entry
        if not end_entry:
            return PerfStats(0.0, 0.0)
        base_r = 1.0 + prev_entry.get("netPerformanceInPercentage", 0.0)
        end_r  = 1.0 + end_entry.get("netPerformanceInPercentage", 0.0)
        pct    = (end_r / base_r - 1.0) * 100 if base_r else 0.0
        abs_v  = end_entry.get("netPerformance", 0.0) - prev_entry.get("netPerformance", 0.0)
        return PerfStats(pct=pct, abs=abs_v)

    orders_raw: list[dict] = []
    try:
        orders_raw = f_orders.result().get("activities", [])
    except Exception:
        pass

    def _count_trades(year: int, month: int) -> int:
        n = 0
        for o in orders_raw:
            if o.get("type") not in ("BUY", "SELL"):
                continue
            d = (o.get("date") or "")[:10]
            try:
                dt = _D.fromisoformat(d)
                if dt.year == year and dt.month == month:
                    n += 1
            except Exception:
                pass
        return n

    today_d = _D.today()
    activity: list[MonthActivity] = []
    for i in range(3):
        m = today_d.month - i
        y = today_d.year
        while m <= 0:
            m += 12
            y -= 1
        month_name = _D(y, m, 1).strftime("%b %Y")
        if i == 0:
            label = f"{month_name} (MTD)"
            ps    = mtd
        else:
            label = month_name
            ps    = _month_perf(y, m)
        activity.append(MonthActivity(
            label=label,
            trades=_count_trades(y, m),
            perf_pct=ps.pct,
            perf_abs=ps.abs,
        ))

    chart_pct   = [e.get("netPerformanceInPercentage", 0.0) * 100 for e in chart_1y]
    chart_dates = [e.get("date", "") for e in chart_1y]

    # 12-month bar chart data: incremental monthly performance, oldest first
    chart_monthly: list[tuple[str, float]] = []
    for i in range(11, -1, -1):
        m = today_d.month - i
        y = today_d.year
        while m <= 0:
            m += 12
            y -= 1
        label = _D(y, m, 1).strftime("%b")
        ps = mtd if i == 0 else _month_perf(y, m)
        chart_monthly.append((label, ps.pct))

    return DetailData(
        total_value=total_value,
        base_currency=base_currency,
        goal=goal,
        today=today,
        wtd=wtd,
        mtd=mtd,
        ytd=ytd,
        one_year=one_year,
        holdings=holdings,
        allocation=allocation,
        ticker=ticker,
        accounts=accounts,
        activity=activity,
        chart_pct=chart_pct,
        chart_dates=chart_dates,
        chart_monthly=chart_monthly,
    )


# ── braille monthly bar chart ──────────────────────────────────────────────────

def _braille_monthly_bars(
    months: list[tuple[str, float]],
    width: int,
    height: int,
) -> tuple[list[Text], Text]:
    """12-column braille bar chart. Positive months green (up), negative red (down)."""
    dot_h = height * 4
    _blank = [Text(chr(0x2800) * width, style="dim") for _ in range(height)]
    if not months or width < len(months) or dot_h < 4:
        return _blank, Text(" " * width, style="dim")

    n     = len(months)
    vals  = [v for _, v in months]
    max_pos = max((v for v in vals if v > 0), default=0.01)
    max_neg = max((-v for v in vals if v < 0), default=0.01)

    # Place zero proportionally between positive and negative extents
    pos_dots = max(2, round(dot_h * max_pos / (max_pos + max_neg)))
    neg_dots = dot_h - pos_dots
    zero_dot = neg_dots  # dot index of the zero line (0 = very bottom)

    # Layout: bar_w chars per bar, 1-char gap, centred
    gap   = 1
    bar_w = max(1, (width - gap * (n - 1)) // n)
    left_pad = (width - (bar_w * n + gap * (n - 1))) // 2

    def _bar_cxs(i: int) -> tuple[int, int]:
        s = left_pad + i * (bar_w + gap)
        return s, s + bar_w

    # Dot height per bar (positive = above zero, negative = below zero)
    dot_sizes: list[int] = []
    for _, pct in months:
        if pct > 0:
            dot_sizes.append(max(1, round(pct / max_pos * (pos_dots - 1))))
        elif pct < 0:
            dot_sizes.append(-max(1, round(-pct / max_neg * (neg_dots - 1))))
        else:
            dot_sizes.append(0)

    rows: list[Text] = []
    for cy in range(height):
        dot_base     = (height - 1 - cy) * 4  # lowest dot index in this char row
        is_zero_cell = dot_base <= zero_dot <= dot_base + 3
        t = Text()

        for cx in range(width):
            bar_idx: int | None = None
            for i in range(n):
                bs, be = _bar_cxs(i)
                if bs <= cx < be:
                    bar_idx = i
                    break

            if bar_idx is None:
                # Gap or outer padding — draw axis tick on zero row
                t.append("─" if is_zero_cell else " ", style="dim")
                continue

            ds   = dot_sizes[bar_idx]
            bits = 0
            if ds > 0:
                bar_top = zero_dot + ds
                for dy in range(dot_base, dot_base + 4):
                    if zero_dot <= dy < bar_top:
                        r = 3 - (dy - dot_base)
                        bits |= _BR_LEFT[r] | _BR_RIGHT[r]
            elif ds < 0:
                bar_bot = max(0, zero_dot + ds)
                for dy in range(dot_base, dot_base + 4):
                    if bar_bot <= dy < zero_dot:
                        r = 3 - (dy - dot_base)
                        bits |= _BR_LEFT[r] | _BR_RIGHT[r]

            if not bits:
                if is_zero_cell:
                    zr   = 3 - (zero_dot - dot_base)
                    bits = _BR_LEFT[zr] | _BR_RIGHT[zr]
                    t.append(chr(0x2800 + bits), style="dim")
                else:
                    t.append(chr(0x2800), style="dim")
                continue

            # Gradient: bright at peak end, dim near zero
            cell_mid = dot_base + 1
            if ds > 0:
                frac = (cell_mid - zero_dot) / ds
            else:
                frac = (zero_dot - 1 - cell_mid) / (-ds)
            frac = max(0.0, min(1.0, frac))

            if ds > 0:
                style = f"bold {PERF_GREAT}" if frac > 0.70 else PERF_GOOD if frac > 0.35 else f"dim {PERF_GOOD}"
            else:
                style = f"bold {PERF_TERRIBLE}" if frac > 0.70 else PERF_POOR if frac > 0.35 else f"dim {PERF_POOR}"

            t.append(chr(0x2800 + bits), style=style)

        rows.append(t)

    # X-axis: 3-char month abbreviations centred under each bar
    line = [" "] * width
    for i, (label, _) in enumerate(months):
        bs, be = _bar_cxs(i)
        cx = bs + max(0, (be - bs - 3) // 2)
        for j, c in enumerate(label[:3]):
            if 0 <= cx + j < width:
                line[cx + j] = c

    return rows, Text("".join(line), style="dim")


# ── rendering ──────────────────────────────────────────────────────────────────

def _perf_cell(label: str, s: PerfStats, cur: str, privacy: bool) -> Text:
    color = PERF_GOOD if s.pct >= 0 else PERF_POOR
    arrow = "▲" if s.pct >= 0 else "▼"
    t = Text()
    t.append(f"{label}\n", style="dim")
    t.append(f"{arrow} {abs(s.pct):.2f}%\n", style=f"bold {color}")
    t.append(_MASK if privacy else _fmt_delta(s.abs, cur), style=f"dim {color}")
    return t


def _render_detail(data: DetailData, width: int, privacy: bool) -> Group:
    cur  = data.base_currency
    sym  = _CURRENCY_SYMBOLS.get(cur, f"{cur} ")
    wide = width >= 52

    # ── net worth + goal bar ──────────────────────────────────────────────────
    dot_color = (
        PERF_FLAT if abs(data.today.pct) <= 0.1
        else (PERF_GOOD if data.today.pct > 0 else PERF_POOR)
    )
    progress_pct = min(data.total_value / data.goal * 100, 100.0) if data.goal else 0.0
    bar_w = max(8, width - 38)
    filled = round(progress_pct / 100 * bar_w)

    nw_line = Text()
    nw_line.append("● ", style=f"bold {dot_color}")
    nw_line.append(sym, style="bold dim")
    nw_line.append(_MASK if privacy else f"{data.total_value:,.0f}", style="bold")
    nw_line.append("  ")
    nw_line.append("█" * filled, style=ACCENT)
    nw_line.append("░" * (bar_w - filled), style="dim")
    nw_line.append(f"  {progress_pct:.1f}%", style="dim")
    nw_line.append(f"  → {sym}", style="dim")
    nw_line.append(_MASK if privacy else _fmt_goal(data.goal), style="dim")

    # ── performance grid ─────────────────────────────────────────────────────
    perf_cells = [("Today", data.today), ("WTD", data.wtd), ("MTD", data.mtd), ("YTD", data.ytd)]
    perf = Table.grid(expand=True, padding=(0, 2))
    for _ in perf_cells:
        perf.add_column(ratio=1)
    perf.add_row(*[_perf_cell(lbl, s, cur, privacy) for lbl, s in perf_cells])

    # ── 1Y monthly bar chart ──────────────────────────────────────────────────
    chart_h  = 6 if wide else 4
    cw       = max(4, width - 2)
    yr_up    = data.one_year.pct >= 0
    yr_arrow = "▲" if yr_up else "▼"
    yr_color = PERF_GOOD if yr_up else PERF_POOR

    yr_line = Text()
    yr_line.append(f"{yr_arrow} {abs(data.one_year.pct):.2f}%", style=f"bold {yr_color}")
    yr_line.append(f"  {_fmt_delta(data.one_year.abs, cur)}", style=yr_color)

    chart_rows, x_axis = _braille_monthly_bars(data.chart_monthly, cw, chart_h)

    # ── top movers today ──────────────────────────────────────────────────────
    movers_parts: list[Any] = []
    if data.ticker:
        ranked   = sorted(data.ticker, key=lambda t: t.change_pct, reverse=True)
        gainers  = [t for t in ranked if t.change_pct > 0.05][:2]
        losers   = [t for t in reversed(ranked) if t.change_pct < -0.05][:2]
        mv_grid  = Table.grid(expand=True, padding=(0, 1))
        n_cols   = (len(gainers) + len(losers)) or 1
        for _ in range(n_cols):
            mv_grid.add_column(ratio=1)
        cells: list[Text] = []
        for t in gainers:
            c = Text()
            c.append(f"▲ {t.symbol}", style=f"bold {PERF_GREAT}")
            c.append(f"  +{t.change_pct:.2f}%", style=PERF_GOOD)
            cells.append(c)
        for t in losers:
            c = Text()
            c.append(f"▼ {t.symbol}", style=f"bold {PERF_TERRIBLE}")
            c.append(f"  {t.change_pct:.2f}%", style=PERF_POOR)
            cells.append(c)
        if cells:
            mv_grid.add_row(*cells)
            movers_parts = [Rule(title=" TODAY'S MOVERS", style="dim", align="left"), mv_grid, Text("")]

    # ── holdings table ────────────────────────────────────────────────────────
    _S = Text(" ")  # separator cell
    hdg = Table(
        show_header=True,
        header_style="bold dim",
        show_edge=False,
        pad_edge=False,
        padding=(0, 0),
        box=None,
        expand=True,
    )
    hdg.add_column("Symbol", no_wrap=True, width=7)
    hdg.add_column("",       no_wrap=True, width=1)
    hdg.add_column("Name",   no_wrap=True, ratio=2)
    hdg.add_column("",       no_wrap=True, width=1)
    hdg.add_column("Return", no_wrap=True, justify="right", width=8)
    hdg.add_column("",       no_wrap=True, width=1)
    hdg.add_column("Day",    no_wrap=True, justify="right", width=7)
    hdg.add_column("",       no_wrap=True, width=1)
    hdg.add_column("Value",  no_wrap=True, justify="right", width=8)
    hdg.add_column("",       no_wrap=True, width=1)
    hdg.add_column("Alloc",  no_wrap=True, justify="right", width=5)

    for h in data.holdings:
        _, cls_color = _ASSET_CLASS.get(h.asset_class.upper(), ("", ""))
        ret_color = PERF_GOOD if h.total_return_pct >= 0 else PERF_POOR
        ret_arrow = "▲" if h.total_return_pct >= 0 else "▼"
        ret_str   = f"{ret_arrow}{abs(h.total_return_pct):.1f}%"

        day_t = Text()
        if h.today_pct is not None:
            arr   = "▲" if h.today_pct > 0.05 else ("▼" if h.today_pct < -0.05 else "─")
            day_t = Text(f"{arr}{abs(h.today_pct):.2f}%", style=_ticker_color(h.today_pct))
        val_str  = _MASK if privacy else f"{sym}{h.value:,.0f}"
        hdg.add_row(
            Text(h.symbol,  style=f"bold {cls_color}"),
            _S,
            Text(h.name,    style="dim"),
            _S,
            Text(ret_str,   style=ret_color),
            _S,
            day_t,
            _S,
            Text(val_str,   style="dim"),
            _S,
            Text(f"{h.allocation_pct:.1f}%", style="dim"),
        )

    # ── asset class allocation bars ───────────────────────────────────────────
    label_w  = max((len(a.label) for a in data.allocation), default=8) + 2
    bar_area = max(10, width - label_w - 8)
    alloc_rows: list[Text] = []
    for ac in data.allocation:
        filled2 = max(1, round(ac.pct / 100 * bar_area))
        row = Text()
        row.append(f"{ac.label:<{label_w}}", style="dim")
        row.append("[", style="dim")
        row.append("█" * filled2,              style=ac.color or "cyan")
        row.append("░" * (bar_area - filled2), style="dim")
        row.append("]", style="dim")
        row.append(f" {ac.pct:.0f}%",          style="dim")
        alloc_rows.append(row)

    # ── accounts table ────────────────────────────────────────────────────────
    acct_tbl = Table(
        show_header=True,
        header_style="bold dim",
        show_edge=False,
        pad_edge=False,
        box=None,
        expand=True,
    )
    acct_tbl.add_column("Account",   no_wrap=True, ratio=2)
    acct_tbl.add_column("Cash",      no_wrap=True, justify="right", width=10)
    acct_tbl.add_column("Positions", no_wrap=True, justify="right", width=10)
    acct_tbl.add_column("Total",     no_wrap=True, justify="right", width=10)

    for a in data.accounts:
        name_str = (a.name[:20] + "…") if len(a.name) > 21 else a.name
        acct_tbl.add_row(
            Text(name_str, style="dim"),
            Text(_MASK if privacy else f"{sym}{a.cash:,.0f}",      style="dim"),
            Text(_MASK if privacy else f"{sym}{a.positions:,.0f}", style="dim"),
            Text(_MASK if privacy else f"{sym}{a.total:,.0f}",     style="bold"),
        )
    if not data.accounts:
        acct_tbl.add_row(Text("—", style="dim"), Text(""), Text(""), Text(""))

    # ── activity table ────────────────────────────────────────────────────────
    act_tbl = Table(
        show_header=True,
        header_style="bold dim",
        show_edge=False,
        pad_edge=False,
        box=None,
        expand=True,
    )
    act_tbl.add_column("Month",  no_wrap=True, ratio=2)
    act_tbl.add_column("Trades", no_wrap=True, justify="right", width=7)
    act_tbl.add_column("Perf",   no_wrap=True, justify="right", width=9)
    act_tbl.add_column("Value",  no_wrap=True, justify="right", width=10)

    for ma in data.activity:
        color = PERF_GOOD if ma.perf_pct >= 0 else PERF_POOR
        arrow = "▲" if ma.perf_pct >= 0 else "▼"
        act_tbl.add_row(
            Text(ma.label, style="dim"),
            Text(str(ma.trades), style="dim"),
            Text(f"{arrow}{abs(ma.perf_pct):.2f}%", style=color),
            Text(_MASK if privacy else _fmt_delta(ma.perf_abs, cur), style=f"dim {color}"),
        )

    n = len(data.holdings)
    return Group(
        nw_line,
        Rule(style="dim"),
        perf,
        Text(""),
        Rule(title=" 1 YEAR", style="dim", align="left"),
        yr_line,
        *chart_rows,
        x_axis,
        Text(""),
        *movers_parts,
        Rule(title=" ACTIVITY", style="dim", align="left"),
        act_tbl,
        Text(""),
        Rule(title=f" HOLDINGS ({n})", style="dim", align="left"),
        hdg,
        Text(""),
        Rule(title=" ACCOUNTS", style="dim", align="left"),
        acct_tbl,
    )


# ── widget ─────────────────────────────────────────────────────────────────────

class GhostfolioDetailWidget(DashWidget):
    """Extended portfolio detail — full holdings, allocation breakdown, summary."""

    _mobile_scrollable = True
    can_focus = True

    data: reactive[DetailData | None] = reactive(None, always_update=True)

    DEFAULT_CSS = """
    GhostfolioDetailWidget { height: 100%; }
    GhostfolioDetailWidget ScrollableContainer { height: 1fr; }
    GhostfolioDetailWidget #gfd-body   { height: auto; padding: 0 1; }
    GhostfolioDetailWidget #gfd-rule   { height: 1; }
    GhostfolioDetailWidget #gfd-ticker { height: 1; padding: 0 1; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client:          GhostfolioClient | None = None
        self._err:             str | None              = None
        self._privacy:         bool                    = False
        self._data_timer:      Timer | None            = None
        self._ticker_timer:    Timer | None            = None
        self._ticker_tick:     int                     = 0
        self._resize_pending:  bool                    = False

    def compose(self) -> ComposeResult:
        with ScrollableContainer() as sc:
            sc.can_focus = False
            yield Static("[dim]Loading…[/dim]", id="gfd-body")
        yield Static(Rule(style="dim"), id="gfd-rule")
        yield Static("", id="gfd-ticker")

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
        self._data_timer = self.set_interval(float(seconds), self._load)

    @work(thread=True)
    def _load(self) -> None:
        if self._client is None:
            return
        try:
            data = _fetch_detail(self._client)
            self.app.call_from_thread(self._show_data, data)
        except Exception as exc:
            self.app.call_from_thread(self._show_error, str(exc))

    def _show_data(self, data: DetailData) -> None:
        self._err = None
        self.data = data

    def _show_error(self, msg: str) -> None:
        self._err = msg
        self.query_one("#gfd-body", Static).update(f"[red]Error:[/red] {msg}")

    def on_resize(self) -> None:
        self._resize_pending = True

    def set_privacy(self, value: bool) -> None:
        self._privacy = value
        if self.data is not None and not self._err:
            self._redraw()

    def _ticker_width(self) -> int:
        return max(20, self.content_size.width or 80)

    def _advance_ticker(self) -> None:
        self._ticker_tick += 1
        if self._resize_pending and self.data and not self._err:
            w = self.content_size.width
            if w > 0:
                self._resize_pending = False
                self._redraw()
        if self.data and self.data.ticker:
            self.query_one("#gfd-ticker", Static).update(
                _render_ticker(self.data.ticker, self._ticker_tick, self._ticker_width())
            )

    def _redraw(self) -> None:
        if self.data is None:
            return
        self.query_one("#gfd-body", Static).update(
            _render_detail(self.data, self.content_size.width or 80, self._privacy)
        )

    def watch_data(self, data: DetailData | None) -> None:
        if data is None or self._err:
            return
        self._redraw()
