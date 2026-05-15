from __future__ import annotations

import platform
import re
import socket
import struct
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import requests as _requests
from rich.console import Group
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


_DEFAULT_IPS      = ["1.1.1.1", "8.8.8.8", "192.168.1.1"]
_DEFAULT_HOSTS    = ["google.com", "amazon.com", "facebook.com"]
_DEFAULT_MAX_MBPS = 600.0
_OK_THRESHOLD     = 0.5
_SPEED_BAR_W      = 9


# ── data model ────────────────────────────────────────────────────────────────

@dataclass
class PingResult:
    ip: str
    reachable: bool
    rtt_ms: float | None = None


@dataclass
class DnsResult:
    host: str
    resolved: bool
    ip: str | None = None
    rtt_ms: float | None = None


@dataclass
class SpeedResult:
    download_mbps: float
    upload_mbps: float
    ping_ms: float
    server: str = ""


@dataclass
class ConnectivityData:
    reachability:  list[PingResult] = field(default_factory=list)
    dns:           list[DnsResult]  = field(default_factory=list)
    speed:         SpeedResult | None = None
    speed_enabled: bool             = False
    resolver_ip:   str              = ""
    max_down_mbps: float            = _DEFAULT_MAX_MBPS
    max_up_mbps:   float            = _DEFAULT_MAX_MBPS

    @property
    def reachability_ok(self) -> bool:
        if not self.reachability:
            return False
        return sum(r.reachable for r in self.reachability) / len(self.reachability) >= _OK_THRESHOLD

    @property
    def dns_ok(self) -> bool:
        if not self.dns:
            return False
        return sum(r.resolved for r in self.dns) / len(self.dns) >= _OK_THRESHOLD

    @property
    def overall_ok(self) -> bool:
        return self.reachability_ok and self.dns_ok


# ── probes ────────────────────────────────────────────────────────────────────

def _ping(ip: str) -> PingResult:
    cmd = (
        ["ping", "-c", "1", "-W", "2000", ip]
        if platform.system() == "Darwin"
        else ["ping", "-c", "1", "-W", "2", ip]
    )
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            m = re.search(r"time=(\d+\.?\d*)\s*ms", result.stdout)
            return PingResult(ip=ip, reachable=True, rtt_ms=float(m.group(1)) if m else None)
        return PingResult(ip=ip, reachable=False)
    except Exception:
        return PingResult(ip=ip, reachable=False)


