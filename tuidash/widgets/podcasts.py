from __future__ import annotations

import hashlib
import json
import os
import socket as _socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

import requests
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.message import Message
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static
from textual import work

from .. import config
from .base import DashWidget


_API_BASE = "https://api.podcastindex.org/api/1.0"

_COVER_COLS = 14
_COVER_ROWS = 7   # half-block rows → 14 pixel rows (square-ish)

# ── pixel art grids (8 tall; rendered as 4 half-block rows) ───────────────────

# Play ► — right-pointing triangle
_PLAY_PIXELS: list[list[int]] = [
    [1, 0, 0, 0, 0, 0, 0, 0],
    [1, 1, 0, 0, 0, 0, 0, 0],
    [1, 1, 1, 0, 0, 0, 0, 0],
    [1, 1, 1, 1, 0, 0, 0, 0],
    [1, 1, 1, 1, 0, 0, 0, 0],
    [1, 1, 1, 0, 0, 0, 0, 0],
    [1, 1, 0, 0, 0, 0, 0, 0],
    [1, 0, 0, 0, 0, 0, 0, 0],
]

# Pause ⏸ — two vertical bars
_PAUSE_PIXELS: list[list[int]] = [
    [1, 1, 0, 0, 0, 1, 1, 0],
    [1, 1, 0, 0, 0, 1, 1, 0],
    [1, 1, 0, 0, 0, 1, 1, 0],
    [1, 1, 0, 0, 0, 1, 1, 0],
    [1, 1, 0, 0, 0, 1, 1, 0],
    [1, 1, 0, 0, 0, 1, 1, 0],
    [1, 1, 0, 0, 0, 1, 1, 0],
    [1, 1, 0, 0, 0, 1, 1, 0],
]


def _pixel_art(pixels: list[list[int]], color: str) -> Text:
    """Render a pixel grid as half-block art using ▀/▄/█/space."""
    t = Text()
    for row in range(0, len(pixels), 2):
        top_row = pixels[row]
        bot_row = pixels[row + 1] if row + 1 < len(pixels) else [0] * len(top_row)
        for col in range(len(top_row)):
            top, bot = top_row[col], bot_row[col]
            if top and bot:
                t.append("█", style=f"{color} on black")
            elif top:
                t.append("▀", style=f"{color} on black")
            elif bot:
                t.append("▄", style=f"{color} on black")
            else:
                t.append(" ", style="on black")
        t.append("\n")
    return t


def _render_cover(data: bytes) -> Text | None:
    """Decode image bytes → half-block art Text, same technique as news_reader.py."""
    try:
        from PIL import Image
        img = Image.open(BytesIO(data)).convert("RGB")
        img = img.resize((_COVER_COLS, _COVER_ROWS * 2), Image.LANCZOS)
        px = img.load()
        t = Text()
        for row in range(_COVER_ROWS):
            for col in range(_COVER_COLS):
                tr, tg, tb = px[col, row * 2]
                br, bg, bb = px[col, row * 2 + 1]
                t.append("▀", style=f"rgb({tr},{tg},{tb}) on rgb({br},{bg},{bb})")
            if row < _COVER_ROWS - 1:
                t.append("\n")
        return t
    except Exception:
        return None


def _fmt_duration(seconds: int) -> str:
    if not seconds:
        return ""
    h, r = divmod(int(seconds), 3600)
    m = r // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _fmt_date(ts: int) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %Y")
    except Exception:
        return ""


# ── data model ────────────────────────────────────────────────────────────────

@dataclass
class Episode:
    id: int
    title: str
    date_published: int = 0
    enclosure_url: str = ""
    duration: int = 0
    image_url: str = ""


@dataclass
class PodcastData:
    feed_id: int
    title: str = ""
    image_url: str = ""
    image_data: bytes | None = None
    episode: Episode | None = None
    error: str = ""


# ── PodcastIndex API ───────────────────────────────────────────────────────────

def _auth_headers(key: str, secret: str) -> dict[str, str]:
    ts = str(int(time.time()))
    h = hashlib.sha1(f"{key}{secret}{ts}".encode()).hexdigest()
    return {
        "X-Auth-Key": key,
        "X-Auth-Date": ts,
        "Authorization": h,
        "User-Agent": "tuidash/1.0",
    }


