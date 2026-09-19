from __future__ import annotations

import platform
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests
from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static
from textual import work

from .. import config
from ..scroll import SCROLL_INTERVAL, current_tick, scroll_offset
from ..theme import PERF_GREAT, PERF_TERRIBLE
from .base import DashWidget, accent_gradient_bar
from .homelab import ContainerDetail, _STOPPED_STATES, _fetch_host_stats, _fetch_portainer_containers, _fetch_portainer_endpoints


_BAR_W = 8


# ── data model ────────────────────────────────────────────────────────────────

@dataclass
class HostData:
    name:      str
    url:       str            # Glances base URL, or a pingable hostname for source="influx"
    source:    str = "glances"  # "glances" | "influx" — scarif has no Glances agent (bare-metal Proxmox)
    reachable: bool            = False
    rtt_ms:    float | None    = None
    cpu_pct:   float | None    = None
    mem_pct:   float | None    = None
    fetch_err: str             = ""


# ── probes ────────────────────────────────────────────────────────────────────

def _ping_host(hostname: str) -> tuple[bool, float | None]:
    cmd = (
        ["ping", "-c", "1", "-W", "2000", hostname]
        if platform.system() == "Darwin"
        else ["ping", "-c", "1", "-W", "2", hostname]
    )
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            m = re.search(r"time=(\d+\.?\d*)\s*ms", result.stdout)
            return True, float(m.group(1)) if m else None
        return False, None
    except Exception:
        return False, None


def _fetch_glances(base_url: str) -> tuple[float | None, float | None]:
    base = base_url.rstrip("/")
    # Try Glances v4 first, fall back to v3
    for v in ("4", "3"):
        api = f"{base}/api/{v}"
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                f_cpu = pool.submit(requests.get, f"{api}/cpu", timeout=8)
                f_mem = pool.submit(requests.get, f"{api}/mem", timeout=8)

            r_cpu = f_cpu.result()
            if r_cpu.status_code == 404:
                continue  # try next version
            r_cpu.raise_for_status()

            r_mem = f_mem.result()
            r_mem.raise_for_status()

            return r_cpu.json().get("total"), r_mem.json().get("percent")
        except Exception:
            continue

    raise RuntimeError("unreachable")


def _monitor_host(name: str, url: str) -> HostData:
    hd = HostData(name=name, url=url, source="glances")
    hostname = urlparse(url).hostname or url
    hd.reachable, hd.rtt_ms = _ping_host(hostname)
    try:
        hd.cpu_pct, hd.mem_pct = _fetch_glances(url)
    except Exception as exc:
        hd.fetch_err = str(exc)
    return hd


def _monitor_host_influx(name: str, ping_hostname: str) -> HostData:
    """For hosts with no Glances agent (scarif — bare-metal Proxmox): reuse the same
    InfluxDB CPU/mem query the Homelab page's host cards use, still pinged directly
    for reachability/RTT since scarif is a normal LAN/Tailscale host."""
    hd = HostData(name=name, url=ping_hostname, source="influx")
    hd.reachable, hd.rtt_ms = _ping_host(ping_hostname)
    try:
        _reporting, hd.cpu_pct, hd.mem_pct, _disk = _fetch_host_stats(name)
    except Exception as exc:
        hd.fetch_err = str(exc)
    return hd


# Curated cross-environment shortlist — not "every container" (that's the Homelab
# page's job), just the ones whose outage would actually matter day-to-day.
_CRITICAL_SERVICES = [
    ("pihole",        "endor"),
    ("proxy-manager", "bespin"),
    ("proxy-manager", "scarif-proxy"),
    ("ghostfolio",    "bespin"),
    ("relay",         "bespin"),
    ("pocket-id",     "bespin"),
]


