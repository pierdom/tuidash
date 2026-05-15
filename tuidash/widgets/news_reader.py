from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

import requests
from rich.console import Group
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import ScrollableContainer
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static
from textual import work

from .. import config
from .base import DashWidget
from .rss import _COLORS, _fetch_feed, _parse_dt, _relative_time, FeedData, Article


_THUMB_COLS = 20   # characters wide
_THUMB_ROWS = 10   # character rows → 20 pixels tall


def _render_image(data: bytes) -> Text | None:
    try:
        from PIL import Image
        img = Image.open(BytesIO(data)).convert("RGB")
        img = img.resize((_THUMB_COLS, _THUMB_ROWS * 2), Image.LANCZOS)
        pixels = img.load()
        t = Text()
        for row in range(_THUMB_ROWS):
            for col in range(_THUMB_COLS):
                tr, tg, tb = pixels[col, row * 2]
                br, bg, bb = pixels[col, row * 2 + 1]
                t.append("▀", style=f"rgb({tr},{tg},{tb}) on rgb({br},{bg},{bb})")
            if row < _THUMB_ROWS - 1:
                t.append("\n")
        return t
    except Exception:
        return None


def _article_renderable(fd: FeedData, art: Article):
    header = Text()
    header.append("● ", style=fd.color)
    header.append(fd.source.upper(), style=f"bold {fd.color}")
    age = _relative_time(art.pub_date)
    if age:
        header.append(f"  {age}", style="dim")

    desc_text = Text()
    if art.description:
        raw = art.description[:300]
        if len(art.description) > 300:
            raw += "…"
        desc_text = Text(raw, style="dim")

    img = _render_image(art.image_data) if art.image_data else None

    if img is not None:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=_THUMB_COLS, no_wrap=True)
        grid.add_column(ratio=1)
        right = Text()
        right.append_text(header)
        right.append("\n")
        right.append(art.title, style="bold")
        if art.description:
            right.append("\n")
            right.append_text(desc_text)
        grid.add_row(img, right)
        return grid

    # text-only fallback
    t = Text()
    t.append_text(header)
    t.append("\n")
    t.append(art.title, style="bold")
    if art.description:
        t.append("\n")
        t.append_text(desc_text)
    return t


_DT_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _render_articles(feeds: list[FeedData]) -> Group:
    if not any(fd.articles for fd in feeds) and not any(fd.error for fd in feeds):
        return Group(Text("No articles — configure TUIDASH_RSS_FEEDS", style="dim"))

    parts: list = []

    # Flatten all articles and sort newest-first; undated articles go last.
    all_articles: list[tuple[FeedData, Article]] = [
        (fd, art) for fd in feeds for art in fd.articles
    ]
    all_articles.sort(
        key=lambda fa: _parse_dt(fa[1].pub_date) or _DT_EPOCH,
        reverse=True,
    )
    # Render in pairs — 2-column grid per row.
    for i in range(0, len(all_articles), 2):
        row = Table.grid(expand=True, padding=(0, 2))
        row.add_column(ratio=1)
        row.add_column(ratio=1)
        left_fd,  left_art  = all_articles[i]
        if i + 1 < len(all_articles):
            right_fd, right_art = all_articles[i + 1]
            row.add_row(_article_renderable(left_fd, left_art),
                        _article_renderable(right_fd, right_art))
        else:
            row.add_row(_article_renderable(left_fd, left_art), Text(""))
        parts.append(row)
        parts.append(Rule(style="dim"))

    # Status line for feeds that returned no articles or errored.
    for fd in feeds:
        if not fd.articles:
            line = Text()
            line.append(f"● {fd.source.upper()}", style=f"bold {fd.color}")
            line.append(f"  {fd.error}" if fd.error else "  no articles",
                        style="dim red" if fd.error else "dim")
            parts.append(line)
            parts.append(Rule(style="dim"))

    return Group(*parts)


def _download_images(articles: list[Article]) -> None:
    def _fetch(art: Article) -> None:
        try:
            r = requests.get(art.image_url, timeout=8, headers={"User-Agent": "tuidash/1.0"})
            if r.ok and r.content:
                art.image_data = r.content
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_fetch, articles))


class NewsReaderWidget(DashWidget):
    """Full-screen scrollable RSS reader with article descriptions and thumbnails."""

    data: reactive[list[FeedData] | None] = reactive(None, always_update=True)

    DEFAULT_CSS = """
    NewsReaderWidget {
        height: 100%;
    }
    #news-reader-scroll {
        height: 100%;
    }
    #news-reader-body {
        height: auto;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._feeds: list[tuple[str, str]] = []
        self._data_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="news-reader-scroll"):
            yield Static("[dim]Loading…[/dim]", id="news-reader-body")

    def on_mount(self) -> None:
        raw = config.get("TUIDASH_RSS_FEEDS", "") or ""
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        if not urls:
            self.query_one("#news-reader-body", Static).update(
                "[dim]No feeds configured — set TUIDASH_RSS_FEEDS[/dim]"
            )
            return
        self._feeds = [(url, _COLORS[i % len(_COLORS)]) for i, url in enumerate(urls)]
        self._load()

    def set_refresh_interval(self, seconds: int) -> None:
        if self._data_timer is not None:
            self._data_timer.stop()
        self._data_timer = self.set_interval(float(seconds), self._load)

    @work(thread=True)
    def _load(self) -> None:
        if not self._feeds:
            return

        # Phase 1: fetch all feed text
        with ThreadPoolExecutor(max_workers=min(len(self._feeds), 8)) as pool:
            futures = {pool.submit(_fetch_feed, url, color): url for url, color in self._feeds}
            results: list[FeedData] = [f.result() for f in as_completed(futures)]
        order = {url: i for i, (url, _) in enumerate(self._feeds)}
        results.sort(key=lambda fd: order.get(fd.url, 999))

        self.app.call_from_thread(self._show_data, results)

        # Phase 2: download images and re-render
        articles_with_images = [
            art for fd in results for art in fd.articles if art.image_url
        ]
        if articles_with_images:
            _download_images(articles_with_images)
            self.app.call_from_thread(self._show_data, results)

    def _show_data(self, feeds: list[FeedData]) -> None:
        self.data = feeds

    def watch_data(self, feeds: list[FeedData] | None) -> None:
        if feeds is None:
            return
        self.query_one("#news-reader-body", Static).update(_render_articles(feeds))
        n_articles = sum(len(fd.articles) for fd in feeds)
        n_sources = sum(1 for fd in feeds if not fd.error)
        self.border_subtitle = f"{n_sources} feeds · {n_articles} articles"
