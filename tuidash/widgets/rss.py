from __future__ import annotations

import html
import re
import defusedxml.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests


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


def _parse_dt(pub_date: str) -> datetime | None:
    """Parse an RSS/Atom date string into a timezone-aware datetime, or None."""
    if not pub_date:
        return None
    try:
        return parsedate_to_datetime(pub_date)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
    except Exception:
        return None


def _relative_time(pub_date: str) -> str:
    dt = _parse_dt(pub_date)
    if dt is None:
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

