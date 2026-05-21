from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import requests
from rich.console import RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static
from textual import work

from .. import config
from ..theme import ACCENT, PERF_TERRIBLE
from .base import DashWidget

_API_CLOUD   = "https://api.hetzner.cloud/v1"
_API_STORAGE = "https://api.hetzner.com/v1"


# ── data model ────────────────────────────────────────────────────────────────

@dataclass
class HetznerServer:
    name:             str
    status:           str
    location:         str
    ipv4:             str
    monthly_net:      float | None
    traffic_used_gb:  float
    traffic_total_gb: float


@dataclass
class HetznerStorageBox:
    name:        str
    status:      str
    location:    str
    server:      str
    used_bytes:  int
    total_bytes: int
    monthly_net: float | None


@dataclass
class HetznerData:
    servers:      list[HetznerServer]     = field(default_factory=list)
    storage_boxes: list[HetznerStorageBox] = field(default_factory=list)
    error:        str                     = ""


# ── helpers ───────────────────────────────────────────────────────────────────

def _bytes_to_gb(n: int | None) -> float:
    return (n or 0) / 1024**3


def _fmt_size(n: int) -> str:
    gb = n / 1024**3
    if gb >= 1024:
        return f"{gb / 1024:.1f} TB"
    return f"{gb:.0f} GB"


def _fmt_traffic(used_gb: float, total_gb: float) -> str:
    def _f(gb: float) -> str:
        return f"{gb / 1024:.1f} TB" if gb >= 1024 else f"{gb:.1f} GB"
    return f"{_f(used_gb)} / {_f(total_gb)}"


def _monthly_price(prices: list[dict], location_name: str) -> float | None:
    for p in prices:
        if p.get("location") == location_name:
            try:
                return float(p["price_monthly"]["net"])
            except (KeyError, ValueError, TypeError):
                pass
    if prices:
        try:
            return float(prices[0]["price_monthly"]["net"])
        except (KeyError, ValueError, TypeError):
            pass
    return None


# ── fetch ─────────────────────────────────────────────────────────────────────

