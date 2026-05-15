from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static
from textual import work

from .. import config
from ..scroll import SCROLL_INTERVAL, current_tick, scroll_window
from .base import DashWidget


_COLORS = [
    "cyan",
    "yellow",
    "magenta",
    "green",
    "bright_blue",
    "orange1",
    "deep_sky_blue1",
    "light_salmon3",
]

_ATOM_NS  = "http://www.w3.org/2005/Atom"
_MEDIA_NS = "http://search.yahoo.com/mrss/"


@dataclass
class Article:
    title: str
    description: str = ""
    link: str = ""
    pub_date: str = ""
    image_url: str = ""
    image_data: bytes | None = None


@dataclass
class FeedData:
    url: str
    color: str
    source: str = ""
    articles: list[Article] = field(default_factory=list)
    error: str = ""


def _extract_image_url(element: ET.Element) -> str:
    """Return the best image URL from an RSS item or Atom entry element."""
    enc = element.find("enclosure")
    if enc is not None and enc.get("type", "").startswith("image/"):
        url = enc.get("url", "")
        if url:
            return url
    for mc in element.findall(f"{{{_MEDIA_NS}}}content"):
        url = mc.get("url", "")
        if not url:
            continue
        medium = mc.get("medium", "")
        ctype  = mc.get("type", "")
        if medium in ("audio", "video", "document", "executable"):
            continue
        if ctype and not ctype.startswith("image/"):
            continue
        return url
    thumb = element.find(f"{{{_MEDIA_NS}}}thumbnail")
    if thumb is not None:
        url = thumb.get("url", "")
        if url:
            return url
    return ""


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _relative_time(pub_date: str) -> str:
    if not pub_date:
        return ""
    dt: datetime | None = None
    try:
        dt = parsedate_to_datetime(pub_date)
    except Exception:
        pass
    if dt is None:
        try:
            dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
        except Exception:
            return ""
    try:
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 0:
            return ""
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return ""


def _fetch_feed(url: str, color: str) -> FeedData:
    fd = FeedData(url=url, color=color)
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "tuidash/1.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)

        channel = root.find("channel")
        if channel is not None:
            src_el = channel.find("title")
            fd.source = src_el.text.strip() if src_el is not None and src_el.text else url
            for item in channel.findall("item"):
                t_el = item.find("title")
                if t_el is None or not t_el.text:
                    continue
                d_el = item.find("description")
                l_el = item.find("link")
                p_el = item.find("pubDate")
                fd.articles.append(Article(
                    title=t_el.text.strip(),
                    description=_strip_html(d_el.text or "") if d_el is not None else "",
                    link=(l_el.text or "").strip() if l_el is not None else "",
                    pub_date=(p_el.text or "").strip() if p_el is not None else "",
                    image_url=_extract_image_url(item),
                ))
        else:
            tag = lambda name: f"{{{_ATOM_NS}}}{name}"
            src_el = root.find(tag("title")) or root.find("title")
            fd.source = src_el.text.strip() if src_el is not None and src_el.text else url
            for entry in root.findall(tag("entry")) or root.findall("entry"):
                t_el = entry.find(tag("title")) or entry.find("title")
                if t_el is None or not t_el.text:
                    continue
                s_el = entry.find(tag("summary")) or entry.find("summary")
                l_el = entry.find(tag("link"))
                p_el = (entry.find(tag("updated"))
                        or entry.find(tag("published"))
                        or entry.find("published"))
                fd.articles.append(Article(
                    title=t_el.text.strip(),
                    description=_strip_html(s_el.text or "") if s_el is not None else "",
                    link=l_el.get("href", "") if l_el is not None else "",
                    pub_date=(p_el.text or "").strip() if p_el is not None else "",
                    image_url=_extract_image_url(entry),
                ))
    except Exception as exc:
        fd.error = str(exc)
        if not fd.source:
            parts = [p for p in url.split("/") if p and p not in ("http:", "https:")]
            fd.source = parts[0] if parts else url
    return fd