def _fetch_critical_services() -> list[ContainerDetail]:
    host      = config.require("TUIDASH_PORTAINER_HOST").rstrip("/")
    endpoints = {e["Name"]: e["Id"] for e in _fetch_portainer_endpoints()}

    by_env: dict[str, dict[str, ContainerDetail]] = {}
    for env in {e for _, e in _CRITICAL_SERVICES}:
        endpoint_id = endpoints.get(env)
        if endpoint_id is not None:
            by_env[env] = {c.name: c for c in _fetch_portainer_containers(host, endpoint_id)}

    out: list[ContainerDetail] = []
    for name, env in _CRITICAL_SERVICES:
        label = f"{name}@{env.removeprefix('scarif-')}"
        c = by_env.get(env, {}).get(name)
        out.append(ContainerDetail(name=label, status="missing", health="") if c is None
                    else ContainerDetail(name=label, status=c.status, health=c.health))
    return out


# ── rendering ─────────────────────────────────────────────────────────────────

def _pct_bar(pct: float | None) -> Text:
    if pct is None:
        return Text("?" * _BAR_W, style="dim")
    filled = round(pct / 100 * _BAR_W)
    return accent_gradient_bar(filled, _BAR_W)


def _render_host(hd: HostData) -> Table:
    dot_color = PERF_GREAT if hd.reachable else PERF_TERRIBLE

    # Left: name + ping
    left = Text()
    left.append("●︎", style=f"bold {dot_color}")
    left.append(f" {hd.name}", style="bold")
    if hd.reachable and hd.rtt_ms is not None:
        left.append(f"  {hd.rtt_ms:.0f}ms", style="dim")
    elif not hd.reachable:
        left.append("  unreachable", style=f"dim {PERF_TERRIBLE}")

    # Right: CPU + MEM bars (right-aligned via ratio=1 on left column)
    right = Text()
    if hd.cpu_pct is not None or hd.mem_pct is not None:
        right.append("CPU ", style="dim")
        right.append_text(_pct_bar(hd.cpu_pct))
        right.append(f"{hd.cpu_pct:3.0f}%" if hd.cpu_pct is not None else "  ?%", style="dim")
        right.append("   MEM ", style="dim")
        right.append_text(_pct_bar(hd.mem_pct))
        right.append(f"{hd.mem_pct:3.0f}%" if hd.mem_pct is not None else "  ?%", style="dim")
    elif hd.fetch_err:
        right.append("stats unavailable", style=f"dim {PERF_TERRIBLE}")

    row = Table.grid(expand=True, padding=(0, 0))
    row.add_column(ratio=1)
    row.add_column(no_wrap=True)
    row.add_row(left, right)
    return row


def _render_hosts(hosts: list[HostData]) -> Table:
    outer = Table.grid(expand=True)
    outer.add_column(ratio=1)
    for hd in hosts:
        outer.add_row(_render_host(hd))
    return outer


def _service_color(c: ContainerDetail) -> str:
    if c.status == "missing" or c.health == "unhealthy":
        return PERF_TERRIBLE
    if c.status.lower() in _STOPPED_STATES:
        return "dim"
    if c.health == "healthy":
        return PERF_GREAT
    return ""


def _scroll_services(services: list[ContainerDetail], width: int, tick: int) -> Text:
    """Boomerang-scroll all services on one line, preserving per-service colour."""
    segments: list[tuple[str, str]] = []
    for i, c in enumerate(services):
        if i:
            segments.append(("   ", ""))
        segments.append((c.name, _service_color(c)))

    full_len = sum(len(s) for s, _ in segments)
    overflow = full_len - width
    if overflow <= 0:
        t = Text()
        for seg, style in segments:
            t.append(seg, style=style)
        return t

    offset = scroll_offset(tick, 0, overflow)
    t        = Text()
    char_pos = 0
    for seg, style in segments:
        seg_end = char_pos + len(seg)
        vis_s   = max(offset, char_pos)
        vis_e   = min(offset + width, seg_end)
        if vis_s < vis_e:
            t.append(seg[vis_s - char_pos : vis_e - char_pos], style=style)
        char_pos = seg_end
    return t


# ── widget ────────────────────────────────────────────────────────────────────