def _get_pages(base: str, path: str, key: str, resource: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {key}"}
    items: list[dict] = []
    page = 1
    while True:
        r = requests.get(
            f"{base}/{path}", headers=headers,
            params={"per_page": 50, "page": page}, timeout=10,
        )
        r.raise_for_status()
        body = r.json()
        items.extend(body.get(resource, []))
        if not (body.get("meta", {}).get("pagination", {}).get("next_page")):
            break
        page += 1
    return items


def _fetch_hetzner(api_key: str) -> HetznerData:
    headers = {"Authorization": f"Bearer {api_key}"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_srv = pool.submit(_get_pages, _API_CLOUD,   "servers",       api_key, "servers")
        f_box = pool.submit(_get_pages, _API_STORAGE,  "storage_boxes", api_key, "storage_boxes")

    servers: list[HetznerServer] = []
    for s in f_srv.result():
        st       = s.get("server_type", {})
        loc      = s.get("location", {})
        pub      = s.get("public_net", {})
        loc_name = loc.get("name", "?")
        servers.append(HetznerServer(
            name             = s.get("name", "?"),
            status           = s.get("status", "?"),
            location         = loc_name,
            ipv4             = (pub.get("ipv4") or {}).get("ip", "—"),
            monthly_net      = _monthly_price(st.get("prices", []), loc_name),
            traffic_used_gb  = _bytes_to_gb(s.get("ingoing_traffic")) + _bytes_to_gb(s.get("outgoing_traffic")),
            traffic_total_gb = _bytes_to_gb(s.get("included_traffic")),
        ))
    servers.sort(key=lambda s: s.name)

    storage_boxes: list[HetznerStorageBox] = []
    for b in f_box.result():
        bt       = b.get("storage_box_type", {})
        loc      = b.get("location", {})
        loc_name = loc.get("name", "?")
        stats    = b.get("stats", {})
        storage_boxes.append(HetznerStorageBox(
            name        = b.get("name", "?"),
            status      = b.get("status", "?"),
            location    = loc_name,
            server      = b.get("server", "?"),
            used_bytes  = stats.get("size", 0),
            total_bytes = bt.get("size", 0),
            monthly_net = _monthly_price(bt.get("prices", []), loc_name),
        ))
    storage_boxes.sort(key=lambda b: b.name)

    return HetznerData(servers=servers, storage_boxes=storage_boxes)


# ── rendering ─────────────────────────────────────────────────────────────────

def _status_dot(status: str, ok_values: tuple[str, ...] = ("running", "active")) -> tuple[str, str]:
    if status in ok_values:
        return "●", f"bold {ACCENT}"
    if status in ("off", "stopped", "locked"):
        return "○", f"dim {PERF_TERRIBLE}"
    return "◌", "dim"


def _render_hetzner(hd: HetznerData, mobile: bool = False) -> RenderableType:
    if hd.error:
        return Text(hd.error, style=f"dim {PERF_TERRIBLE}")

    if not hd.servers and not hd.storage_boxes:
        return Text("No resources found", style="dim")

    host_w = 12 if mobile else 30
    name_w = 10 if mobile else 16

    # Separate columns for LOC and HOST so TRAFFIC/USED always start at the
    # same horizontal position regardless of section.
    tbl = Table.grid(expand=True, padding=(0, 1, 0, 0))
    tbl.pad_edge = False
    tbl.add_column(width=1,      no_wrap=True)  # dot
    tbl.add_column(width=name_w, no_wrap=True)  # name
    tbl.add_column(width=6,      no_wrap=True)  # LOC
    tbl.add_column(width=host_w, no_wrap=True)  # HOST (blank for servers)
    tbl.add_column(no_wrap=True, min_width=0)   # TRAFFIC / USED — flexible, can shrink
    tbl.add_column(width=9,      no_wrap=True)  # COST/MO — always reserved

    _E = Text("")

    # ── servers ───────────────────────────────────────────────────────────────
    if hd.servers:
        tbl.add_row(_E, Text("SERVER", style="bold dim"), Text("LOC", style="bold dim"),
                    Text("IP", style="bold dim"), Text("TRAFFIC", style="bold dim"),
                    Text("COST/MO", style="bold dim"))
        for s in hd.servers:
            dot, dot_style = _status_dot(s.status)
            dim     = "" if s.status == "running" else "dim"
            cost    = f"€{s.monthly_net:.2f}" if s.monthly_net is not None else "?"
            traffic = _fmt_traffic(s.traffic_used_gb, s.traffic_total_gb)
            tbl.add_row(
                Text(dot,             style=dot_style),
                Text(s.name[:name_w], style=f"bold {dim}".strip()),
                Text(s.location,      style="dim"),
                Text(s.ipv4,          style="dim"),
                Text(traffic,         style="dim"),
                Text(cost,            style=f"dim {ACCENT}"),
            )

    # ── storage boxes ─────────────────────────────────────────────────────────
    if hd.storage_boxes:
        tbl.add_row(_E, Text("STORAGE BOX", style="bold dim"), Text("LOC", style="bold dim"),
                    Text("HOST", style="bold dim"), Text("USED", style="bold dim"),
                    Text("COST/MO", style="bold dim"))
        for b in hd.storage_boxes:
            dot, dot_style = _status_dot(b.status, ok_values=("active",))
            used_pct = (b.used_bytes / b.total_bytes * 100) if b.total_bytes else 0
            if mobile:
                used_str = f"{_fmt_size(b.used_bytes)}/{_fmt_size(b.total_bytes)} ({used_pct:.0f}%)"
            else:
                used_str = f"{_fmt_size(b.used_bytes)} / {_fmt_size(b.total_bytes)}  ({used_pct:.0f}%)"
            cost = f"€{b.monthly_net:.2f}" if b.monthly_net is not None else "?"
            tbl.add_row(
                Text(dot,               style=dot_style),
                Text(b.name[:name_w],   style="bold"),
                Text(b.location,        style="dim"),
                Text(b.server[:host_w], style="dim"),
                Text(used_str,          style="dim"),
                Text(cost,              style=f"dim {ACCENT}"),
            )

    return tbl


# ── widget ────────────────────────────────────────────────────────────────────

class HetznerWidget(DashWidget):
    """Hetzner Cloud: servers and storage boxes."""

    data: reactive[HetznerData | None] = reactive(None, always_update=True)

    DEFAULT_CSS = """
    HetznerWidget        { height: auto; width: 1fr; }
    HetznerWidget Static { height: auto; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._api_key: str = ""
        self._data_timer: Timer | None = None
        self._initial_load_done: bool = False

    def compose(self) -> ComposeResult:
        yield Static("[dim]loading…[/dim]")

    def on_mount(self) -> None:
        self.border_title = "  Hetzner"
        self._api_key = config.get("TUIDASH_HETZNER_KEY") or ""
        if not self._api_key:
            self.border_subtitle = "no key"
            self.query_one(Static).update(
                Text("Set TUIDASH_HETZNER_KEY to enable", style="dim")
            )
            return

    def on_show(self) -> None:
        if not self._initial_load_done:
            self._initial_load_done = True
            if self._api_key:
                self._load()

    def set_refresh_interval(self, seconds: int) -> None:
        if self._data_timer is not None:
            self._data_timer.stop()
        self._data_timer = self.set_interval(float(seconds), self._load)

    @work(thread=True)
    def _load(self) -> None:
        if not self._api_key:
            return
        try:
            hd = _fetch_hetzner(self._api_key)
        except Exception as exc:
            hd = HetznerData(error=str(exc))
        self.app.call_from_thread(self._show_data, hd)

    def _show_data(self, hd: HetznerData) -> None:
        self.data = hd

    def _redraw(self) -> None:
        if self.data is None:
            return
        hd = self.data
        if hd.error:
            self.border_subtitle = "error"
        else:
            running = sum(1 for s in hd.servers if s.status == "running")
            boxes   = len(hd.storage_boxes)
            costs   = [r.monthly_net for r in [*hd.servers, *hd.storage_boxes] if r.monthly_net is not None]
            total   = f" · €{sum(costs):.2f}/mo" if costs else ""
            self.border_subtitle = f"{running}/{len(hd.servers)} running · {boxes} storage{total}"
        mobile = self.screen.has_class("mobile")
        self.query_one(Static).update(_render_hetzner(hd, mobile=mobile))

    def watch_data(self, hd: HetznerData | None) -> None:
        if hd is None:
            return
        self._redraw()

    def on_resize(self) -> None:
        self.call_after_refresh(self._redraw)
