from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests
from rich.console import Group
from rich.measure import Measurement
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import ScrollableContainer
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static
from textual import work

from ..theme import ACCENT, BAR_HIGH, BAR_LOW, BAR_MID, PERF_BAD, PERF_GREAT, PERF_TERRIBLE
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
    return "●︎", ACCENT


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
            cpu_count = load_j.get("cpucore") or load_j.get("cpucount") or load_j.get("cpu_count")

            uptime_r = f_uptime.result()
            uptime   = uptime_r.text.strip().strip('"') if uptime_r.ok else ""

            disks: list[DiskInfo] = []
            fs_r = f_fs.result()
            if fs_r.ok:
                # Group entries by device_name to de-duplicate Docker bind-mounts.
                # When Glances runs in Docker it sees the same physical partition
                # mounted at multiple file paths (/etc/resolv.conf, /etc/hostname,
                # etc.). Collapsing by device keeps one row per real disk.
                device_groups: dict[str, list[dict]] = {}
                for d in fs_r.json():
                    if not isinstance(d, dict):
                        continue
                    size = d.get("size", 0)
                    mnt  = d.get("mnt_point") or d.get("mount_point") or ""
                    if size < 100 * 1024 * 1024:
                        continue
                    if any(mnt.startswith(p) for p in ("/proc", "/sys", "/dev/pts", "/run/user")):
                        continue
                    dev = d.get("device_name") or mnt
                    device_groups.setdefault(dev, []).append(d)

                for dev, entries in device_groups.items():
                    d    = entries[0]
                    mnt  = d.get("mnt_point") or d.get("mount_point") or ""
                    # Multiple entries sharing a device → Docker file bind-mounts;
                    # use stripped device name (e.g. "sda1") as the label.
                    if len(entries) > 1:
                        label = dev.removeprefix("/dev/")
                    else:
                        label = mnt
                    disks.append(DiskInfo(
                        mountpoint=label,
                        used_pct=d.get("percent", 0.0),
                        used_bytes=d.get("used", 0),
                        total_bytes=d.get("size", 0),
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

class _FluidNeonBar:
    """neon_bar that fills its Table.grid column at Rich render time."""
    def __init__(self, pct: float) -> None:
        self._pct = pct
    def __rich_console__(self, console, options):
        yield neon_bar(self._pct, options.max_width)
    def __rich_measure__(self, console, options):
        return Measurement(1, options.max_width)


class _HomelabBar:
    """Thin ━/─ bar — same colour gradient as neon_bar but with natural row spacing."""
    def __init__(self, pct: float) -> None:
        self._pct = pct
    def __rich_console__(self, console, options):
        w      = options.max_width
        filled = int(w * max(0.0, min(100.0, self._pct)) / 100)
        color  = BAR_HIGH if self._pct >= 80 else (BAR_MID if self._pct >= 60 else BAR_LOW)
        t = Text()
        t.append("━" * filled,       style=color)
        t.append("─" * (w - filled), style="dim")
        yield t
    def __rich_measure__(self, console, options):
        return Measurement(1, options.max_width)


def _build_container_col(containers: list[ContainerDetail], name_w: int) -> Table:
    tbl = Table.grid(padding=(0, 1, 0, 0))
    tbl.pad_edge = False
    tbl.add_column(width=2,      no_wrap=True)
    tbl.add_column(width=name_w, no_wrap=True)
    tbl.add_column(width=7,      no_wrap=True, justify="right")
    tbl.add_column(no_wrap=True)
    tbl.add_row(
        Text(""),
        Text("CONTAINER", style="bold dim"),
        Text("CPU",       style="bold dim", justify="right"),
        Text("MEM",       style="bold dim"),
    )
    for c in containers:
        badge, badge_style = _container_badge(c)
        running = "running" in c.status.lower()
        cpu_str = f"{c.cpu_pct:.1f}%" if c.cpu_pct is not None else "—"
        tbl.add_row(
            Text(badge,           style=badge_style),
            Text(c.name[:name_w], style="" if running else "dim"),
            Text(cpu_str,         style="dim", justify="right"),
            Text(_fmt_mem_compact(c.mem_used), style="dim"),
        )
    return tbl


def _render_host_body(hd: HostDetail, width: int = 0) -> Group:
    """Body content for one host widget; name lives in the border title."""
    parts: list[Any] = []

    # ── stats grid: CPU, MEM, disks — one shared ratio=1 bar column ─────────
    has_stats = hd.cpu_pct is not None or hd.mem_pct is not None or hd.disks
    if has_stats or hd.glances_err:
        if hd.glances_err and not has_stats:
            parts.append(Text(f"glances: {hd.glances_err}", style=f"dim {PERF_TERRIBLE}"))
        else:
            lbl_w = max(
                3,  # len("CPU") / len("MEM")
                *(len(d.mountpoint) for d in hd.disks),
            )
            grid = Table.grid(expand=True, padding=(0, 0))
            grid.add_column(width=lbl_w + 2, no_wrap=True)  # label + gap
            grid.add_column(ratio=1)                          # shared fluid bar
            grid.add_column(no_wrap=True)                     # suffix

            if hd.cpu_pct is not None:
                cpu_suffix = Text()
                cpu_suffix.append(f"  {hd.cpu_pct:4.1f}%", style="dim")
                if hd.cpu_count:
                    cpu_suffix.append("  ", style="dim")
                    cpu_suffix.append("▪" * min(hd.cpu_count, 32), style=ACCENT)
                grid.add_row(
                    Text(f"{'CPU':<{lbl_w}}  ", style="dim"),
                    _HomelabBar(hd.cpu_pct),
                    cpu_suffix,
                )
            if hd.mem_pct is not None:
                mem_suffix = f"  {hd.mem_pct:4.1f}%"
                if hd.mem_used and hd.mem_total:
                    mem_suffix += f"  {_fmt_gb(hd.mem_used)}/{_fmt_gb(hd.mem_total)}"
                grid.add_row(
                    Text(f"{'MEM':<{lbl_w}}  ", style="dim"),
                    _HomelabBar(hd.mem_pct),
                    Text(mem_suffix, style="dim"),
                )
            for d in hd.disks:
                grid.add_row(
                    Text(f"{d.mountpoint:<{lbl_w}}  ", style="dim"),
                    _HomelabBar(d.used_pct),
                    Text(
                        f"  {d.used_pct:4.1f}%  {_fmt_gb(d.used_bytes)} / {_fmt_gb(d.total_bytes)}",
                        style="dim",
                    ),
                )
            parts.append(grid)


    # ── containers ────────────────────────────────────────────────────────────
    if hd.containers:
        parts.append(Text(""))
        two_col = width >= 62 and len(hd.containers) >= 2
        if two_col:
            outer = Table.grid(expand=True, padding=(0, 2, 0, 0))
            outer.pad_edge = False
            outer.add_column(ratio=1)
            outer.add_column(ratio=1)
            outer.add_row(
                _build_container_col(hd.containers[0::2], name_w=12),
                _build_container_col(hd.containers[1::2], name_w=12),
            )
            parts.append(outer)
        else:
            parts.append(_build_container_col(hd.containers, name_w=30))

    return Group(*parts)


# ── widget ────────────────────────────────────────────────────────────────────

class HomelabHostWidget(DashWidget):
    """Detailed view for a single homelab host."""

    _mobile_scrollable = True
    data: reactive[HostDetail | None] = reactive(None, always_update=True)

    DEFAULT_CSS = """
    HomelabHostWidget               { height: 1fr; width: 1fr; }
    HomelabHostWidget #host-scroll  { height: 1fr; }
    HomelabHostWidget Static        { height: auto; }
    """

    def __init__(self, url: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._url  = url
        self._name = _name_from_url(url)
        self._data_timer: Timer | None = None
        self._initial_load_done: bool = False

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="host-scroll"):
            yield Static("[dim]loading…[/dim]")

    def on_mount(self) -> None:
        self.border_title    = f"  {self._name}"
        self.border_subtitle = "loading…"

    def on_show(self) -> None:
        if not self._initial_load_done:
            self._initial_load_done = True
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

    def _redraw(self) -> None:
        if self.data is None:
            return
        hd = self.data
        uptime_str = (
            f"  [not bold dim](up {_fmt_uptime(hd.uptime)})[/]" if hd.uptime else ""
        )
        self.border_title    = f"  {self._name}{uptime_str}"
        self.border_subtitle = (
            f"{hd.rtt_ms:.0f} ms" if hd.reachable and hd.rtt_ms is not None
            else ("ok" if hd.reachable else "unreachable")
        )
        self.query_one(Static).update(_render_host_body(hd, width=self.size.width))

    def watch_data(self, hd: HostDetail | None) -> None:
        if hd is None:
            return
        self._redraw()

    def on_resize(self) -> None:
        self.call_after_refresh(self._redraw)