class HostsWidget(DashWidget):
    """Host monitoring via ping + Glances (CPU, MEM) — plus one InfluxDB-sourced
    host (TUIDASH_HOMELAB_CENTRAL, scarif) that has no Glances agent to poll."""

    data: reactive[list[HostData] | None] = reactive(None, always_update=True)

    DEFAULT_CSS = """
    HostsWidget { height: auto; }
    #hosts-body { height: auto; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._hosts: list[tuple[str, str, str]] = []
        self._services: list[ContainerDetail] = []
        self._services_err: str = ""
        self._data_timer: Timer | None = None
        self._scroll_timer: Timer | None = None
        self._tick: int = 0
        self._scroll_epoch: int = 0

    def compose(self) -> ComposeResult:
        yield Static("[dim]Loading…[/dim]", id="hosts-body")

    def on_mount(self) -> None:
        hosts: list[tuple[str, str, str]] = []
        central = config.get("TUIDASH_HOMELAB_CENTRAL") or ""
        if central:
            hosts.append((central, f"{central}.dala-aldebaran.ts.net", "influx"))

        raw  = config.get("TUIDASH_HOSTS", "") or ""
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        hosts += [(_name_from_url(url), url, "glances") for url in urls]

        if not hosts:
            self.query_one("#hosts-body", Static).update(
                "[dim]No hosts configured — set TUIDASH_HOSTS[/dim]"
            )
            return
        self._hosts = hosts
        self._load()
        self._scroll_timer = self.set_interval(SCROLL_INTERVAL, self._advance_scroll)

    def set_refresh_interval(self, seconds: int) -> None:
        if self._data_timer is not None:
            self._data_timer.stop()
        self._data_timer = self.set_interval(float(seconds), self._load)

    def _advance_scroll(self) -> None:
        self._tick = current_tick() - self._scroll_epoch
        if self.data is not None:
            self._redraw()

    def reset_scroll(self) -> None:
        self._scroll_epoch = current_tick()
        self._tick = 0
        if self.data is not None:
            self._redraw()

    def _line_width(self) -> int:
        content_w = self.content_size.width or 60
        return max(10, content_w - 4)  # subtract "  ▣ " prefix

    def _redraw(self) -> None:
        if self.data is None:
            return
        parts: list[Any] = [_render_hosts(self.data)]
        if self._services_err:
            parts.append(Text(f"  services unavailable: {self._services_err}", style=f"dim {PERF_TERRIBLE}"))
        elif self._services:
            line = Text("  ▣ ", style="dim")
            line.append_text(_scroll_services(self._services, self._line_width(), self._tick))
            parts.append(line)
        self.query_one("#hosts-body", Static).update(Group(*parts))

    @work(thread=True)
    def _load(self) -> None:
        if not self._hosts:
            return

        def _fetch_one(h: tuple[str, str, str]) -> HostData:
            name, target, source = h
            return _monitor_host_influx(name, target) if source == "influx" else _monitor_host(name, target)

        with ThreadPoolExecutor(max_workers=len(self._hosts)) as pool:
            results = list(pool.map(_fetch_one, self._hosts))

        try:
            services, services_err = _fetch_critical_services(), ""
        except Exception as exc:
            services, services_err = [], str(exc)

        self.app.call_from_thread(self._show_data, results, services, services_err)

    def _show_data(self, data: list[HostData], services: list[ContainerDetail], services_err: str) -> None:
        self._services     = services
        self._services_err = services_err
        self.data = data

    def watch_data(self, data: list[HostData] | None) -> None:
        if data is None:
            return
        self._redraw()

    def pause_animations(self) -> None:
        if self._scroll_timer is not None:
            self._scroll_timer.pause()

    def resume_animations(self) -> None:
        if self._scroll_timer is not None:
            self._scroll_timer.resume()


def _name_from_url(url: str) -> str:
    hostname = urlparse(url).hostname or url
    # Keep full hostname for bare IPs; take first label for proper hostnames
    parts = hostname.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return hostname
    return parts[0]
