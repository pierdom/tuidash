from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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

from .. import config, influx
from ..theme import ACCENT, BAR_HIGH, BAR_LOW, BAR_MID, PERF_GOOD, PERF_GREAT, PERF_TERRIBLE
from .base import DashWidget, accent_gradient_bar, neon_bar

_BAR_W = 20

# scarif's dockerized CTs, one Portainer environment each (see relay #319)
_SCARIF_ENVIRONMENTS = ["scarif-apps", "scarif-files", "scarif-monitoring", "scarif-sync", "scarif-proxy"]
# present in every scarif environment, adds no signal
_NOISY_CONTAINERS = {"portainer_agent"}
# every host that reports to InfluxDB — used by the fleet connectivity strip
_FLEET_HOSTS = ["scarif", "bespin", "endor", "malachor"]
_DEFAULT_MAX_MBPS = 600.0
_SPEED_BAR_W = 9
_PING_BUDGET_MS = 50.0  # "reasonable ping" ceiling for the budget gauge — not an alert threshold
_PING_BAR_W = 6
_PORTAINER_TIMEOUT = 10.0
_PORTAINER_CONTAINER_TIMEOUT = 20.0

# ── data model ────────────────────────────────────────────────────────────────

@dataclass
class ContainerDetail:
    name:   str
    status: str
    health: str = ""


@dataclass
class DiskInfo:
    mountpoint:  str
    used_pct:    float
    used_bytes:  int
    total_bytes: int


@dataclass
class BackupStatus:
    label:  str
    ok:     bool
    detail: str


@dataclass
class HostDetail:
    name:        str
    central:     bool                  = False
    reachable:   bool                  = False   # reporting to InfluxDB in the last 3 min
    cpu_pct:     float | None          = None
    mem_pct:     float | None          = None
    disks:       list[DiskInfo]        = field(default_factory=list)
    containers:  list[ContainerDetail] = field(default_factory=list)
    backups:     list[BackupStatus]    = field(default_factory=list)
    fetch_err:   str                   = ""


@dataclass
class FleetHost:
    name:      str
    reporting: bool


@dataclass
class FleetStatus:
    hosts:      list[FleetHost] = field(default_factory=list)
    unhealthy:  int             = 0
    stopped:    int             = 0
    running:    int             = 0
    docker_stale: bool          = False   # True if the Portainer collector itself has no recent data
    down_mbps:  float | None    = None
    up_mbps:    float | None    = None
    ping_ms:    float | None    = None
    fetch_err:  str             = ""


# ── helpers ───────────────────────────────────────────────────────────────────

def _fmt_gb(n: int | None) -> str:
    if n is None:
        return "?"
    return f"{n / 1024**3:.1f} GB"


_STOPPED_STATES = {"exited", "dead", "created", "paused", "removing"}


def _is_running(c: ContainerDetail) -> bool:
    return c.status.lower().split()[0] not in _STOPPED_STATES


def _container_badge(c: ContainerDetail) -> tuple[str, str]:
    if not _is_running(c):
        return "▪", "dim"
    if c.health == "unhealthy":
        return "●", PERF_TERRIBLE
    return "●", ACCENT


# ── InfluxDB fetch ────────────────────────────────────────────────────────────

def _fetch_host_stats(host: str) -> tuple[bool, float | None, float | None, DiskInfo | None]:
    reporting = influx.last(
        f'SELECT count("usage_idle") AS "n" FROM "cpu" '
        f"WHERE \"host\" = '{host}' AND \"cpu\" = 'cpu-total' AND time > now() - 3m"
    ) is not None

    cpu_pct: float | None = None
    row = influx.last(
        f'SELECT last("usage_idle") AS "idle" FROM "cpu" '
        f"WHERE \"host\" = '{host}' AND \"cpu\" = 'cpu-total' AND time > now() - 10m"
    )
    if row is not None:
        cpu_pct = 100 - row["idle"]

    mem_pct: float | None = None
    row = influx.last(
        f'SELECT last("used_percent") AS "pct" FROM "mem" '
        f"WHERE \"host\" = '{host}' AND time > now() - 10m"
    )
    if row is not None:
        mem_pct = row["pct"]

    disk: DiskInfo | None = None
    row = influx.last(
        f'SELECT last("used_percent") AS "pct", last("used") AS "used", last("total") AS "total" '
        f"FROM \"disk\" WHERE \"host\" = '{host}' AND \"path\" = '/' AND time > now() - 10m"
    )
    if row is not None and row.get("total"):
        disk = DiskInfo(mountpoint="/", used_pct=row["pct"], used_bytes=row["used"], total_bytes=row["total"])

    return reporting, cpu_pct, mem_pct, disk