def _dns_query(host: str, resolver: str, timeout: float = 3.0) -> str | None:
    """Send a raw UDP DNS A query to resolver; return first A record IP or None."""
    labels  = host.rstrip(".").split(".")
    qname   = b"".join(bytes([len(l)]) + l.encode() for l in labels) + b"\x00"
    query   = struct.pack(">HHHHHH", 0x1A2B, 0x0100, 1, 0, 0, 0) + qname + struct.pack(">HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(query, (resolver, 53))
        resp, _ = s.recvfrom(512)
    ancount = struct.unpack(">H", resp[6:8])[0]
    if not ancount:
        return None
    # Skip header (12) + question name + QTYPE/QCLASS (4)
    pos = 12
    while resp[pos]:
        pos += (resp[pos] & 0x3F) + 1
    pos += 5  # null byte + QTYPE + QCLASS
    for _ in range(ancount):
        if resp[pos] & 0xC0 == 0xC0:
            pos += 2
        else:
            while resp[pos]:
                pos += resp[pos] + 1
            pos += 1
        rtype, _, _, rdlen = struct.unpack(">HHIH", resp[pos:pos + 10])
        pos += 10
        if rtype == 1 and rdlen == 4:
            return ".".join(str(b) for b in resp[pos:pos + 4])
        pos += rdlen
    return None


def _resolve(host: str, resolver: str | None = None) -> DnsResult:
    try:
        t0 = time.monotonic()
        if resolver:
            ip = _dns_query(host, resolver)
            if ip is None:
                return DnsResult(host=host, resolved=False)
        else:
            ip = socket.gethostbyname(host)
        return DnsResult(host=host, resolved=True, ip=ip, rtt_ms=(time.monotonic() - t0) * 1000)
    except Exception:
        return DnsResult(host=host, resolved=False)


def _get_resolver_ip() -> str:
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                parts = line.split()
                if parts and parts[0] == "nameserver":
                    return parts[1]
    except Exception:
        pass
    return "?"


def _check_reachability(ips: list[str]) -> list[PingResult]:
    with ThreadPoolExecutor(max_workers=max(1, len(ips))) as pool:
        return list(pool.map(_ping, ips))


def _check_dns(hosts: list[str], resolver: str | None = None) -> list[DnsResult]:
    with ThreadPoolExecutor(max_workers=max(1, len(hosts))) as pool:
        return list(pool.map(lambda h: _resolve(h, resolver), hosts))


def _fetch_speed(url: str, token: str | None) -> SpeedResult | None:
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = _requests.get(
            f"{url.rstrip('/')}/api/v1/results/latest",
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        j    = resp.json().get("data", resp.json())
        srv  = (j.get("data") or {}).get("server", {})
        server_str = " ".join(
            filter(None, [srv.get("name"), srv.get("location")])
        ).strip()
        return SpeedResult(
            download_mbps=j.get("download_bits", 0) / 1_000_000,
            upload_mbps=j.get("upload_bits", 0) / 1_000_000,
            ping_ms=float(j.get("ping", 0.0)),
            server=server_str,
        )
    except Exception:
        return None


# ── rendering ─────────────────────────────────────────────────────────────────

def _summary_col(
    title: str,
    passed: list[bool],
    ok: bool,
    rtts: list[float | None],
    subtitle: str = "",
) -> Text:
    n_ok    = sum(passed)
    n_total = len(passed)
    color   = "green" if ok else "red"
    valid   = [r for r in rtts if r is not None]
    avg_rtt = sum(valid) / len(valid) if valid else None

    t = Text()
    t.append(title, style="bold dim")
    if subtitle:
        t.append(f"  {subtitle}", style="dim")
    t.append("\n")
    for p in passed:
        t.append("●", style="green" if p else "red")
        t.append(" ")
    t.append(" ")
    t.append("OK" if ok else "FAIL", style=f"bold {color}")
    t.append(f"  {n_ok}/{n_total}", style=f"dim {color}")
    if avg_rtt is not None:
        t.append(f"  {avg_rtt:.0f}ms", style="dim")
    return t


def _speed_bar(actual: float, max_val: float) -> Text:
    pct    = min(actual / max_val, 1.0) if max_val > 0 else 0.0
    filled = max(0, round(pct * _SPEED_BAR_W))
    color  = "green" if pct >= 0.8 else ("yellow" if pct >= 0.5 else "red")
    bar = Text()
    bar.append("█" * filled,                style=color)
    bar.append("░" * (_SPEED_BAR_W - filled), style="dim")
    return bar


def _render_speed(d: ConnectivityData) -> Group:
    header = Text()
    header.append("Speed", style="bold dim")

    if d.speed is None:
        header.append("  unavailable", style="dim red")
        return Group(header)

    sp = d.speed
    header.append(f"  ping {sp.ping_ms:.0f}ms", style="dim")
    if sp.server:
        header.append(f"  {sp.server}", style="dim")

    def _cell(arrow: str, actual: float, max_val: float) -> Text:
        pct   = min(actual / max_val, 1.0) if max_val > 0 else 0.0
        color = "green" if pct >= 0.8 else ("yellow" if pct >= 0.5 else "red")
        t = Text()
        t.append(arrow,                style=f"bold {color}")
        t.append(f" {actual:.0f} Mbps ", style=f"bold {color}")
        t.append_text(_speed_bar(actual, max_val))
        t.append(f" {pct * 100:.0f}%", style=f"dim {color}")
        return t

    row = Table.grid(expand=True, padding=(0, 1))
    row.add_column(ratio=1)
    row.add_column(ratio=1)
    row.add_row(
        _cell("↓", sp.download_mbps, d.max_down_mbps),
        _cell("↑", sp.upload_mbps,   d.max_up_mbps),
    )

    return Group(header, row)


def _render_connectivity(d: ConnectivityData) -> Group:
    reach_col = _summary_col(
        "Reachability",
        [r.reachable for r in d.reachability],
        d.reachability_ok,
        rtts=[r.rtt_ms for r in d.reachability],
    )
    dns_col = _summary_col(
        "DNS",
        [r.resolved for r in d.dns],
        d.dns_ok,
        rtts=[r.rtt_ms for r in d.dns],
        subtitle=d.resolver_ip,
    )

    top = Table.grid(expand=True, padding=(0, 2))
    top.add_column(ratio=1)
    top.add_column(ratio=1)
    top.add_row(reach_col, dns_col)

    parts: list[Any] = [top]
    if d.speed_enabled:
        parts += [Rule(style="dim"), _render_speed(d)]

    return Group(*parts)


# ── widget ────────────────────────────────────────────────────────────────────

class ConnectivityWidget(DashWidget):
    """Network connectivity checks."""

    data: reactive[ConnectivityData | None] = reactive(None, always_update=True)

    DEFAULT_CSS = """
    ConnectivityWidget { height: auto; }
    #conn-body { height: auto; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._ips: list[str]              = list(_DEFAULT_IPS)
        self._hosts: list[str]            = list(_DEFAULT_HOSTS)
        self._dns_resolver: str | None    = None
        self._max_down                    = _DEFAULT_MAX_MBPS
        self._max_up                      = _DEFAULT_MAX_MBPS
        self._speedtracker_url: str | None   = None
        self._speedtracker_token: str | None = None
        self._err: str | None             = None
        self._data_timer: Timer | None    = None

    def compose(self) -> ComposeResult:
        yield Static("[dim]Loading…[/dim]", id="conn-body")

    def on_mount(self) -> None:
        raw_ips = config.get("TUIDASH_REACHABILITY_IPS", "")
        if raw_ips:
            self._ips = [s.strip() for s in raw_ips.split(",") if s.strip()]

        raw_hosts = config.get("TUIDASH_RESOLVE_HOSTS", "")
        if raw_hosts:
            self._hosts = [s.strip() for s in raw_hosts.split(",") if s.strip()]

        try:
            self._max_down = float(config.get("TUIDASH_NETSPEED_DOWN") or _DEFAULT_MAX_MBPS)
        except ValueError:
            pass
        try:
            self._max_up = float(config.get("TUIDASH_NETSPEED_UP") or _DEFAULT_MAX_MBPS)
        except ValueError:
            pass

        self._dns_resolver       = config.get("TUIDASH_DNS_RESOLVER") or None
        self._speedtracker_url   = config.get("TUIDASH_SPEEDTESTTRACKER_URL") or None
        self._speedtracker_token = config.get("TUIDASH_SPEEDTESTTRACKER_TOKEN") or None

        self._load()

    def set_refresh_interval(self, seconds: int) -> None:
        if self._data_timer is not None:
            self._data_timer.stop()
        self._data_timer = self.set_interval(seconds, self._load)

    @work(thread=True)
    def _load(self) -> None:
        try:
            resolver_ip   = self._dns_resolver or _get_resolver_ip()
            speed_enabled = self._speedtracker_url is not None
            n_workers     = 3 if speed_enabled else 2

            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                f_reach = pool.submit(_check_reachability, self._ips)
                f_dns   = pool.submit(_check_dns, self._hosts, self._dns_resolver)
                f_speed = (
                    pool.submit(_fetch_speed, self._speedtracker_url, self._speedtracker_token)
                    if speed_enabled else None
                )

            data = ConnectivityData(
                reachability  = f_reach.result(),
                dns           = f_dns.result(),
                speed         = f_speed.result() if f_speed else None,
                speed_enabled = speed_enabled,
                resolver_ip   = resolver_ip,
                max_down_mbps = self._max_down,
                max_up_mbps   = self._max_up,
            )
            self.app.call_from_thread(self._show_data, data)

        except Exception as exc:
            self.app.call_from_thread(self._show_error, str(exc))

    def _show_data(self, data: ConnectivityData) -> None:
        self._err = None
        self.data = data

    def _show_error(self, msg: str) -> None:
        self._err = msg
        self.query_one("#conn-body", Static).update(f"[red]Error:[/red] {msg}")

    def watch_data(self, data: ConnectivityData | None) -> None:
        if data is None or self._err:
            return
        self.border_subtitle = "OK" if data.overall_ok else "FAIL"
        self.query_one("#conn-body", Static).update(_render_connectivity(data))
