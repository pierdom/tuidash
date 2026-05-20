from __future__ import annotations

import re
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

from ..theme import ACCENT, PERF_BAD, PERF_GREAT, PERF_TERRIBLE
from .base import DashWidget, neon_bar
from .hosts import _name_from_url, _ping_host

_BAR_W = 20

# ── data model ────────────────────────────────────────────────────────────────

@dataclass
class ContainerDetail:
    name:      str
    status:    str
    health:    str            = ""
    cpu_pct:   float | None   = None
    mem_used:  int | None     = None   # bytes
    mem_limit: int | None     = None   # bytes
    image:     str            = ""
    uptime:    str            = ""


@dataclass
class DiskInfo:
    mountpoint:  str
    used_pct:    float
    used_bytes:  int
    total_bytes: int


@dataclass
class HostDetail:
    name:        str
    url:         str
    reachable:   bool                  = False
    rtt_ms:      float | None          = None
    cpu_pct:     float | None          = None
    mem_pct:     float | None          = None
    mem_used:    int | None            = None
    mem_total:   int | None            = None
    load1:       float | None          = None
    load5:       float | None          = None
    load15:      float | None          = None
    cpu_count:   int | None            = None
    uptime:      str                   = ""
    disks:       list[DiskInfo]        = field(default_factory=list)
    containers:  list[ContainerDetail] = field(default_factory=list)
    glances_err: str                   = ""


# ── helpers ───────────────────────────────────────────────────────────────────

def _fmt_gb(n: int | None) -> str:
    if n is None:
        return "?"
    return f"{n / 1024**3:.1f} GB"


