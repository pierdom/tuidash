from __future__ import annotations

import hashlib
import json
import os
import socket as _socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

import requests
from rich.console import Group
from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from .. import config
from ..podcast_progress import store as _progress
from .base import DashWidget


_API_BASE = "https://api.podcastindex.org/api/1.0"

_COVER_COLS = 14
_COVER_ROWS = 7   # half-block rows → 14 pixel rows (square-ish)


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
    episodes: list[Episode] = field(default_factory=list)
    error: str = ""

    @property
    def episode(self) -> Episode | None:
        return self.episodes[0] if self.episodes else None


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
            params={"id": feed_id, "max": 10},
            headers=_auth_headers(key, secret),
            timeout=15,
        )
        r2.raise_for_status()
        for ep_raw in r2.json().get("items", []):
            pd.episodes.append(Episode(
                id=ep_raw.get("id", 0),
                title=ep_raw.get("title", ""),
                date_published=ep_raw.get("datePublished", 0),
                enclosure_url=ep_raw.get("enclosureUrl", ""),
                duration=ep_raw.get("duration", 0),
                image_url=ep_raw.get("image", "") or ep_raw.get("feedImage", ""),
            ))
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

    def play(self, url: str, start_pos: float = 0.0, paused: bool = False) -> None:
        with self._lock:
            self._kill()
            try:
                os.unlink(self._SOCK)
            except FileNotFoundError:
                pass
            args = ["mpv", "--no-video", "--really-quiet",
                    f"--input-ipc-server={self._SOCK}"]
            if start_pos > 0:
                args += [f"--start={start_pos}"]
            if paused:
                args += ["--pause"]
            args.append(url)
            self._proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._paused = paused

    def pause_toggle(self) -> None:
        self._cmd({"command": ["cycle", "pause"]})
        self._paused = not self._paused

    def seek(self, delta: int) -> None:
        self._cmd({"command": ["seek", delta, "relative"]})

    def seek_abs(self, position: float) -> None:
        self._cmd({"command": ["seek", position, "absolute"]})

    def get_property(self, prop: str) -> float | None:
        """Query an mpv property via IPC. Returns None on error or if not running."""
        if not os.path.exists(self._SOCK):
            return None
        try:
            with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(self._SOCK)
                s.sendall(json.dumps({"command": ["get_property", prop]}).encode() + b"\n")
                data = b""
                while b"\n" not in data:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                result = json.loads(data.split(b"\n")[0])
                if result.get("error") == "success":
                    val = result.get("data")
                    return float(val) if val is not None else None
        except Exception:
            pass
        return None

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
            self._proc.kill()  # SIGKILL — cannot be caught or ignored
            try:
                self._proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
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


# Module-level singleton — killed on app shutdown via app.py's _shutdown().
player = _MpvPlayer()

# ── interactive sub-widgets ───────────────────────────────────────────────────

