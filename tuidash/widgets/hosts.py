from __future__ import annotations

import platform
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
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
from .base import DashWidget, neon_bar


_BAR_W = 8


# ── data model ────────────────────────────────────────────────────────────────

@dataclass
class ContainerInfo:
    name:   str
    status: str
    health: str = ""   # "healthy" | "unhealthy" | "starting" | "" (no healthcheck)


@dataclass
class HostData:
    name:       str
    url:        str
    reachable:  bool                   = False
    rtt_ms:     float | None           = None
    cpu_pct:    float | None           = None
    mem_pct:    float | None           = None
    containers: list[ContainerInfo]    = field(default_factory=list)
    glances_err: str                   = ""


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


def _fetch_glances(base_url: str) -> tuple[float | None, float | None, list[ContainerInfo]]:
    base = base_url.rstrip("/")
    # Try Glances v4 first, fall back to v3
    for v, containers_path in (("4", "containers"), ("3", "docker")):
        api = f"{base}/api/{v}"
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                f_cpu  = pool.submit(requests.get, f"{api}/cpu",             timeout=8)
                f_mem  = pool.submit(requests.get, f"{api}/mem",             timeout=8)
                f_cont = pool.submit(requests.get, f"{api}/{containers_path}", timeout=8)

            r_cpu = f_cpu.result()
            if r_cpu.status_code == 404:
                continue  # try next version
            r_cpu.raise_for_status()

            r_mem = f_mem.result()
            r_mem.raise_for_status()

            cpu_pct = r_cpu.json().get("total")
            mem_pct = r_mem.json().get("percent")

            containers: list[ContainerInfo] = []
            r_cont = f_cont.result()
            if r_cont.ok:
                raw = r_cont.json()
                items = raw if isinstance(raw, list) else raw.get("containers", [])
                for c in items:
                    if isinstance(c, dict):
                        status = c.get("status", "?")
                        # Glances may expose health as a dedicated field or
                        # Docker embeds it in status: "running (healthy)"
                        health = c.get("health", "")
                        if not health:
                            m = re.search(r"\((\w+)\)", status)
                            health = m.group(1) if m else ""
                        containers.append(ContainerInfo(
                            name=c.get("name", "?"),
                            status=status,
                            health=health.lower(),
                        ))

            return cpu_pct, mem_pct, containers
        except Exception:
            continue

    raise RuntimeError("unreachable")


def _monitor_host(name: str, url: str) -> HostData:
    hd = HostData(name=name, url=url)
    hostname = urlparse(url).hostname or url
    hd.reachable, hd.rtt_ms = _ping_host(hostname)
    try:
        hd.cpu_pct, hd.mem_pct, hd.containers = _fetch_glances(url)
    except Exception as exc:
        hd.glances_err = str(exc)
    return hd


# ── rendering ─────────────────────────────────────────────────────────────────

def _pct_bar(pct: float | None) -> Text:
    if pct is None:
        return Text("?" * _BAR_W, style="dim")
    return neon_bar(pct, _BAR_W)


def _container_color(c: ContainerInfo) -> str:
    if "running" not in c.status.lower():
        return "dim"
    if c.health == "healthy":
        return PERF_GREAT
    if c.health == "unhealthy":
        return PERF_TERRIBLE
    return ""


def _scroll_containers(
    containers: list[ContainerInfo],
    width: int,
    tick: int,
    phase: int,
) -> Text:
    """Render all containers as a single scrolling line, preserving per-container colours."""
    # Build a flat list of (text_segment, style) pairs
    segments: list[tuple[str, str]] = []
    for i, c in enumerate(containers):
        if i:
            segments.append(("  ", ""))
        segments.append((c.name, _container_color(c)))

    full_len = sum(len(s) for s, _ in segments)
    overflow = full_len - width

    if overflow <= 0:
        t = Text()
        for seg, style in segments:
            t.append(seg, style=style)
        return t

    offset = scroll_offset(tick, phase, overflow)

    # Slice [offset, offset+width) across the styled segments
    t       = Text()
    char_pos = 0
    for seg, style in segments:
        seg_end = char_pos + len(seg)
        vis_s   = max(offset, char_pos)
        vis_e   = min(offset + width, seg_end)
        if vis_s < vis_e:
            t.append(seg[vis_s - char_pos : vis_e - char_pos], style=style)
        char_pos = seg_end

    return t


def _render_host(
    hd: HostData,
    tick: int = 0,
    host_idx: int = 0,
    cont_width: int = 60,
) -> Group:
    dot_color = PERF_GREAT if hd.reachable else PERF_TERRIBLE

    # Left: name + ping
    left = Text()
    left.append("●", style=f"bold {dot_color}")
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
    elif hd.glances_err:
        right.append("glances unavailable", style=f"dim {PERF_TERRIBLE}")

    row1 = Table.grid(expand=True, padding=(0, 0))
    row1.add_column(ratio=1)
    row1.add_column(no_wrap=True)
    row1.add_row(left, right)

    parts: list[Any] = [row1]

    # Line 2: all containers, scrolling, colour-coded by health
    if hd.containers:
        phase   = (host_idx * 43) % 60
        cont    = Text()
        cont.append("  ▣ ", style="dim")
        cont.append_text(_scroll_containers(hd.containers, cont_width, tick, phase))
        parts.append(cont)

    return Group(*parts)


def _render_hosts(
    hosts: list[HostData],
    tick: int = 0,
    cont_width: int = 60,
) -> Group:
    parts: list[Any] = []
    for i, hd in enumerate(hosts):
        parts.append(_render_host(hd, tick, i, cont_width))
    return Group(*parts)


# ── widget ────────────────────────────────────────────────────────────────────

class HostsWidget(DashWidget):
    """Host monitoring via ping + Glances (CPU, MEM, Docker containers)."""

    data: reactive[list[HostData] | None] = reactive(None, always_update=True)

    DEFAULT_CSS = """
    HostsWidget { height: auto; }
    #hosts-body { height: auto; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._hosts: list[tuple[str, str]] = []
        self._data_timer: Timer | None = None
        self._scroll_timer: Timer | None = None
        self._tick: int = 0
        self._scroll_epoch: int = 0

    def compose(self) -> ComposeResult:
        yield Static("[dim]Loading…[/dim]", id="hosts-body")

    def on_mount(self) -> None:
        raw  = config.get("TUIDASH_HOSTS", "") or ""
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        if not urls:
            self.query_one("#hosts-body", Static).update(
                "[dim]No hosts configured — set TUIDASH_HOSTS[/dim]"
            )
            return
        self._hosts = [(_name_from_url(url), url) for url in urls]
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

    def _cont_width(self) -> int:
        content_w = self.content_size.width or 60
        return max(10, content_w - 4)  # subtract "  ▣ " prefix

    def _redraw(self) -> None:
        if self.data is None:
            return
        self.query_one("#hosts-body", Static).update(
            _render_hosts(self.data, self._tick, self._cont_width())
        )

    @work(thread=True)
    def _load(self) -> None:
        if not self._hosts:
            return
        with ThreadPoolExecutor(max_workers=len(self._hosts)) as pool:
            results = list(pool.map(lambda h: _monitor_host(h[0], h[1]), self._hosts))
        self.app.call_from_thread(self._show_data, results)

    def _show_data(self, data: list[HostData]) -> None:
        self.data = data

    def watch_data(self, data: list[HostData] | None) -> None:
        if data is None:
            return
        self._redraw()


def _name_from_url(url: str) -> str:
    hostname = urlparse(url).hostname or url
    # Keep full hostname for bare IPs; take first label for proper hostnames
    parts = hostname.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return hostname
    return parts[0]