def _render_feeds(feeds: list[FeedData], tick: int = 0, col_text_width: int = 40) -> Table:
    t = Table.grid(expand=True, padding=(0, 2))
    for _ in feeds:
        t.add_column(ratio=1, no_wrap=True)

    # Source name header row
    header_cells: list[Text] = []
    for fd in feeds:
        h = Text()
        h.append(fd.source, style=f"bold {fd.color}")
        if fd.error:
            h.append("  error", style="dim red")
        header_cells.append(h)
    t.add_row(*header_cells)

    # One row per article, scrolling title after the fixed bullet
    max_articles = max((len(fd.articles) for fd in feeds), default=0)
    for i in range(min(max_articles, 20)):
        cells: list[Text] = []
        for j, fd in enumerate(feeds):
            if i < len(fd.articles):
                title  = fd.articles[i].title
                phase  = (j * 37 + i * 13) % 60
                window = scroll_window(title, col_text_width, tick, phase)
                cell   = Text()
                cell.append("• ", style="dim")
                cell.append(window, style=fd.color)
                cells.append(cell)
            else:
                cells.append(Text(""))
        t.add_row(*cells)

    return t


class RssWidget(DashWidget):
    """Multi-source RSS news feed with scrolling article titles."""

    data: reactive[list[FeedData] | None] = reactive(None, always_update=True)

    DEFAULT_CSS = """
    RssWidget { height: 100%; }
    #rss-content { height: 100%; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._feeds: list[tuple[str, str]] = []
        self._data_timer:   Timer | None = None
        self._scroll_timer: Timer | None = None
        self._tick: int = 0
        self._scroll_epoch: int = 0

    def compose(self) -> ComposeResult:
        yield Static("[dim]Loading…[/dim]", id="rss-content")

    def on_mount(self) -> None:
        raw  = config.get("TUIDASH_RSS_FEEDS", "") or ""
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        if not urls:
            self.query_one("#rss-content", Static).update(
                "[dim]No feeds configured — set TUIDASH_RSS_FEEDS[/dim]"
            )
            return
        self._feeds = [(url, _COLORS[i % len(_COLORS)]) for i, url in enumerate(urls)]
        self._load()
        self._scroll_timer = self.set_interval(SCROLL_INTERVAL, self._advance_scroll)

    # ── scroll animation ──────────────────────────────────────────────────────

    def _advance_scroll(self) -> None:
        self._tick = current_tick() - self._scroll_epoch
        if self.data is not None:
            self._redraw()

    def reset_scroll(self) -> None:
        self._scroll_epoch = current_tick()
        self._tick = 0
        if self.data is not None:
            self._redraw()

    def _col_text_width(self) -> int:
        n         = max(1, len(self._feeds))
        content_w = self.content_size.width or 80
        # padding=(0,2) → 2 left + 2 right per cell = 4 chars per column
        col_w = max(10, (content_w - n * 4) // n)
        return max(5, col_w - 2)   # subtract the fixed "• " prefix

    def _redraw(self) -> None:
        if self.data is None:
            return
        self.query_one("#rss-content", Static).update(
            _render_feeds(self.data, self._tick, self._col_text_width())
        )

    # ── data loading ──────────────────────────────────────────────────────────

    def set_refresh_interval(self, seconds: int) -> None:
        if self._data_timer is not None:
            self._data_timer.stop()
        self._data_timer = self.set_interval(float(seconds), self._load)

    @work(thread=True)
    def _load(self) -> None:
        if not self._feeds:
            return
        with ThreadPoolExecutor(max_workers=min(len(self._feeds), 8)) as pool:
            futures = {pool.submit(_fetch_feed, url, color): url for url, color in self._feeds}
            results: list[FeedData] = [f.result() for f in as_completed(futures)]
        order = {url: i for i, (url, _) in enumerate(self._feeds)}
        results.sort(key=lambda fd: order.get(fd.url, 999))
        self.app.call_from_thread(self._show_data, results)

    def _show_data(self, feeds: list[FeedData]) -> None:
        self.data = feeds

    def watch_data(self, feeds: list[FeedData] | None) -> None:
        if feeds is None:
            return
        self._redraw()
