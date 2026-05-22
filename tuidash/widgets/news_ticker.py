from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static
from textual import work

from .. import config
from .base import DashWidget
from .rss import _COLORS, FeedData, _fetch_feed, _parse_dt


_TICKER_INTERVAL = 0.125   # seconds per step (≈8 chars/sec)
_SEP             = "   ◆︎   "
_MAX_AGE_HOURS   = 6
_VS15            = "︎"  # variation selector 15 — zero display width


def _disp_len(s: str) -> int:
    return len(s.replace(_VS15, ""))


def _disp_slice(s: str, start: int, end: int) -> str:
    result: list[str] = []
    col = 0
    for ch in s:
        if ch == _VS15:
            if result:
                result.append(ch)
        else:
            if col >= end:
                break
            if col >= start:
                result.append(ch)
            col += 1
    return "".join(result)


def _is_recent(pub_date: str) -> bool:
    dt = _parse_dt(pub_date)
    if dt is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_MAX_AGE_HOURS)
    return dt >= cutoff


def _render_ticker(feeds: list[FeedData], tick: int, width: int) -> Text:
    segments: list[tuple[str, str]] = []

    for fd in feeds:
        for article in fd.articles:
            if not _is_recent(article.pub_date):
                continue
            segments.append((_SEP, "dim"))
            segments.append((fd.source, f"bold {fd.color}"))
            segments.append(("  ", ""))
            segments.append((article.title, fd.color))

    if not segments:
        t = Text()
        t.append(" No news in the last 6 hours", style="dim")
        return t

    segments.append((_SEP, "dim"))   # trailing sep → seamless loop

    full_len = sum(_disp_len(s) for s, _ in segments)
    if full_len <= width:
        t = Text()
        for seg, style in segments:
            t.append(seg, style=style)
        return t

    offset   = tick % full_len
    t        = Text()
    char_pos = 0
    for seg, style in (segments + segments):   # doubled for wrap-around
        seg_end = char_pos + _disp_len(seg)
        vis_s   = max(offset, char_pos)
        vis_e   = min(offset + width, seg_end)
        if vis_s < vis_e:
            t.append(_disp_slice(seg, vis_s - char_pos, vis_e - char_pos), style=style)
        char_pos = seg_end
        if char_pos >= offset + width:
            break

    return t


class NewsTickerWidget(DashWidget):
    """Single-row continuous news ticker — RSS headlines from the last 6 hours."""

    data: reactive[list[FeedData] | None] = reactive(None, always_update=True)

    DEFAULT_CSS = """
    NewsTickerWidget { height: 3; }
    #news-ticker     { height: 1; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._feeds: list[tuple[str, str]] = []
        self._data_timer:   Timer | None = None
        self._ticker_timer: Timer | None = None
        self._tick: int = 0

    def compose(self) -> ComposeResult:
        yield Static("", id="news-ticker")

    def on_mount(self) -> None:
        raw  = config.get("TUIDASH_RSS_FEEDS", "") or ""
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        if not urls:
            self.query_one("#news-ticker", Static).update(
                "[dim]No feeds configured — set TUIDASH_RSS_FEEDS[/dim]"
            )
            return
        self._feeds = [(url, _COLORS[i % len(_COLORS)]) for i, url in enumerate(urls)]
        self._load()
        self._ticker_timer = self.set_interval(_TICKER_INTERVAL, self._advance_ticker)

    # ── scroll ─────────────────────────────────────────────────────────────────

    def _ticker_width(self) -> int:
        return max(20, self.content_size.width or 80)

    def _advance_ticker(self) -> None:
        self._tick += 1
        if self.data is not None:
            self._redraw()

    def _redraw(self) -> None:
        if self.data is None:
            return
        t = _render_ticker(self.data, self._tick, self._ticker_width())
        self.query_one("#news-ticker", Static).update(t)

    # ── data ───────────────────────────────────────────────────────────────────

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

    def pause_animations(self) -> None:
        if self._ticker_timer is not None:
            self._ticker_timer.pause()

    def resume_animations(self) -> None:
        if self._ticker_timer is not None:
            self._ticker_timer.resume()
