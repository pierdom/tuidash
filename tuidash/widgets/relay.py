from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests
from rich.console import Group
from rich.markdown import Markdown as RichMarkdown
from rich.rule import Rule
from rich.style import Style
from rich.text import Text
from rich.theme import Theme
from textual.app import ComposeResult
from textual.containers import ScrollableContainer
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static
from textual import work

from .. import config
from ..theme import ACCENT
from .base import DashWidget


# ── palette-aware Markdown ──────────────────────────────────────────────────────

_MD_THEME = Theme({
    "markdown.h1":        Style(bold=True, color=ACCENT, underline=True),
    "markdown.h1.border": Style(color=ACCENT),
    "markdown.h2":        Style(bold=True, color=ACCENT),
    "markdown.h3":        Style(bold=True, color=ACCENT),
    "markdown.h4":        Style(bold=True, color=ACCENT),
    "markdown.code":      Style(bold=True, color=ACCENT),
    "markdown.link":      Style(color=ACCENT),
})


class _PaletteMarkdown(RichMarkdown):
    def __rich_console__(self, console, options):
        with console.use_theme(_MD_THEME):
            yield from super().__rich_console__(console, options)


_BASE_URL = (config.get("TUIDASH_RELAY_URL") or "").rstrip("/")
_TOKEN = config.get("TUIDASH_RELAY_TOKEN") or ""
_LIMIT = 20


def _auth(last_event_id: int | None = None) -> dict[str, str]:
    h: dict[str, str] = {"Authorization": f"Bearer {_TOKEN}"}
    if last_event_id is not None:
        h["Last-Event-ID"] = str(last_event_id)
    return h


# ── data model ─────────────────────────────────────────────────────────────────

@dataclass
class RelayPost:
    id: int
    title: str
    content: str
    source: str
    created_at: datetime

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RelayPost:
        return cls(
            id=int(d["id"]),
            title=d.get("title", ""),
            content=d.get("content", ""),
            source=d.get("source", ""),
            created_at=datetime.fromisoformat(d["created_at"].replace("Z", "+00:00")),
        )


# ── REST fetch ─────────────────────────────────────────────────────────────────

def _fetch_posts(topic: str, limit: int = _LIMIT) -> list[RelayPost]:
    if not _BASE_URL:
        raise RuntimeError("Missing required env var: TUIDASH_RELAY_URL")
    if not _TOKEN:
        raise RuntimeError("Missing required env var: TUIDASH_RELAY_TOKEN")
    resp = requests.get(
        f"{_BASE_URL}/posts",
        params={"tag": topic, "limit": limit},
        headers=_auth(),
        timeout=15,
    )
    resp.raise_for_status()
    return [RelayPost.from_dict(p) for p in resp.json()["items"]]


# ── rendering ──────────────────────────────────────────────────────────────────

def _render_posts(posts: list[RelayPost], show_title: bool = True) -> Group:
    if not posts:
        return Group(Text("No posts yet", style="dim"))

    items: list[Any] = []
    for i, post in enumerate(posts):
        if i > 0:
            items.append(Rule(style="dim"))
        if show_title:
            ts = post.created_at.astimezone().strftime("%Y-%m-%d %H:%M")
            header = Text()
            header.append(post.title, style="bold")
            header.append(f"  {post.source}  {ts}", style="dim")
            items.append(header)
        if post.content.strip():
            items.append(_PaletteMarkdown(post.content))

    return Group(*items)


# ── widget ─────────────────────────────────────────────────────────────────────