def _fetch_podcast(feed_id: int, key: str, secret: str) -> PodcastData:
    pd = PodcastData(feed_id=feed_id)
    try:
        r = requests.get(
            f"{_API_BASE}/podcasts/byfeedid",
            params={"id": feed_id},
            headers=_auth_headers(key, secret),
            timeout=15,
        )
        r.raise_for_status()
        feed = r.json().get("feed", {})
        pd.title     = feed.get("title", "") or f"Feed {feed_id}"
        pd.image_url = feed.get("artwork", "") or feed.get("image", "")

        r2 = requests.get(
            f"{_API_BASE}/episodes/byfeedid",
            params={"id": feed_id, "max": 1},
            headers=_auth_headers(key, secret),
            timeout=15,
        )
        r2.raise_for_status()
        items = r2.json().get("items", [])
        if items:
            ep = items[0]
            pd.episode = Episode(
                id=ep.get("id", 0),
                title=ep.get("title", ""),
                date_published=ep.get("datePublished", 0),
                enclosure_url=ep.get("enclosureUrl", ""),
                duration=ep.get("duration", 0),
                image_url=ep.get("image", "") or ep.get("feedImage", ""),
            )
    except Exception as exc:
        pd.error = str(exc)
        if not pd.title:
            pd.title = f"Feed {feed_id}"
    return pd


def _download_cover(pd: PodcastData) -> None:
    url = pd.image_url or (pd.episode.image_url if pd.episode else "")
    if not url:
        return
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "tuidash/1.0"})
        if r.ok and r.content:
            pd.image_data = r.content
    except Exception:
        pass


# ── mpv player ────────────────────────────────────────────────────────────────