def _fetch_pool(name: str) -> DiskInfo | None:
    """scarif's ZFS pools, via Proxmox's own native metric push (cross-db query into the proxmox bucket)."""
    row = influx.last(
        f'SELECT last("used") AS "used", last("total") AS "total" '
        f'FROM "proxmox"."autogen"."system" WHERE "object" = \'storages\' '
        f"AND \"host\" = '{name}' AND time > now() - 1h"
    )
    if not row or not row.get("total"):
        return None
    used, total = row["used"], row["total"]
    return DiskInfo(mountpoint=name, used_pct=used / total * 100, used_bytes=used, total_bytes=total)


def _portainer_headers() -> dict[str, str]:
    return {"X-API-Key": config.require("TUIDASH_PORTAINER_TOKEN")}


def _fetch_portainer_endpoints() -> list[dict]:
    host = config.require("TUIDASH_PORTAINER_HOST").rstrip("/")
    r = requests.get(f"{host}/api/endpoints", headers=_portainer_headers(), timeout=_PORTAINER_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _container_health(status: str) -> str:
    if "(unhealthy)" in status:
        return "unhealthy"
    if "(healthy)" in status:
        return "healthy"
    if "health: starting" in status:
        return "starting"
    return ""


def _fetch_portainer_containers(host: str, endpoint_id: int) -> list[ContainerDetail]:
    r = requests.get(
        f"{host}/api/endpoints/{endpoint_id}/docker/containers/json",
        params={"all": "true"},
        headers=_portainer_headers(),
        timeout=_PORTAINER_CONTAINER_TIMEOUT,
    )
    r.raise_for_status()
    out: list[ContainerDetail] = []
    for c in r.json():
        name = (c.get("Names") or ["?"])[0].lstrip("/")
        if name in _NOISY_CONTAINERS:
            continue
        out.append(ContainerDetail(name=name, status=c.get("State", "?"), health=_container_health(c.get("Status", ""))))
    return out


def _fetch_containers(environments: list[str], prefix_env: bool) -> list[ContainerDetail]:
    """Live per-container status straight from the Portainer API — one call per environment."""
    host = config.require("TUIDASH_PORTAINER_HOST").rstrip("/")
    by_name = {e["Name"]: e["Id"] for e in _fetch_portainer_endpoints()}
    out: list[ContainerDetail] = []
    for env in environments:
        endpoint_id = by_name.get(env)
        if endpoint_id is None:
            continue
        for c in _fetch_portainer_containers(host, endpoint_id):
            name = f"{env.removeprefix('scarif-')}/{c.name}" if prefix_env else c.name
            out.append(ContainerDetail(name=name, status=c.status, health=c.health))
    out.sort(key=lambda c: c.name)
    return out


def _fetch_backups() -> list[BackupStatus]:
    """scarif-only: freshness of sanoid snapshots, borg offsite, and the daily vzdump job.

    Uses a single canary dataset ("appdata") per backup leg rather than every dataset —
    good enough to catch "the whole pipeline stopped running", not a full per-dataset view
    (that lives on the Scarif v3 Grafana dashboard).
    """
    backups: list[BackupStatus] = []

    row = influx.last(
        'SELECT last("latest_age_s") AS "age" FROM "zfs_snapshot" '
        "WHERE \"host\" = 'scarif' AND \"dataset\" = 'tank/appdata' AND time > now() - 2h"
    )
    if row is not None:
        age_h = row["age"] / 3600
        backups.append(BackupStatus("zfs snapshot", age_h < 3, f"{age_h:.1f}h ago"))

    row = influx.last(
        'SELECT last("latest_age_s") AS "age" FROM "borg_archive" '
        "WHERE \"host\" = 'scarif' AND \"dataset\" = 'appdata' AND time > now() - 3h"
    )
    if row is not None:
        age_h = row["age"] / 3600
        backups.append(BackupStatus("borg offsite", age_h < 30, f"{age_h:.1f}h ago"))

    row = influx.last(
        'SELECT last("ok") AS "ok", last("status") AS "status", last("age_s") AS "age" '
        "FROM \"pve_vzdump\" WHERE \"host\" = 'scarif' AND time > now() - 2h"
    )
    if row is not None:
        age_h = (row.get("age") or 0) / 3600
        backups.append(BackupStatus("vzdump", bool(row.get("ok")), f"{row.get('status', '?')} · {age_h:.1f}h ago"))

    return backups


def _monitor_host_influx(host: str, central: bool) -> HostDetail:
    hd = HostDetail(name=host, central=central)
    try:
        hd.reachable, hd.cpu_pct, hd.mem_pct, disk = _fetch_host_stats(host)
        if disk is not None and not central:
            hd.disks.append(disk)
        if central:
            for pool in ("tank", "scratch"):
                p = _fetch_pool(pool)
                if p is not None:
                    hd.disks.append(p)
            hd.containers = _fetch_containers(_SCARIF_ENVIRONMENTS, prefix_env=True)
            hd.backups = _fetch_backups()
        else:
            hd.containers = _fetch_containers([host], prefix_env=False)
    except Exception as exc:
        hd.fetch_err = str(exc)
    return hd


def _fetch_connectivity() -> list[FleetHost]:
    out: list[FleetHost] = []
    for host in _FLEET_HOSTS:
        reporting = influx.last(
            f'SELECT count("usage_idle") AS "n" FROM "cpu" '
            f"WHERE \"host\" = '{host}' AND \"cpu\" = 'cpu-total' AND time > now() - 3m"
        ) is not None
        out.append(FleetHost(name=host, reporting=reporting))
    return out


def _fetch_fleet_docker() -> tuple[int, int, int, bool]:
    """(unhealthy, stopped, running, has_data), summed from each endpoint's own Portainer snapshot.

    `has_data` distinguishes "Portainer confirmed zero problems" from "couldn't reach
    Portainer at all" — those must not look the same (see relay #319's `noValue` note).
    """
    endpoints = _fetch_portainer_endpoints()
    if not endpoints:
        return 0, 0, 0, False
    unhealthy = stopped = running = 0
    for e in endpoints:
        snap = (e.get("Snapshots") or [{}])[0]
        running   += snap.get("RunningContainerCount", 0)
        stopped   += snap.get("StoppedContainerCount", 0)
        unhealthy += snap.get("UnhealthyContainerCount", 0)
    return unhealthy, stopped, running, True


def _fetch_fleet_status() -> FleetStatus:
    fs = FleetStatus()
    try:
        fs.hosts = _fetch_connectivity()
        fs.unhealthy, fs.stopped, fs.running, has_data = _fetch_fleet_docker()
        fs.docker_stale = not has_data
        row = influx.last(
            'SELECT last("download_bits") AS "down", last("upload_bits") AS "up", last("ping") AS "ping" '
            'FROM "speedtest"'
        )
        if row is not None:
            fs.down_mbps = row["down"] / 1_000_000
            fs.up_mbps   = row["up"] / 1_000_000
            fs.ping_ms   = row["ping"]
    except Exception as exc:
        fs.fetch_err = str(exc)
    return fs


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


_COL_W = 22  # badge + name + gap; Portainer gives no per-container cpu/mem, so no bar columns needed
_MAX_COLS = 6


def _build_container_col(containers: list[ContainerDetail], name_w: int) -> Table:
    tbl = Table.grid(padding=(0, 1, 0, 0))
    tbl.pad_edge = False
    tbl.add_column(width=2,      no_wrap=True)
    tbl.add_column(width=name_w, no_wrap=True)
    for c in containers:
        badge, badge_style = _container_badge(c)
        running = _is_running(c)
        max_name = name_w - 2 if c.health == "healthy" else name_w
        name_text = Text()
        name_text.append(c.name[:max_name], style="" if running else "dim")
        if c.health == "healthy":
            name_text.append(" ✓", style=ACCENT)
        tbl.add_row(
            Text(badge, style=badge_style),
            name_text,
        )
    return tbl


def _render_containers_grid(containers: list[ContainerDetail], width: int) -> Table:
    """Split containers into as many columns as fit the available width, filled left-to-right."""
    n_cols = max(1, min(_MAX_COLS, len(containers), width // _COL_W))
    name_w = max(12, _COL_W - 4)
    if n_cols == 1:
        return _build_container_col(containers, name_w=30)
    outer = Table.grid(expand=True, padding=(0, 2, 0, 0))
    outer.pad_edge = False
    for _ in range(n_cols):
        outer.add_column(ratio=1)
    cols = [containers[i::n_cols] for i in range(n_cols)]
    outer.add_row(*(_build_container_col(c, name_w=name_w) for c in cols))
    return outer


def _build_backups_block(backups: list[BackupStatus]) -> Table:
    tbl = Table.grid(padding=(0, 1, 0, 0))
    tbl.pad_edge = False
    tbl.add_column(width=2,  no_wrap=True)
    tbl.add_column(width=14, no_wrap=True)
    tbl.add_column(no_wrap=True)
    for b in backups:
        dot_style = ACCENT if b.ok else PERF_TERRIBLE
        tbl.add_row(
            Text("●", style=dot_style),
            Text(b.label, style="" if b.ok else "dim"),
            Text(b.detail, style="dim"),
        )
    return tbl


def _render_host_body(hd: HostDetail, width: int = 0) -> Group:
    """Body content for one host widget; name lives in the border title."""
    parts: list[Any] = []

    # ── stats grid: CPU, MEM, disks/pools — one shared ratio=1 bar column ──────
    has_stats = hd.cpu_pct is not None or hd.mem_pct is not None or hd.disks
    if has_stats or hd.fetch_err:
        if hd.fetch_err and not has_stats:
            parts.append(Text(f"error: {hd.fetch_err}", style=f"dim {PERF_TERRIBLE}"))
        else:
            lbl_w = max(
                3,  # len("CPU") / len("MEM")
                *(len(d.mountpoint) for d in hd.disks),
            ) if hd.disks else 3
            grid = Table.grid(expand=True, padding=(0, 0))
            grid.add_column(width=lbl_w + 2, no_wrap=True)  # label + gap
            grid.add_column(ratio=1)                          # shared fluid bar
            grid.add_column(no_wrap=True)                     # suffix

            if hd.cpu_pct is not None:
                grid.add_row(
                    Text(f"{'CPU':<{lbl_w}}  ", style="dim"),
                    _HomelabBar(hd.cpu_pct),
                    Text(f"  {hd.cpu_pct:4.1f}%", style="dim"),
                )
            if hd.mem_pct is not None:
                grid.add_row(
                    Text(f"{'MEM':<{lbl_w}}  ", style="dim"),
                    _HomelabBar(hd.mem_pct),
                    Text(f"  {hd.mem_pct:4.1f}%", style="dim"),
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

    # ── backups (central host only) ─────────────────────────────────────────
    if hd.backups:
        parts.append(Text(""))
        parts.append(_build_backups_block(hd.backups))

    # ── containers ────────────────────────────────────────────────────────────
    if hd.containers:
        parts.append(Text(""))
        parts.append(_render_containers_grid(hd.containers, width))

    return Group(*parts)


def _render_hosts_col(hosts: list[FleetHost]) -> Group:
    header = Text("Connectivity", style="bold dim")

    def _cell(h: FleetHost) -> Text:
        t = Text()
        t.append("●︎" if h.reporting else "○︎", style=f"bold {ACCENT}" if h.reporting else f"dim {PERF_TERRIBLE}")
        t.append(f" {h.name[:8]}", style="" if h.reporting else "dim")
        return t

    grid = Table.grid(padding=(0, 1, 0, 0))
    grid.add_column(width=11, no_wrap=True)
    grid.add_column(width=11, no_wrap=True)
    for i in range(0, len(hosts), 2):
        pair = [_cell(h) for h in hosts[i:i + 2]]
        while len(pair) < 2:
            pair.append(Text(""))
        grid.add_row(*pair)
    return Group(header, grid)


def _render_docker_col(fs: FleetStatus) -> Text:
    t = Text()
    t.append("Docker", style="bold dim")
    if fs.docker_stale:
        t.append("  no data", style=f"dim {PERF_TERRIBLE}")
        return t
    t.append(f"  {fs.running} running", style="dim")
    t.append("\n")
    u_style = f"bold {PERF_TERRIBLE}" if fs.unhealthy else f"bold {ACCENT}"
    t.append("●︎", style=u_style)
    t.append(f" {fs.unhealthy} unhealthy", style="" if fs.unhealthy else "dim")
    t.append("   ")
    t.append("▪", style="dim")
    t.append(f" {fs.stopped} stopped", style="dim")
    return t


def _ping_budget_bar(ping_ms: float) -> Text:
    """Small btop/netwatch-style gauge: how much of a "reasonable ping" budget is used."""
    pct    = min(ping_ms / _PING_BUDGET_MS, 1.0)
    filled = max(0, round(pct * _PING_BAR_W))
    color  = PERF_TERRIBLE if pct >= 0.8 else (PERF_GOOD if pct >= 0.4 else PERF_GREAT)
    t = Text()
    t.append("▪" * filled, style=color)
    t.append("▪" * (_PING_BAR_W - filled), style="dim")
    return t


def _render_speed_col(fs: FleetStatus, max_down: float, max_up: float) -> Any:
    header = Text()
    header.append("Speed", style="bold dim")
    if fs.down_mbps is None:
        header.append("  unavailable", style=f"dim {PERF_TERRIBLE}")
        return header
    if fs.ping_ms is not None:
        header.append(f"  ping {fs.ping_ms:.0f}ms ", style="dim")
        header.append_text(_ping_budget_bar(fs.ping_ms))

    def _line(arrow: str, actual: float, max_val: float) -> Text:
        pct    = min(actual / max_val, 1.0) if max_val > 0 else 0.0
        filled = max(0, round(pct * _SPEED_BAR_W))
        color  = PERF_GREAT if pct >= 0.8 else (PERF_GOOD if pct >= 0.5 else PERF_TERRIBLE)
        t = Text()
        t.append(f"{arrow} {actual:4.0f} Mbps ", style=color)
        t.append_text(accent_gradient_bar(filled, _SPEED_BAR_W))
        return t

    return Group(header, _line("↓", fs.down_mbps, max_down), _line("↑", fs.up_mbps, max_up))


def _render_fleet_status(fs: FleetStatus, max_down: float, max_up: float) -> Any:
    if fs.fetch_err:
        return Text(f"error: {fs.fetch_err}", style=f"dim {PERF_TERRIBLE}")
    grid = Table.grid(expand=True, padding=(0, 3, 0, 0))
    grid.add_column(ratio=2)
    grid.add_column(ratio=2)
    grid.add_column(ratio=3)
    grid.add_row(
        _render_hosts_col(fs.hosts),
        _render_docker_col(fs),
        _render_speed_col(fs, max_down, max_up),
    )
    return grid


# ── widget ────────────────────────────────────────────────────────────────────

class HomelabHostWidget(DashWidget):
    """Detailed view for a single homelab host, sourced from InfluxDB (bucket `homelab`).

    `central=True` (scarif) additionally shows the tank/scratch ZFS pools and the
    backup-pipeline freshness (sanoid, borg, vzdump), and aggregates containers
    across every scarif-* Portainer environment instead of a single one.
    """

    _mobile_scrollable = True
    data: reactive[HostDetail | None] = reactive(None, always_update=True)

    DEFAULT_CSS = """
    HomelabHostWidget               { height: 1fr; width: 1fr; }
    HomelabHostWidget #host-scroll  { height: 1fr; }
    HomelabHostWidget Static        { height: auto; }
    """

    def __init__(self, host: str, central: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._host = host
        self._central = central
        self._data_timer: Timer | None = None
        self._initial_load_done: bool = False

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="host-scroll"):
            yield Static("[dim]loading…[/dim]")

    def on_mount(self) -> None:
        self.border_title    = f"  {self._host}"
        self.border_subtitle = "loading…"

    def _sync_scroll_mode(self) -> None:
        """Central host (scarif) sizes to its own content on desktop instead of a
        fixed share — bespin/endor's `1fr` then naturally absorbs whatever scarif
        doesn't need, capped so a big container grid can't swallow the whole page."""
        if self._central and not self.screen.has_class("mobile"):
            try:
                sc = self.query_one(ScrollableContainer)
            except Exception:
                return
            sc.styles.height     = "auto"
            sc.styles.max_height = 24
            sc.styles.overflow_y = "auto"
            return
        super()._sync_scroll_mode()

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
        hd = _monitor_host_influx(self._host, self._central)
        self.app.call_from_thread(self._show_data, hd)

    def _show_data(self, hd: HostDetail) -> None:
        self.data = hd

    def _redraw(self) -> None:
        if self.data is None:
            return
        hd = self.data
        self.border_title    = f"  {self._host}"
        self.border_subtitle = "ok" if hd.reachable else "not reporting"
        self.query_one(Static).update(_render_host_body(hd, width=self.size.width))

    def watch_data(self, hd: HostDetail | None) -> None:
        if hd is None:
            return
        self._redraw()

    def on_resize(self) -> None:
        self.call_after_refresh(self._redraw)


class FleetStatusWidget(DashWidget):
    """Compact fleet-wide strip: host connectivity, aggregate Docker health, speedtest throughput.

    A simplified TUI condensation of the Grafana Homelab dashboard's Connectivity
    and Docker services rows — all three figures read straight off the same
    InfluxDB measurements the host cards use (`cpu`, `portainer_container`, `speedtest`).
    """

    data: reactive[FleetStatus | None] = reactive(None, always_update=True)

    DEFAULT_CSS = """
    FleetStatusWidget        { height: auto; }
    FleetStatusWidget Static { height: auto; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._data_timer: Timer | None = None
        self._initial_load_done: bool = False
        self._max_down: float = _DEFAULT_MAX_MBPS
        self._max_up: float = _DEFAULT_MAX_MBPS

    def compose(self) -> ComposeResult:
        yield Static("[dim]loading…[/dim]")

    def on_mount(self) -> None:
        self.border_title = "  Fleet"
        try:
            self._max_down = float(config.get("TUIDASH_NETSPEED_DOWN") or _DEFAULT_MAX_MBPS)
        except ValueError:
            pass
        try:
            self._max_up = float(config.get("TUIDASH_NETSPEED_UP") or _DEFAULT_MAX_MBPS)
        except ValueError:
            pass

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
        fs = _fetch_fleet_status()
        self.app.call_from_thread(self._show_data, fs)

    def _show_data(self, fs: FleetStatus) -> None:
        self.data = fs

    def watch_data(self, fs: FleetStatus | None) -> None:
        if fs is None:
            return
        if fs.fetch_err:
            self.border_subtitle = "error"
        else:
            up = sum(1 for h in fs.hosts if h.reporting)
            docker = "docker: no data" if fs.docker_stale else f"{fs.unhealthy} unhealthy · {fs.stopped} stopped"
            self.border_subtitle = f"{up}/{len(fs.hosts)} up · {docker}"
        self.query_one(Static).update(_render_fleet_status(fs, self._max_down, self._max_up))