class PlayPauseButton(Widget):
    """Global ▶ / ⏸ toggle for whatever is currently playing."""

    class Toggled(Message):
        pass

    DEFAULT_CSS = """
    PlayPauseButton {
        width: auto;
        height: 4;
        border: round $panel;
        padding: 0 1;
        content-align: center middle;
    }
    PlayPauseButton:hover { background: $boost; }
    PlayPauseButton:focus { border: round $accent; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._playing = False

    def render(self) -> Text:
        if self._playing:
            return Text("⏸", style="bright_yellow bold")
        return Text("▶", style="bright_green bold")

    def set_playing(self, value: bool) -> None:
        self._playing = value
        self.refresh()

    def on_click(self) -> None:
        self.post_message(self.Toggled())

    def on_key(self, event) -> None:
        if event.key in ("enter", "space"):
            event.stop()
            self.post_message(self.Toggled())


class EpisodePlayButton(Widget):
    """Small per-card ▶ button — selects this episode and starts playing."""

    class Pressed(Message):
        def __init__(self, feed_id: int) -> None:
            super().__init__()
            self.feed_id = feed_id

    DEFAULT_CSS = """
    EpisodePlayButton:hover { background: $boost; }
    EpisodePlayButton:focus { background: $accent 20%; }
    """

    def __init__(self, feed_id: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.feed_id = feed_id

    def render(self) -> Text:
        return Text("▶ Play", style="bright_green bold")

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


class MarkListenedButton(Widget):
    """✓ — mark episode as completed."""

    class Pressed(Message):
        def __init__(self, feed_id: int, episode_id: int) -> None:
            super().__init__()
            self.feed_id    = feed_id
            self.episode_id = episode_id

    DEFAULT_CSS = """
    MarkListenedButton { width: 3; height: 1; }
    MarkListenedButton:hover { background: $boost; }
    MarkListenedButton:focus { background: $boost; }
    """

    def __init__(self, feed_id: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.feed_id     = feed_id
        self._episode_id = 0

    def set_episode(self, episode_id: int) -> None:
        self._episode_id = episode_id

    def render(self) -> Text:
        return Text(" ✓ ", style="dim green")

    def on_click(self) -> None:
        if self._episode_id:
            self.post_message(self.Pressed(self.feed_id, self._episode_id))

    def on_key(self, event) -> None:
        if event.key in ("enter", "space"):
            event.stop()
            if self._episode_id:
                self.post_message(self.Pressed(self.feed_id, self._episode_id))


class ResetEpisodeButton(Widget):
    """↺ — reset episode to new."""

    class Pressed(Message):
        def __init__(self, feed_id: int, episode_id: int) -> None:
            super().__init__()
            self.feed_id    = feed_id
            self.episode_id = episode_id

    DEFAULT_CSS = """
    ResetEpisodeButton { width: 3; height: 1; }
    ResetEpisodeButton:hover { background: $boost; }
    ResetEpisodeButton:focus { background: $boost; }
    """

    def __init__(self, feed_id: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.feed_id     = feed_id
        self._episode_id = 0

    def set_episode(self, episode_id: int) -> None:
        self._episode_id = episode_id

    def render(self) -> Text:
        return Text(" ↺ ", style="dim")

    def on_click(self) -> None:
        if self._episode_id:
            self.post_message(self.Pressed(self.feed_id, self._episode_id))

    def on_key(self, event) -> None:
        if event.key in ("enter", "space"):
            event.stop()
            if self._episode_id:
                self.post_message(self.Pressed(self.feed_id, self._episode_id))


class PrevEpisodeButton(Widget):
    """◀ — show older episode in the same card."""

    class Pressed(Message):
        def __init__(self, feed_id: int) -> None:
            super().__init__()
            self.feed_id = feed_id

    DEFAULT_CSS = """
    PrevEpisodeButton { width: auto; height: 1; }
    PrevEpisodeButton:hover { background: $boost; }
    PrevEpisodeButton:focus { background: $boost; }
    """

    def __init__(self, feed_id: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.feed_id = feed_id
        self._enabled = True

    def set_enabled(self, value: bool) -> None:
        self._enabled = value
        self.refresh()

    def render(self) -> Text:
        return Text(" ← ", style="" if self._enabled else "dim")

    def on_click(self) -> None:
        if self._enabled:
            self.post_message(self.Pressed(self.feed_id))

    def on_key(self, event) -> None:
        if event.key in ("enter", "space"):
            event.stop()
            if self._enabled:
                self.post_message(self.Pressed(self.feed_id))


class NextEpisodeButton(Widget):
    """→ — show newer episode in the same card."""

    class Pressed(Message):
        def __init__(self, feed_id: int) -> None:
            super().__init__()
            self.feed_id = feed_id

    DEFAULT_CSS = """
    NextEpisodeButton { width: auto; height: 1; }
    NextEpisodeButton:hover { background: $boost; }
    NextEpisodeButton:focus { background: $boost; }
    """

    def __init__(self, feed_id: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.feed_id = feed_id
        self._enabled = True

    def set_enabled(self, value: bool) -> None:
        self._enabled = value
        self.refresh()

    def render(self) -> Text:
        return Text(" → ", style="" if self._enabled else "dim")

    def on_click(self) -> None:
        if self._enabled:
            self.post_message(self.Pressed(self.feed_id))

    def on_key(self, event) -> None:
        if event.key in ("enter", "space"):
            event.stop()
            if self._enabled:
                self.post_message(self.Pressed(self.feed_id))


# ── playback progress bar ─────────────────────────────────────────────────────

def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class PlaybackBar(Widget):
    """Clickable / draggable playback progress bar.

    Click or drag anywhere on the bar to seek to that position.
    """

    class SeekTo(Message):
        def __init__(self, position: float) -> None:
            super().__init__()
            self.position = position

    position: reactive[float] = reactive(0.0)
    duration: reactive[float] = reactive(0.0)
    label:    reactive[str]   = reactive("")

    DEFAULT_CSS = """
    PlaybackBar {
        height: 4;
        border: round $panel;
        padding: 0 1;
    }
    PlaybackBar:hover { background: $boost; }
    """

    def render(self) -> Group:
        w = self.content_size.width or 1

        # ── label line ──
        if self.label:
            line1 = Text(self.label[:w], style="bold", no_wrap=True)
        else:
            line1 = Text("No podcast playing", style="dim", no_wrap=True)

        # ── bar line ──
        time_str = f" {_fmt_time(self.position)} / {_fmt_time(self.duration)}"
        bar_w = max(1, w - len(time_str))
        # store for seek calculation
        self._bar_w = bar_w

        if self.duration > 0:
            filled = round(bar_w * min(self.position, self.duration) / self.duration)
        else:
            filled = 0

        bar = Text(no_wrap=True)
        bar.append("█" * filled,           style="bright_green")
        bar.append("░" * (bar_w - filled), style="dim green")
        bar.append(time_str,               style="dim")

        return Group(line1, bar)

    # ── mouse interaction ────────────────────────────────────────────────────

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self.capture_mouse()
        self._seek_from_x(event.x)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if event.button:   # any button held
            self._seek_from_x(event.x)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        self.release_mouse()

    def _seek_from_x(self, x: int) -> None:
        if self.duration <= 0:
            return
        # content starts at x=2 (1 border + 1 padding); bar_w set at last render
        bar_x = x - 2
        bar_w = getattr(self, "_bar_w", self.content_size.width)
        ratio = max(0.0, min(1.0, bar_x / max(1, bar_w - 1)))
        self.post_message(self.SeekTo(ratio * self.duration))


# ── podcast card ──────────────────────────────────────────────────────────────

class PodcastCard(Widget):
    """One podcast section: artwork, episode info, and playback controls."""

    DEFAULT_CSS = """
    PodcastCard {
        height: 9;
        padding: 0 1 0 1;
        margin: 0 0 1 0;
        border: round $panel;
    }
    PodcastCard .card-main {
        height: 7;
    }
    PodcastCard .card-cover {
        width: 16;
        height: auto;
    }
    PodcastCard .card-right {
        width: 1fr;
        height: 7;
        padding: 0 1;
    }
    PodcastCard .card-title-row {
        width: 1fr;
        height: 1;
        align: left top;
    }
    PodcastCard .card-title {
        width: 1fr;
        height: 1;
    }
    PodcastCard .card-info {
        width: 1fr;
        height: 5;
    }
    PodcastCard .card-play {
        height: 1;
        width: 1fr;
        align: left middle;
    }
    """

    def __init__(self, feed_id: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._feed_id = feed_id
        self._data: PodcastData | None = None
        self._ep_idx: int = 0

    def compose(self) -> ComposeResult:
        with Horizontal(classes="card-main"):
            yield Static("", id=f"cover-{self._feed_id}", classes="card-cover")
            with Vertical(classes="card-right"):
                with Horizontal(classes="card-title-row"):
                    yield Static("[dim]Loading…[/dim]", id=f"title-{self._feed_id}", classes="card-title")
                    yield MarkListenedButton(self._feed_id, id=f"mark-{self._feed_id}")
                    yield ResetEpisodeButton(self._feed_id, id=f"reset-{self._feed_id}")
                yield Static("", id=f"info-{self._feed_id}", classes="card-info")
                with Horizontal(classes="card-play"):
                    yield EpisodePlayButton(self._feed_id, id=f"play-{self._feed_id}")
                    yield PrevEpisodeButton(self._feed_id, id=f"ep-prev-{self._feed_id}")
                    yield NextEpisodeButton(self._feed_id, id=f"ep-next-{self._feed_id}")

    def update_data(self, pd: PodcastData) -> None:
        self._data = pd

        if pd.image_data:
            art = _render_cover(pd.image_data)
            if art:
                self.query_one(f"#cover-{self._feed_id}", Static).update(art)

        self.query_one(f"#title-{self._feed_id}", Static).update(Text(pd.title, style="bold"))

        if pd.error and not pd.episodes:
            self.query_one(f"#info-{self._feed_id}", Static).update(Text(pd.error, style="dim red"))
            return

        if not pd.episodes:
            self.query_one(f"#info-{self._feed_id}", Static).update(Text("No episodes found", style="dim"))
            return

        self._ep_idx = min(self._ep_idx, len(pd.episodes) - 1)
        self._show_episode(self._ep_idx)

    def _show_episode(self, idx: int) -> None:
        if not self._data or not self._data.episodes:
            return
        episodes = self._data.episodes
        idx = max(0, min(idx, len(episodes) - 1))
        self._ep_idx = idx
        ep = episodes[idx]
        is_last = idx == len(episodes) - 1

        info = Text()
        info.append(ep.title)
        parts: list[str] = []
        if ep.date_published:
            parts.append(_fmt_date(ep.date_published))
        if ep.duration:
            parts.append(_fmt_duration(ep.duration))
        if parts:
            info.append(f"\n{' · '.join(parts)}", style="dim")

        status = _progress.get_status(ep.id, ep.date_published)
        if status == "new":
            info.append("  ● NEW", style="bold bright_green")
        elif status == "started":
            saved_pos = _progress.get_position(ep.id)
            info.append(f"  ▶ {_fmt_time(saved_pos)}", style="bright_yellow")
        elif status == "completed":
            info.append("  ✓", style="dim green")

        if is_last:
            info.append("  [last]", style="dim")

        self.query_one(f"#info-{self._feed_id}", Static).update(info)

        try:
            self.query_one(f"#mark-{self._feed_id}", MarkListenedButton).set_episode(ep.id)
            self.query_one(f"#reset-{self._feed_id}", ResetEpisodeButton).set_episode(ep.id)
        except Exception:
            pass

        try:
            self.query_one(f"#ep-prev-{self._feed_id}", PrevEpisodeButton).set_enabled(idx < len(episodes) - 1)
            self.query_one(f"#ep-next-{self._feed_id}", NextEpisodeButton).set_enabled(idx > 0)
        except Exception:
            pass

    def on_prev_episode_button_pressed(self, event: PrevEpisodeButton.Pressed) -> None:
        self._show_episode(self._ep_idx + 1)

    def on_next_episode_button_pressed(self, event: NextEpisodeButton.Pressed) -> None:
        self._show_episode(self._ep_idx - 1)

    @property
    def enclosure_url(self) -> str:
        if not self._data or not self._data.episodes:
            return ""
        return self._data.episodes[self._ep_idx].enclosure_url


# ── main widget ───────────────────────────────────────────────────────────────

class PodcastsWidget(DashWidget):
    """Podcast page — fetches PodcastIndex feeds and drives mpv playback."""

    DEFAULT_CSS = """
    PodcastsWidget { height: 100%; }
    PodcastsWidget > Vertical { height: 100%; }
    #podcasts-scroll { height: 1fr; padding: 0 1; }
    #podcasts-grid {
        layout: grid;
        grid-size: 2;
        grid-rows: 10;
        grid-gutter: 1;
        height: auto;
        width: 100%;
    }
    #podcasts-controls {
        height: 4;
        margin: 0 1 0 1;
        align: left middle;
    }
    #podcasts-bar {
        width: 1fr;
        height: 4;
        border: round $panel;
        border-title-color: $accent;
        border-title-style: bold;
        margin: 0 1;
    }
    #podcasts-controls SeekButton {
        height: 4;
        border: round $panel;
        padding: 0 1;
        width: auto;
        content-align: center middle;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._feed_ids: list[int] = []
        self._key     = ""
        self._secret  = ""
        self._playing_id: int | None = None
        self._playing_episode_id: int | None = None  # PodcastIndex episode ID for progress tracking
        self._data_timer: Timer | None = None
        self._poll_timer: Timer | None = None
        self._now_playing = ""
        self._auto_resumed = False

    def compose(self) -> ComposeResult:
        with Vertical():
            with ScrollableContainer(id="podcasts-scroll") as sc:
                sc.can_focus = False
                with Widget(id="podcasts-grid"):
                    yield Static("[dim]Loading…[/dim]", id="podcasts-placeholder")
            with Horizontal(id="podcasts-controls"):
                yield SeekButton(-10)
                yield PlayPauseButton(id="podcasts-playpause")
                yield PlaybackBar(id="podcasts-bar")
                yield SeekButton(+10)

    def on_mount(self) -> None:
        self._key    = config.get("TUIDASH_PODCASTINDEX_KEY",    "") or ""
        self._secret = config.get("TUIDASH_PODCASTINDEX_SECRET", "") or ""
        raw_ids      = config.get("TUIDASH_PODCASTINDEX_IDS",    "") or ""

        self.query_one(PlaybackBar).border_title = "  Now Playing"

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

        self._poll_timer = self.set_interval(0.5, self._trigger_poll)
        self._load()

    def _error(self, msg: str) -> None:
        self.query_one("#podcasts-placeholder", Static).update(f"[dim red]{msg}[/dim red]")

    def set_refresh_interval(self, seconds: int) -> None:
        if self._data_timer is not None:
            self._data_timer.stop()
        self._data_timer = self.set_interval(float(seconds), self._load)

    def on_unmount(self) -> None:
        player.stop()

    # ── data loading ──────────────────────────────────────────────────────────

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

        grid = self.query_one("#podcasts-grid")
        for pd in feeds:
            try:
                card = self.query_one(f"#card-{pd.feed_id}", PodcastCard)
            except Exception:
                card = PodcastCard(pd.feed_id, id=f"card-{pd.feed_id}")
                grid.mount(card)
            card.update_data(pd)

        n_ok = sum(1 for pd in feeds if not pd.error)
        self.border_subtitle = f"{n_ok}/{len(feeds)} podcasts"

        # Auto-resume the most recently started episode (paused) on first load only.
        if not player.running and not self._auto_resumed:
            self._auto_resumed = True
            started = _progress.latest_started()
            if started:
                ep_id, pos = started
                for pd in feeds:
                    ep = next((e for e in pd.episodes if e.id == ep_id), None)
                    if ep and ep.enclosure_url:
                        ep_idx = pd.episodes.index(ep)
                        self._now_playing = f"{pd.title} — {ep.title}"
                        self._playing_episode_id = ep_id
                        self._playing_id = pd.feed_id
                        player.play(ep.enclosure_url, start_pos=pos, paused=True)
                        bar = self.query_one(PlaybackBar)
                        bar.position = pos
                        bar.duration = float(ep.duration) if ep.duration else 0.0
                        bar.label    = self._now_playing
                        self._set_global_playing(False)
                        try:
                            card = self.query_one(f"#card-{pd.feed_id}", PodcastCard)
                            card._ep_idx = ep_idx
                            card._show_episode(ep_idx)
                        except Exception:
                            pass
                        break

    # ── playback polling ──────────────────────────────────────────────────────

    def _trigger_poll(self) -> None:
        if player.running:
            self._poll_worker()
        elif self._playing_id is not None:
            # mpv exited naturally — reset global button and bar
            self._playing_id = None
            self._playing_episode_id = None
            self.query_one(PlayPauseButton).set_playing(False)
            bar = self.query_one(PlaybackBar)
            bar.position = 0.0
            bar.label = ""

    @work(thread=True)
    def _poll_worker(self) -> None:
        pos = player.get_property("time-pos")
        dur = player.get_property("duration")
        status = None
        if pos is not None and dur and self._playing_episode_id:
            status = _progress.update(self._playing_episode_id, pos, dur)
        self.app.call_from_thread(self._update_bar, pos, dur, status)

    def _update_bar(self, pos: float | None, dur: float | None, status: str | None) -> None:
        bar = self.query_one(PlaybackBar)
        if pos is not None:
            bar.position = pos
        if dur is not None and dur > 0:
            bar.duration = dur
        bar.label = self._now_playing
        # Refresh the card's status badge if it changed
        if status and self._playing_id is not None:
            try:
                card = self.query_one(f"#card-{self._playing_id}", PodcastCard)
                card.update_data(card._data)
            except Exception:
                pass

    def _set_global_playing(self, value: bool) -> None:
        self.query_one(PlayPauseButton).set_playing(value)

    # ── playback message handlers ─────────────────────────────────────────────

    def on_play_pause_button_toggled(self, event: PlayPauseButton.Toggled) -> None:
        """Global ▶/⏸ button in the bar — toggle pause on whatever is playing."""
        if not player.running:
            self.app.notify("Nothing is playing", severity="warning")
            return
        player.pause_toggle()
        self._set_global_playing(not player.paused)

    def on_episode_play_button_pressed(self, event: EpisodePlayButton.Pressed) -> None:
        """Per-card ▶ Play button — start (or switch to) that episode."""
        feed_id = event.feed_id

        try:
            card = self.query_one(f"#card-{feed_id}", PodcastCard)
            url  = card.enclosure_url
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

        try:
            pd = card._data
            ep = pd.episodes[card._ep_idx] if (pd and pd.episodes) else None
            ep_title = ep.title if ep else ""
            self._now_playing = f"{pd.title} — {ep_title}" if (pd and ep_title) else (pd.title if pd else "")
            self._playing_episode_id = ep.id if ep else None
            start_pos = _progress.get_position(ep.id) if ep else 0.0
        except Exception:
            self._now_playing = ""
            self._playing_episode_id = None
            start_pos = 0.0

        player.play(url, start_pos=start_pos)
        self._playing_id = feed_id

        bar = self.query_one(PlaybackBar)
        bar.position = start_pos
        bar.duration = 0.0
        bar.label    = self._now_playing
        self._set_global_playing(True)

    def on_seek_button_pressed(self, event: SeekButton.Pressed) -> None:
        if player.running:
            player.seek(event.delta)
        else:
            self.app.notify("Nothing is playing", severity="warning")

    def _refresh_card(self, feed_id: int) -> None:
        try:
            card = self.query_one(f"#card-{feed_id}", PodcastCard)
            card.update_data(card._data)
        except Exception:
            pass

    def on_mark_listened_button_pressed(self, event: MarkListenedButton.Pressed) -> None:
        try:
            card = self.query_one(f"#card-{event.feed_id}", PodcastCard)
            dur  = card._data.episodes[card._ep_idx].duration if (card._data and card._data.episodes) else 0.0
        except Exception:
            dur = 0.0
        _progress.mark_completed(event.episode_id, dur)
        self._refresh_card(event.feed_id)

    def on_reset_episode_button_pressed(self, event: ResetEpisodeButton.Pressed) -> None:
        _progress.reset(event.episode_id)
        self._refresh_card(event.feed_id)

    def on_playback_bar_seek_to(self, event: PlaybackBar.SeekTo) -> None:
        if player.running:
            player.seek_abs(event.position)
            self.query_one(PlaybackBar).position = event.position
        else:
            self.app.notify("Nothing is playing", severity="warning")