class _MpvPlayer:
    """Thin wrapper around mpv --input-ipc-server for gapless seek/pause."""

    _SOCK = "/tmp/tuidash-mpv.sock"

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._paused = False

    # ── public ──

    def play(self, url: str) -> None:
        with self._lock:
            self._kill()
            try:
                os.unlink(self._SOCK)
            except FileNotFoundError:
                pass
            self._proc = subprocess.Popen(
                ["mpv", "--no-video", "--really-quiet",
                 f"--input-ipc-server={self._SOCK}", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._paused = False

    def pause_toggle(self) -> None:
        self._cmd({"command": ["cycle", "pause"]})
        self._paused = not self._paused

    def seek(self, delta: int) -> None:
        self._cmd({"command": ["seek", delta, "relative"]})

    def stop(self) -> None:
        with self._lock:
            self._kill()

    @property
    def running(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    @property
    def paused(self) -> bool:
        return self._paused

    # ── private ──

    def _kill(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self._proc = None
        self._paused = False

    def _cmd(self, payload: dict) -> None:
        for _ in range(15):
            if os.path.exists(self._SOCK):
                break
            time.sleep(0.1)
        try:
            with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(self._SOCK)
                s.sendall(json.dumps(payload).encode() + b"\n")
        except Exception:
            pass


# ── interactive sub-widgets ───────────────────────────────────────────────────

class PlayPauseButton(Widget):
    """Half-block pixel art ▶ / ⏸ button."""

    class Pressed(Message):
        def __init__(self, feed_id: int) -> None:
            super().__init__()
            self.feed_id = feed_id

    DEFAULT_CSS = """
    PlayPauseButton {
        width: auto;
        height: auto;
        padding: 0 1;
    }
    PlayPauseButton:hover { background: $boost; }
    PlayPauseButton:focus { border: tall $accent; }
    """

    def __init__(self, feed_id: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.feed_id = feed_id
        self._playing = False

    def render(self) -> Text:
        if self._playing:
            return _pixel_art(_PAUSE_PIXELS, "bright_yellow")
        return _pixel_art(_PLAY_PIXELS, "bright_green")

    def set_playing(self, value: bool) -> None:
        self._playing = value
        self.refresh()

    def on_click(self) -> None:
        self.post_message(self.Pressed(self.feed_id))

    def on_key(self, event) -> None:
        if event.key in ("enter", "space"):
            event.stop()
            self.post_message(self.Pressed(self.feed_id))


class SeekButton(Widget):
    """◀◀ / ▶▶ seek button (±10 s)."""

    class Pressed(Message):
        def __init__(self, delta: int) -> None:
            super().__init__()
            self.delta = delta

    DEFAULT_CSS = """
    SeekButton {
        width: auto;
        height: auto;
        padding: 0 1;
        border: round $panel;
    }
    SeekButton:hover { background: $boost; }
    SeekButton:focus { border: round $accent; }
    """

    def __init__(self, delta: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.delta = delta

    def render(self) -> Text:
        label = "◀◀ -10s" if self.delta < 0 else "+10s ▶▶"
        return Text(label, style="bold")

    def on_click(self) -> None:
        self.post_message(self.Pressed(self.delta))

    def on_key(self, event) -> None:
        if event.key in ("enter", "space"):
            event.stop()
            self.post_message(self.Pressed(self.delta))


# ── podcast card ──────────────────────────────────────────────────────────────

class PodcastCard(Widget):
    """One podcast section: artwork, episode info, and playback controls."""

    DEFAULT_CSS = """
    PodcastCard {
        height: auto;
        padding: 1 1 0 1;
        margin: 0 0 1 0;
        border: round $panel;
    }
    PodcastCard .card-main {
        height: auto;
    }
    PodcastCard .card-cover {
        width: 16;
        height: auto;
    }
    PodcastCard .card-info {
        width: 1fr;
        height: auto;
        padding: 0 1;
    }
    PodcastCard .card-controls {
        height: auto;
        padding: 1 0 0 0;
        align: left middle;
    }
    """

    def __init__(self, feed_id: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._feed_id = feed_id
        self._data: PodcastData | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(classes="card-main"):
            yield Static("", id=f"cover-{self._feed_id}", classes="card-cover")
            yield Static("[dim]Loading…[/dim]", id=f"info-{self._feed_id}", classes="card-info")
        with Horizontal(classes="card-controls"):
            yield PlayPauseButton(self._feed_id, id=f"play-{self._feed_id}")
            yield SeekButton(-10)
            yield SeekButton(+10)

    def update_data(self, pd: PodcastData) -> None:
        self._data = pd

        cover = self.query_one(f"#cover-{self._feed_id}", Static)
        if pd.image_data:
            art = _render_cover(pd.image_data)
            if art:
                cover.update(art)

        info = Text()
        info.append(pd.title, style="bold")
        if pd.error and not pd.episode:
            info.append(f"\n{pd.error}", style="dim red")
        elif pd.episode:
            ep = pd.episode
            info.append(f"\n{ep.title}")
            parts: list[str] = []
            if ep.date_published:
                parts.append(_fmt_date(ep.date_published))
            if ep.duration:
                parts.append(_fmt_duration(ep.duration))
            if parts:
                info.append(f"\n{' · '.join(parts)}", style="dim")
        else:
            info.append("\nNo episodes found", style="dim")

        self.query_one(f"#info-{self._feed_id}", Static).update(info)

    def set_playing(self, value: bool) -> None:
        try:
            self.query_one(f"#play-{self._feed_id}", PlayPauseButton).set_playing(value)
        except Exception:
            pass

    @property
    def enclosure_url(self) -> str:
        return self._data.episode.enclosure_url if (self._data and self._data.episode) else ""


# ── main widget ───────────────────────────────────────────────────────────────

class PodcastsWidget(DashWidget):
    """Podcast page — fetches PodcastIndex feeds and drives mpv playback."""

    DEFAULT_CSS = """
    PodcastsWidget { height: 100%; }
    #podcasts-scroll { height: 100%; padding: 0 1; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._feed_ids: list[int] = []
        self._key    = ""
        self._secret = ""
        self._player = _MpvPlayer()
        self._playing_id: int | None = None
        self._data_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="podcasts-scroll") as sc:
            sc.can_focus = False
            yield Static("[dim]Loading…[/dim]", id="podcasts-placeholder")

    def on_mount(self) -> None:
        self._key    = config.get("TUIDASH_PODCASTINDEX_KEY",    "") or ""
        self._secret = config.get("TUIDASH_PODCASTINDEX_SECRET", "") or ""
        raw_ids      = config.get("TUIDASH_PODCASTINDEX_IDS",    "") or ""

        if not self._key or not self._secret:
            self._error("Configure TUIDASH_PODCASTINDEX_KEY and TUIDASH_PODCASTINDEX_SECRET")
            return
        try:
            self._feed_ids = [int(x.strip()) for x in raw_ids.split(",") if x.strip()]
        except ValueError:
            self._error("TUIDASH_PODCASTINDEX_IDS must be comma-separated integers")
            return
        if not self._feed_ids:
            self._error("No podcasts configured — set TUIDASH_PODCASTINDEX_IDS")
            return
        self._load()

    def _error(self, msg: str) -> None:
        self.query_one("#podcasts-placeholder", Static).update(f"[dim red]{msg}[/dim red]")

    def set_refresh_interval(self, seconds: int) -> None:
        if self._data_timer is not None:
            self._data_timer.stop()
        self._data_timer = self.set_interval(float(seconds), self._load)

    def on_unmount(self) -> None:
        self._player.stop()

    @work(thread=True)
    def _load(self) -> None:
        if not self._feed_ids or not self._key or not self._secret:
            return

        with ThreadPoolExecutor(max_workers=min(len(self._feed_ids), 4)) as pool:
            futures = {
                pool.submit(_fetch_podcast, fid, self._key, self._secret): fid
                for fid in self._feed_ids
            }
            results: list[PodcastData] = [f.result() for f in as_completed(futures)]

        order = {fid: i for i, fid in enumerate(self._feed_ids)}
        results.sort(key=lambda pd: order.get(pd.feed_id, 999))
        self.app.call_from_thread(self._show_data, results)

        for pd in results:
            _download_cover(pd)
        self.app.call_from_thread(self._show_data, results)

    def _show_data(self, feeds: list[PodcastData]) -> None:
        try:
            self.query_one("#podcasts-placeholder").remove()
        except Exception:
            pass

        scroll = self.query_one("#podcasts-scroll", ScrollableContainer)
        for pd in feeds:
            try:
                card = self.query_one(f"#card-{pd.feed_id}", PodcastCard)
            except Exception:
                card = PodcastCard(pd.feed_id, id=f"card-{pd.feed_id}")
                scroll.mount(card)
            card.update_data(pd)

        n_ok = sum(1 for pd in feeds if not pd.error)
        self.border_subtitle = f"{n_ok}/{len(feeds)} podcasts"

    # ── playback message handlers ─────────────────────────────────────────────

    def on_play_pause_button_pressed(self, event: PlayPauseButton.Pressed) -> None:
        feed_id = event.feed_id

        if self._playing_id == feed_id and self._player.running:
            self._player.pause_toggle()
            try:
                self.query_one(f"#card-{feed_id}", PodcastCard).set_playing(not self._player.paused)
            except Exception:
                pass
            return

        # Switch to a different (or stopped) podcast
        if self._playing_id is not None:
            try:
                self.query_one(f"#card-{self._playing_id}", PodcastCard).set_playing(False)
            except Exception:
                pass

        try:
            url = self.query_one(f"#card-{feed_id}", PodcastCard).enclosure_url
        except Exception:
            url = ""

        if not url:
            self.app.notify("No audio URL for this episode", severity="warning")
            return

        try:
            subprocess.run(["mpv", "--version"], capture_output=True, timeout=2, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self.app.notify("mpv not found — install it to play podcasts", severity="error")
            return

        self._player.play(url)
        self._playing_id = feed_id
        try:
            self.query_one(f"#card-{feed_id}", PodcastCard).set_playing(True)
        except Exception:
            pass

    def on_seek_button_pressed(self, event: SeekButton.Pressed) -> None:
        if self._player.running:
            self._player.seek(event.delta)
        else:
            self.app.notify("Nothing is playing", severity="warning")
