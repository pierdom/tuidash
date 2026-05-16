from __future__ import annotations

import platform
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich.align import Align
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


# ── data model ────────────────────────────────────────────────────────────────

@dataclass
class NetInfo:
    iface: str = ""
    is_wifi: bool = False
    ssid: str | None = None
    rssi: int | None = None  # dBm (negative)


# ── platform detection ────────────────────────────────────────────────────────

_MACOS_VIRTUAL = ("utun", "gif", "stf", "lo", "bridge", "p2p", "awdl", "llw", "anpi", "vmnet")
_LINUX_VIRTUAL = ("tun", "tap", "wg", "lo", "docker", "veth", "br", "virbr", "dummy")


def _is_virtual_macos(iface: str) -> bool:
    return any(iface.startswith(p) for p in _MACOS_VIRTUAL)


def _is_virtual_linux(iface: str) -> bool:
    return any(iface.startswith(p) for p in _LINUX_VIRTUAL)


def _get_net_info() -> NetInfo:
    system = platform.system()
    if system == "Darwin":
        return _macos_net_info()
    if system == "Linux":
        return _linux_net_info()
    return NetInfo()


def _macos_net_info() -> NetInfo:
    iface = ""
    try:
        out = subprocess.check_output(
            ["route", "-n", "get", "default"],
            text=True, stderr=subprocess.DEVNULL, timeout=2,
        )
        for line in out.splitlines():
            if "interface:" in line:
                candidate = line.split(":", 1)[1].strip()
                if not _is_virtual_macos(candidate):
                    iface = candidate
                break
    except Exception:
        pass

    # VPN or no default route: find the first active physical interface
    if not iface:
        iface = _macos_active_physical_iface()
    if not iface:
        return NetInfo()

    is_wifi = _macos_is_wifi(iface)
    if not is_wifi:
        return NetInfo(iface=iface)

    ssid, rssi = _macos_wifi_details()
    return NetInfo(iface=iface, is_wifi=True, ssid=ssid, rssi=rssi)


def _macos_active_physical_iface() -> str:
    """Return the first active non-virtual interface with an IPv4 address."""
    try:
        hw_out = subprocess.check_output(
            ["networksetup", "-listallhardwareports"],
            text=True, stderr=subprocess.DEVNULL, timeout=3,
        )
        devices = [
            line.split(":", 1)[1].strip()
            for line in hw_out.splitlines()
            if line.strip().startswith("Device:")
        ]
        for dev in devices:
            if _is_virtual_macos(dev):
                continue
            try:
                ifc = subprocess.check_output(
                    ["ifconfig", dev], text=True, stderr=subprocess.DEVNULL, timeout=2,
                )
                if "status: active" in ifc and "inet " in ifc:
                    return dev
            except Exception:
                pass
    except Exception:
        pass
    return ""


def _macos_is_wifi(iface: str) -> bool:
    try:
        out = subprocess.check_output(
            ["networksetup", "-listallhardwareports"],
            text=True, stderr=subprocess.DEVNULL, timeout=3,
        )
        current_wifi = False
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Hardware Port:"):
                port = line.split(":", 1)[1].strip().lower()
                current_wifi = any(kw in port for kw in ("wi-fi", "airport", "wireless"))
            elif line.startswith("Device:") and line.split(":", 1)[1].strip() == iface:
                return current_wifi
    except Exception:
        pass
    return False


def _macos_wifi_details() -> tuple[str | None, int | None]:
    airport = (
        "/System/Library/PrivateFrameworks/Apple80211.framework"
        "/Versions/Current/Resources/airport"
    )
    ssid: str | None = None
    rssi: int | None = None
    try:
        out = subprocess.check_output(
            [airport, "-I"], text=True, stderr=subprocess.DEVNULL, timeout=3,
        )
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("SSID:") and ssid is None:
                ssid = line.split(":", 1)[1].strip() or None
            elif line.startswith("agrCtlRSSI:"):
                rssi = int(line.split(":", 1)[1].strip())
    except Exception:
        pass
    return ssid, rssi


def _linux_net_info() -> NetInfo:
    iface = ""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 8 and parts[1] == "00000000" and parts[7] == "00000000":
                    candidate = parts[0]
                    if not _is_virtual_linux(candidate):
                        iface = candidate
                    break
    except Exception:
        pass
    if not iface:
        try:
            out = subprocess.check_output(
                ["ip", "route", "get", "8.8.8.8"],
                text=True, stderr=subprocess.DEVNULL, timeout=2,
            )
            m = re.search(r"\bdev\s+(\S+)", out)
            if m and not _is_virtual_linux(m.group(1)):
                iface = m.group(1)
        except Exception:
            pass
    # VPN active: fall back to first active physical interface
    if not iface:
        iface = _linux_active_physical_iface()
    if not iface:
        return NetInfo()

    is_wifi = Path(f"/sys/class/net/{iface}/wireless").exists()
    if not is_wifi:
        return NetInfo(iface=iface)

    ssid = _linux_wifi_ssid(iface)
    rssi = _linux_wifi_rssi(iface)
    return NetInfo(iface=iface, is_wifi=True, ssid=ssid, rssi=rssi)