class RelayWidget(DashWidget):
    """Live Markdown feed from a relay server (TUIDASH_RELAY_URL), filtered by topic tag.

    Connects via SSE for real-time push and seeds initial content via REST.
    Multiple instances with different topics can coexist on the same screen.
    """

    _mobile_scrollable = True
    can_focus = True

    data: reactive[list[RelayPost] | None] = reactive(None, always_update=True)

    DEFAULT_CSS = """
    RelayWidget                      { height: 1fr; }
    RelayWidget ScrollableContainer  { height: 1fr; }
    RelayWidget Static               { height: auto; }
    """

    def __init__(self, topic: str, title: str | None = None, show_title: bool = True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._topic      = topic
        self._title      = title or f"Relay ({topic})"
        self._show_title = show_title
        self._posts:   list[RelayPost] = []
        self._last_id: int | None      = None
        self._err:     str | None      = None
        self._data_timer: Timer | None = None
        self._stop     = threading.Event()
        self._sse_resp: requests.Response | None = None

    def compose(self) -> ComposeResult:
        with ScrollableContainer() as sc:
            sc.can_focus = False
            yield Static("[dim]Loading…[/dim]")

    def on_mount(self) -> None:
        self.border_title = f"  {self._title}"
        self._load()
        self._listen()

    def on_unmount(self) -> None:
        self._stop.set()
        resp = self._sse_resp
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def set_refresh_interval(self, seconds: int) -> None:
        if self._data_timer is not None:
            self._data_timer.stop()
        self._data_timer = self.set_interval(float(seconds), self._load)

    # ── REST fetch (seed + periodic refresh) ───────────────────────────────────

    @work(thread=True)
    def _load(self) -> None:
        try:
            posts = _fetch_posts(self._topic)
            self.app.call_from_thread(self._merge_posts, posts)
        except Exception as exc:
            self.app.call_from_thread(self._show_error, str(exc))

    def _merge_posts(self, incoming: list[RelayPost]) -> None:
        self._err = None
        seen      = {p.id for p in self._posts}
        new       = [p for p in incoming if p.id not in seen]
        self._posts = sorted(self._posts + new, key=lambda p: p.created_at, reverse=True)
        if self._posts:
            self._last_id = max(p.id for p in self._posts)
        self.data = list(self._posts)

    def _show_error(self, msg: str) -> None:
        self._err = msg
        self.query_one(Static).update(f"[red]Error:[/red] {msg}")

    def watch_data(self, posts: list[RelayPost] | None) -> None:
        if posts is None or self._err:
            return
        self.query_one(Static).update(_render_posts(posts, self._show_title))

    # ── SSE listener (real-time push) ──────────────────────────────────────────

    @work(thread=True)
    def _listen(self) -> None:
        if not _BASE_URL or not _TOKEN:
            return  # _load() already surfaced the config error
        retry_delay = 2.0
        while not self._stop.is_set():
            try:
                self._connect_sse()
                retry_delay = 2.0
            except Exception:
                self._stop.wait(retry_delay)
                retry_delay = min(retry_delay * 2, 60.0)

    def _connect_sse(self) -> None:
        if not _BASE_URL:
            raise RuntimeError("Missing required env var: TUIDASH_RELAY_URL")
        if not _TOKEN:
            raise RuntimeError("Missing required env var: TUIDASH_RELAY_TOKEN")
        headers = {
            **_auth(self._last_id),
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }
        with requests.get(
            f"{_BASE_URL}/events",
            params={"tag": self._topic},
            headers=headers,
            stream=True,
            timeout=(10, 30),
        ) as resp:
            self._sse_resp = resp
            resp.raise_for_status()
            event_type: str | None = None
            data_lines: list[str]  = []
            buf                    = b""

            for chunk in resp.iter_content(chunk_size=1024):
                if self._stop.is_set():
                    return
                buf += chunk
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r")

                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                    elif line.startswith("id:"):
                        try:
                            self._last_id = int(line[3:].strip())
                        except ValueError:
                            pass
                    elif line == "":
                        if event_type == "post" and data_lines:
                            try:
                                post = RelayPost.from_dict(
                                    json.loads("\n".join(data_lines))
                                )
                                if not self._stop.is_set():
                                    self.app.call_from_thread(self._on_sse_post, post)
                            except Exception:
                                pass
                        event_type = None
                        data_lines = []
        self._sse_resp = None

    def _on_sse_post(self, post: RelayPost) -> None:
        if any(p.id == post.id for p in self._posts):
            return
        self._posts.insert(0, post)
        self._last_id = max(self._last_id or 0, post.id)
        self.data = list(self._posts)
