from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any

import requests
from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import ScrollableContainer
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static
from textual import work

from .. import config
from ..theme import ACCENT, PERF_TERRIBLE
from .base import DashWidget

_API_BASE = "https://api.tailscale.com/api/v2"


# ── data model ────────────────────────────────────────────────────────────────

@dataclass
class TsDevice:
    name:      str
    ip:        str
    os:        str
    online:    bool
    last_seen: str
    exit_node: bool = False


@dataclass
class TsService:
    name: str
    ip:   str


@dataclass
class TailscaleData:
    devices:  list[TsDevice]  = field(default_factory=list)
    services: list[TsService] = field(default_factory=list)
    error:    str              = ""


# ── helpers ───────────────────────────────────────────────────────────────────

def _short_name(fqdn: str, fallback: str) -> str:
    label = fqdn.rstrip(".").split(".")[0]
    return label or fallback


def _ipv4(addresses: list[str]) -> str:
    return next((a for a in addresses if ":" not in a), addresses[0] if addresses else "—")


def _fmt_last_seen(iso: str) -> str:
    if not iso or iso.startswith("0001"):
        return "—"
    try:
        dt    = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        delta = int((datetime.now(timezone.utc) - dt).total_seconds())
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{delta // 60}m ago"
        if delta < 86400:
            return f"{delta // 3600}h ago"
        return f"{delta // 86400}d ago"
    except Exception:
        return iso[:10]


# ── fetch ─────────────────────────────────────────────────────────────────────

def _fetch_tailscale(api_key: str) -> TailscaleData:
    headers = {"Authorization": f"Bearer {api_key}"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_dev = pool.submit(
            requests.get,
            f"{_API_BASE}/tailnet/-/devices",
            headers=headers,
            params={"fields": "all"},
            timeout=10,
        )
        f_svc = pool.submit(
            requests.get,
            f"{_API_BASE}/tailnet/-/services",
            headers=headers,
            timeout=10,
        )

    r_dev = f_dev.result()
    r_dev.raise_for_status()

    devices: list[TsDevice] = []
    for d in r_dev.json().get("devices", []):
        addrs     = d.get("addresses") or []
        last_seen = d.get("lastSeen", "")
        if d.get("connectedToControl"):
            last_seen = ""
        devices.append(TsDevice(
            name      = _short_name(d.get("name", ""), d.get("hostname", "?")),
            ip        = _ipv4(addrs),
            os        = d.get("os", "?"),
            online    = bool(d.get("connectedToControl")),
            last_seen = last_seen,
            exit_node = "0.0.0.0/0" in (d.get("advertisedRoutes") or []),
        ))
    devices.sort(key=lambda d: (not d.online, d.name))

    services: list[TsService] = []
    r_svc = f_svc.result()
    if r_svc.ok:
        for s in r_svc.json().get("vipServices", []):
            name  = s.get("name", "").replace("svc:", "")
            addrs = s.get("addrs") or []
            services.append(TsService(
                name = name,
                ip   = _ipv4(addrs),
            ))
    services.sort(key=lambda s: s.name)

    return TailscaleData(devices=devices, services=services)


# ── rendering ─────────────────────────────────────────────────────────────────

def _build_devices_table(devices: list[TsDevice]) -> Table:
    tbl = Table.grid(padding=(0, 1, 0, 0))
    tbl.pad_edge = False
    tbl.add_column(width=2,  no_wrap=True)
    tbl.add_column(width=16, no_wrap=True)
    tbl.add_column(width=15, no_wrap=True)
    tbl.add_column(width=7,  no_wrap=True)
    tbl.add_column(no_wrap=True)

    tbl.add_row(
        Text(""),
        Text("DEVICE",    style="bold dim"),
        Text("IP",        style="bold dim"),
        Text("OS",        style="bold dim"),
        Text("LAST SEEN", style="bold dim"),
    )

    for d in devices:
        dot        = "●︎" if d.online else "○︎"
        seen       = "online" if d.online else _fmt_last_seen(d.last_seen)
        dim        = "" if d.online else "dim"
        name_trunc = 13 if d.exit_node else 16
        name_text  = Text(d.name[:name_trunc], style=f"bold {dim}".strip())
        if d.exit_node:
            name_text.append(" ↗︎", style=f"bold {ACCENT}")
        tbl.add_row(
            Text(dot,      style=f"bold {ACCENT}" if d.online else f"dim {PERF_TERRIBLE}"),
            name_text,
            Text(d.ip,     style="dim"),
            Text(d.os[:7], style="dim"),
            Text(seen,     style=f"dim {ACCENT}" if d.online else "dim"),
        )
    return tbl


def _build_services_table(services: list[TsService]) -> Table:
    tbl = Table.grid(padding=(0, 1, 0, 0))
    tbl.pad_edge = False
    tbl.add_column(width=2,  no_wrap=True)
    tbl.add_column(width=16, no_wrap=True)
    tbl.add_column(no_wrap=True)

    tbl.add_row(
        Text(""),
        Text("SERVICE", style="bold dim"),
        Text("VIP",     style="bold dim"),
    )

    for s in services:
        tbl.add_row(
            Text("◆︎",         style=f"bold {ACCENT}"),
            Text(s.name[:16], style="bold"),
            Text(s.ip,        style="dim"),
        )
    return tbl


def _render_tailscale(td: TailscaleData, mobile: bool = False) -> Group:
    if td.error:
        return Group(Text(td.error, style=f"dim {PERF_TERRIBLE}"))

    dev_tbl = _build_devices_table(td.devices)
    svc_tbl = _build_services_table(td.services)

    if mobile or not td.services:
        parts: list[Any] = [dev_tbl]
        if td.services:
            parts += [Text(""), svc_tbl]
        return Group(*parts)

    outer = Table.grid(expand=True, padding=(0, 1, 0, 0))
    outer.pad_edge = False
    outer.add_column(ratio=1)
    outer.add_column(ratio=1)
    outer.add_row(dev_tbl, svc_tbl)
    return Group(outer)


# ── widget ────────────────────────────────────────────────────────────────────

class TailscaleWidget(DashWidget):
    """Tailscale network: devices and VIP services."""

    _mobile_scrollable = True
    data: reactive[TailscaleData | None] = reactive(None, always_update=True)

    DEFAULT_CSS = """
    TailscaleWidget             { height: 1fr; width: 1fr; }
    TailscaleWidget #ts-scroll  { height: 1fr; }
    TailscaleWidget Static      { height: auto; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._api_key: str = ""
        self._data_timer: Timer | None = None
        self._initial_load_done: bool = False

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="ts-scroll"):
            yield Static("[dim]loading…[/dim]")

    def on_mount(self) -> None:
        self.border_title = "  Tailscale"
        self._api_key = config.get("TUIDASH_TAILSCALE_KEY") or ""
        if not self._api_key:
            self.border_subtitle = "no key"
            self.query_one(Static).update(
                Text("Set TUIDASH_TAILSCALE_KEY to enable", style="dim")
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
            td = _fetch_tailscale(self._api_key)
        except Exception as exc:
            td = TailscaleData(error=str(exc))
        self.app.call_from_thread(self._show_data, td)

    def _show_data(self, td: TailscaleData) -> None:
        self.data = td

    def _redraw(self) -> None:
        if self.data is None:
            return
        td     = self.data
        mobile = self.screen.has_class("mobile")
        if td.error:
            self.border_subtitle = "error"
        else:
            online = sum(1 for d in td.devices if d.online)
            total  = len(td.devices)
            self.border_subtitle = f"{online}/{total} online · {len(td.services)} services"
        self.query_one(Static).update(_render_tailscale(td, mobile=mobile))

    def watch_data(self, td: TailscaleData | None) -> None:
        if td is None:
            return
        self._redraw()

    def on_resize(self) -> None:
        self.call_after_refresh(self._redraw)