def _linux_active_physical_iface() -> str:
    """Return the first active non-virtual interface with an IPv4 address."""
    try:
        net_dir = Path("/sys/class/net")
        for iface_path in sorted(net_dir.iterdir()):
            name = iface_path.name
            if _is_virtual_linux(name):
                continue
            carrier = (iface_path / "carrier")
            try:
                if carrier.read_text().strip() != "1":
                    continue
            except Exception:
                continue
            # Verify it has an IPv4 address via ip addr
            try:
                out = subprocess.check_output(
                    ["ip", "addr", "show", name],
                    text=True, stderr=subprocess.DEVNULL, timeout=2,
                )
                if re.search(r"\binet\s+\d+\.\d+\.\d+\.\d+", out):
                    return name
            except Exception:
                pass
    except Exception:
        pass
    return ""


def _linux_wifi_ssid(iface: str) -> str | None:
    try:
        ssid = subprocess.check_output(
            ["iwgetid", "-r", iface], text=True, stderr=subprocess.DEVNULL, timeout=2,
        ).strip()
        return ssid or None
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
            text=True, stderr=subprocess.DEVNULL, timeout=3,
        )
        for line in out.splitlines():
            if line.lower().startswith("yes:"):
                return line[4:] or None
    except Exception:
        pass
    return None


def _linux_wifi_rssi(iface: str) -> int | None:
    try:
        with open("/proc/net/wireless") as f:
            lines = f.readlines()[2:]
        for line in lines:
            if ":" not in line:
                continue
            name, rest = line.split(":", 1)
            if name.strip() == iface:
                parts = rest.split()
                # parts: status link level noise ...
                level = float(parts[2].rstrip("."))
                return int(level) if level < 0 else int(level - 100)
    except Exception:
        pass
    return None


# ── rendering ─────────────────────────────────────────────────────────────────

def _rssi_bars(rssi: int) -> Text:
    if rssi >= -50:
        filled, style = 4, "green"
    elif rssi >= -60:
        filled, style = 3, "green"
    elif rssi >= -70:
        filled, style = 2, "yellow"
    else:
        filled, style = 1, "red"
    t = Text()
    for i, c in enumerate("▂▄▆█"):
        t.append(c, style=style if i < filled else "dim")
    return t


def _render_net(info: NetInfo) -> Text:
    if not info.iface:
        return Text("no link", style="dim")
    t = Text()
    if info.is_wifi:
        t.append("≋ ", style="cyan")
    else:
        t.append("⌁ ", style="cyan")
    t.append(info.iface, style="")
    if info.is_wifi:
        if info.ssid:
            t.append(f"  {info.ssid}", style="bright_white")
        if info.rssi is not None:
            t.append("  ")
            t.append_text(_rssi_bars(info.rssi))
    return t


# ── widgets ───────────────────────────────────────────────────────────────────

class NetStatusWidget(Widget):
    """Live network interface status: icon, SSID, and signal bars for Wi-Fi."""

    DEFAULT_CSS = """
    NetStatusWidget {
        width: auto;
        height: 1;
        padding: 0 1;
    }
    """

    _info: reactive[NetInfo] = reactive(NetInfo(), repaint=True)

    def on_mount(self) -> None:
        self._poll()
        self.set_interval(30, self._poll)

    def render(self) -> Text:
        return _render_net(self._info)

    @work(thread=True)
    def _poll(self) -> None:
        info = _get_net_info()
        self.app.call_from_thread(setattr, self, "_info", info)


class PlayStatusWidget(Widget):
    """▶ / ⏸ indicator in the header — visible while a podcast is playing or paused."""

    DEFAULT_CSS = """
    PlayStatusWidget {
        width: auto;
        height: 1;
        padding: 0 1;
    }
    PlayStatusWidget:hover { background: $boost; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._playing = False
        self._paused  = False

    def set_playback(self, playing: bool, paused: bool) -> None:
        self._playing = playing
        self._paused  = paused
        self.refresh()

    def render(self) -> Text:
        if not self._playing:
            return Text("")
        if self._paused:
            return Text("⏸", style="bright_yellow")
        return Text("▶", style="bright_green")

    def on_click(self) -> None:
        if self._playing:
            self.app.action_toggle_playback()


class DashHeader(Widget):
    """App header: net status (left) · title + subtitle (center) · play status + clock (right)."""

    DEFAULT_CSS = """
    DashHeader {
        dock: top;
        height: 1;
        background: $panel;
        color: $text;
        layout: horizontal;
    }
    DashHeader > NetStatusWidget {
        color: $text-muted;
    }
    DashHeader > #title-center {
        width: 1fr;
        height: 1;
        text-align: center;
        content-align: center middle;
    }
    DashHeader > PlayStatusWidget {
        height: 1;
    }
    DashHeader > #clock-right {
        width: auto;
        min-width: 9;
        height: 1;
        padding: 0 1;
        text-align: right;
        content-align: right middle;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        yield NetStatusWidget()
        yield Static("", id="title-center")
        yield PlayStatusWidget()
        yield Static("", id="clock-right")

    def on_mount(self) -> None:
        self._tick_clock()
        self.set_interval(1, self._tick_clock)
        self._refresh_title()

    def _tick_clock(self) -> None:
        self.query_one("#clock-right", Static).update(
            Text(datetime.now().strftime("%H:%M:%S"), style="dim")
        )

    def _refresh_title(self, subtitle: str = "") -> None:
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(self.app.TITLE, style="bold")
        if subtitle:
            t.append("  ")
            t.append(subtitle, style="dim")
        self.query_one("#title-center", Static).update(Align.center(t))

    def set_subtitle(self, text: str) -> None:
        self._refresh_title(text)

    def set_playback(self, playing: bool, paused: bool) -> None:
        try:
            self.query_one(PlayStatusWidget).set_playback(playing, paused)
        except Exception:
            pass