def _fmt_mem_compact(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1024**3:
        return f"{n / 1024**3:.1f}G"
    if n >= 1024**2:
        return f"{n / 1024**2:.0f}M"
    if n >= 1024:
        return f"{n / 1024:.0f}K"
    return f"{n}B"


def _fmt_uptime(s: str) -> str:
    if not s:
        return ""
    if s.isdigit():
        secs = int(s)
        d = secs // 86400
        h = (secs % 86400) // 3600
        m = (secs % 3600) // 60
        return f"{d}d {h}h" if d else (f"{h}h {m}m" if h else f"{m}m")
    m = re.match(r"(\d+) day[s]?,\s*(\d+):", s)
    if m:
        d, h = int(m.group(1)), int(m.group(2))
        return f"{d}d {h}h" if d else f"{h}h"
    m2 = re.match(r"(\d+):(\d+):", s)
    if m2:
        h, mi = int(m2.group(1)), int(m2.group(2))
        return f"{h}h {mi}m" if h else f"{mi}m"
    return s[:8]


def _container_badge(c: ContainerDetail) -> tuple[str, str]:
    running = "running" in c.status.lower()
    if not running:
        return "▪", "dim"
    if c.health == "healthy":
        return "✓", PERF_GREAT
    if c.health == "unhealthy":
        return "✗", PERF_TERRIBLE
    if c.health == "starting":
        return "⚡", PERF_BAD
    return "●", ACCENT


# ── Glances fetch ─────────────────────────────────────────────────────────────

def _parse_container(c: dict[str, Any]) -> ContainerDetail:
    status = c.get("status", "?")
    health = c.get("health", "")
    if not health:
        m = re.search(r"\((\w+)\)", status)
        health = m.group(1) if m else ""

    raw_cpu = c.get("cpu_percent")
    if raw_cpu is None:
        raw_cpu = c.get("cpu")
    cpu: float | None = None
    if isinstance(raw_cpu, (int, float)):
        cpu = float(raw_cpu)
    elif isinstance(raw_cpu, dict):
        v = raw_cpu.get("total") or raw_cpu.get("percent")
        if v is not None:
            cpu = float(v)

    raw_mem = c.get("memory")
    mem_used: int | None  = None
    mem_limit: int | None = None
    if isinstance(raw_mem, dict):
        mem_used  = raw_mem.get("usage")
        mem_limit = raw_mem.get("limit")
    else:
        mem_used  = c.get("memory_usage")
        mem_limit = c.get("memory_limit")
    if mem_used  is not None: mem_used  = int(mem_used)
    if mem_limit is not None:
        mem_limit = int(mem_limit)
        if mem_limit <= 0 or mem_limit > 2**62:
            mem_limit = None

    image = c.get("image", "")
    if isinstance(image, list):
        image = image[0] if image else ""
    if image and "/" in image:
        image = image.rsplit("/", 1)[-1]

    return ContainerDetail(
        name=c.get("name", "?"),
        status=status,
        health=health.lower(),
        cpu_pct=cpu,
        mem_used=mem_used,
        mem_limit=mem_limit,
        image=str(image),
        uptime=str(c.get("uptime", "") or ""),
    )


def _fetch_glances_detail(base_url: str) -> tuple[
    float | None, float | None, int | None, int | None,
    float | None, float | None, float | None, int | None,
    str, list[DiskInfo], list[ContainerDetail],
]:
    base = base_url.rstrip("/")
    for v, cont_path in (("4", "containers"), ("3", "docker")):
        api = f"{base}/api/{v}"
        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                f_cpu    = pool.submit(requests.get, f"{api}/cpu",         timeout=8)
                f_mem    = pool.submit(requests.get, f"{api}/mem",         timeout=8)
                f_load   = pool.submit(requests.get, f"{api}/load",        timeout=8)
                f_uptime = pool.submit(requests.get, f"{api}/uptime",      timeout=8)
                f_fs     = pool.submit(requests.get, f"{api}/fs",          timeout=8)
                f_cont   = pool.submit(requests.get, f"{api}/{cont_path}", timeout=8)

            cpu_r = f_cpu.result()
            if cpu_r.status_code == 404:
                continue
            cpu_r.raise_for_status()

            cpu_j     = cpu_r.json()
            mem_j     = f_mem.result().json()
            cpu_pct   = cpu_j.get("total")
            mem_pct   = mem_j.get("percent")
            mem_used  = mem_j.get("used")
            mem_total = mem_j.get("total")

            load_r = f_load.result()
            load_j = load_r.json() if load_r.ok else {}
            load1     = load_j.get("min1")
            load5     = load_j.get("min5")
            load15    = load_j.get("min15")
            cpu_count = load_j.get("cpucount") or load_j.get("cpu_count")

            uptime_r = f_uptime.result()
            uptime   = uptime_r.text.strip().strip('"') if uptime_r.ok else ""

            disks: list[DiskInfo] = []
            fs_r = f_fs.result()
            if fs_r.ok:
                for d in fs_r.json():
                    if not isinstance(d, dict):
                        continue
                    size = d.get("size", 0)
                    mnt  = d.get("mnt_point") or d.get("mount_point") or ""
                    if size < 100 * 1024 * 1024:
                        continue
                    if any(mnt.startswith(p) for p in ("/proc", "/sys", "/dev/pts", "/run/user")):
                        continue
                    disks.append(DiskInfo(
                        mountpoint=mnt,
                        used_pct=d.get("percent", 0.0),
                        used_bytes=d.get("used", 0),
                        total_bytes=size,
                    ))

            containers: list[ContainerDetail] = []
            cont_r = f_cont.result()
            if cont_r.ok:
                raw = cont_r.json()
                items = raw if isinstance(raw, list) else raw.get("containers", [])
                for c in items:
                    if isinstance(c, dict):
                        containers.append(_parse_container(c))

            return (cpu_pct, mem_pct, mem_used, mem_total,
                    load1, load5, load15, cpu_count,
                    uptime, disks, containers)
        except Exception:
            continue

    raise RuntimeError("Glances API unreachable")


def _monitor_host_detail(name: str, url: str) -> HostDetail:
    hd = HostDetail(name=name, url=url)
    hostname = urlparse(url).hostname or url
    hd.reachable, hd.rtt_ms = _ping_host(hostname)
    try:
        (hd.cpu_pct, hd.mem_pct, hd.mem_used, hd.mem_total,
         hd.load1, hd.load5, hd.load15, hd.cpu_count,
         hd.uptime, hd.disks, hd.containers) = _fetch_glances_detail(url)
    except Exception as exc:
        hd.glances_err = str(exc)
    return hd


# ── rendering ─────────────────────────────────────────────────────────────────

def _render_host_body(hd: HostDetail) -> Group:
    """Body content for one host widget; name lives in the border title."""
    parts: list[Any] = []

    # ── system stats ──────────────────────────────────────────────────────────
    if hd.cpu_pct is not None or hd.mem_pct is not None:
        stats = Text()
        if hd.cpu_pct is not None:
            stats.append("CPU ", style="dim")
            stats.append_text(neon_bar(hd.cpu_pct, _BAR_W))
            stats.append(f" {hd.cpu_pct:4.1f}%", style="dim")
        if hd.mem_pct is not None:
            stats.append("   MEM ", style="dim")
            stats.append_text(neon_bar(hd.mem_pct, _BAR_W))
            stats.append(f" {hd.mem_pct:4.1f}%", style="dim")
            if hd.mem_used and hd.mem_total:
                stats.append(
                    f"  {_fmt_gb(hd.mem_used)} / {_fmt_gb(hd.mem_total)}", style="dim"
                )
        if hd.load1 is not None:
            load_str = f"   load {hd.load1:.2f}  {hd.load5:.2f}  {hd.load15:.2f}"
            if hd.cpu_count:
                load_str += f"  ({hd.cpu_count} cores)"
            stats.append(load_str, style="dim")
        if hd.uptime:
            stats.append(f"   up {_fmt_uptime(hd.uptime)}", style="dim")
        parts.append(stats)
    elif hd.glances_err:
        parts.append(Text(f"glances: {hd.glances_err}", style=f"dim {PERF_TERRIBLE}"))

    # ── containers ────────────────────────────────────────────────────────────
    if hd.containers:
        tbl = Table.grid(padding=(0, 1))
        tbl.add_column(width=2,  no_wrap=True)                    # badge
        tbl.add_column(width=22, no_wrap=True)                    # name
        tbl.add_column(width=6,  no_wrap=True, justify="right")   # cpu%
        tbl.add_column(width=13, no_wrap=True)                    # mem
        tbl.add_column(width=22, no_wrap=True)                    # image
        tbl.add_column(no_wrap=True)                              # uptime

        tbl.add_row(
            Text(""),
            Text("CONTAINER", style="bold dim"),
            Text("CPU",       style="bold dim", justify="right"),
            Text("MEMORY",    style="bold dim"),
            Text("IMAGE",     style="bold dim"),
            Text("UP",        style="bold dim"),
        )

        for c in hd.containers:
            badge, badge_style = _container_badge(c)
            running = "running" in c.status.lower()
            dim_s   = "" if running else "dim"

            cpu_str = f"{c.cpu_pct:.1f}%" if c.cpu_pct is not None else "—"
            mem_str = _fmt_mem_compact(c.mem_used)
            if c.mem_limit:
                mem_str += f" / {_fmt_mem_compact(c.mem_limit)}"

            tbl.add_row(
                Text(badge, style=badge_style),
                Text(c.name[:22],           style=dim_s),
                Text(cpu_str, style="dim",  justify="right"),
                Text(mem_str, style="dim"),
                Text((c.image or "—")[:22], style="dim"),
                Text(_fmt_uptime(c.uptime), style="dim"),
            )

        parts.append(Text(""))
        parts.append(tbl)

    # ── disks ─────────────────────────────────────────────────────────────────
    if hd.disks:
        parts.append(Text(""))
        lbl_w = max(len(d.mountpoint) for d in hd.disks)
        for d in hd.disks:
            row = Text()
            row.append(f"{d.mountpoint:<{lbl_w}}", style="dim")
            row.append("  ")
            row.append_text(neon_bar(d.used_pct, _BAR_W))
            row.append(
                f"  {d.used_pct:4.1f}%   {_fmt_gb(d.used_bytes)} / {_fmt_gb(d.total_bytes)}",
                style="dim",
            )
            parts.append(row)

    return Group(*parts)


# ── widget ────────────────────────────────────────────────────────────────────

class HomelabHostWidget(DashWidget):
    """Detailed view for a single homelab host."""

    data: reactive[HostDetail | None] = reactive(None, always_update=True)

    DEFAULT_CSS = """
    HomelabHostWidget        { height: auto; width: 100%; }
    HomelabHostWidget Static { height: auto; }
    """

    def __init__(self, url: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._url  = url
        self._name = _name_from_url(url)
        self._data_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Static("[dim]loading…[/dim]")

    def on_mount(self) -> None:
        self.border_title    = f"  {self._name}"
        self.border_subtitle = "loading…"
        self._load()

    def set_refresh_interval(self, seconds: int) -> None:
        if self._data_timer is not None:
            self._data_timer.stop()
        self._data_timer = self.set_interval(float(seconds), self._load)

    @work(thread=True)
    def _load(self) -> None:
        hd = _monitor_host_detail(self._name, self._url)
        self.app.call_from_thread(self._show_data, hd)

    def _show_data(self, hd: HostDetail) -> None:
        self.data = hd

    def watch_data(self, hd: HostDetail | None) -> None:
        if hd is None:
            return
        self.border_subtitle = (
            f"{hd.rtt_ms:.0f} ms" if hd.reachable and hd.rtt_ms is not None
            else ("ok" if hd.reachable else "unreachable")
        )
        self.query_one(Static).update(_render_host_body(hd))
