"""Akışları toplar, tekilleştirir ve konu başına aday listesi üretir."""
from __future__ import annotations

import datetime as dt
import difflib
import re
import time
from dataclasses import dataclass
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup

from .util import canon_url, is_http_url, item_id

UA = "gunun-okumalari/0.1 (+https://github.com/terzioglumtn/gunun_okumalari)"


@dataclass
class Item:
    id: str
    topic: str
    source: str
    kind: str
    url: str
    title: str
    snippet: str
    published: dt.datetime | None


def _text(html: str, limit: int) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    t = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    return t[:limit]


def _read(url: str) -> bytes:
    if url.startswith("file://"):
        return Path(url[7:]).read_bytes()
    r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    return r.content


def fetch_feed(feed: dict) -> list[Item]:
    parsed = feedparser.parse(_read(feed["url"]))
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"akış okunamadı: {parsed.bozo_exception}")
    items: list[Item] = []
    for e in parsed.entries:
        link = e.get("link") or ""
        title = (e.get("title") or "").strip()
        if not title or not is_http_url(link):
            continue
        ts = e.get("published_parsed") or e.get("updated_parsed")
        published = (dt.datetime.fromtimestamp(time.mktime(ts), dt.timezone.utc)
                     if ts else None)
        summary = e.get("summary") or ""
        if not summary and e.get("content"):
            summary = e["content"][0].get("value", "")
        items.append(Item(
            id=item_id(link), topic=feed["topic"], source=feed["name"],
            kind=feed.get("kind", "article"), url=link, title=title,
            snippet=_text(summary, 300), published=published))
    return items


def collect(cfg: dict, now: dt.datetime, seen_urls: set[str]):
    """Konu -> aday listesi ve hata listesini döndürür."""
    run = cfg["run"]
    since = now - dt.timedelta(hours=run["lookback_hours"])
    per_topic: dict[str, list[list[Item]]] = {}
    errors: list[str] = []
    feeds = [f for f in cfg["feeds"] if f.get("enabled", True)]

    for feed in feeds:
        try:
            items = fetch_feed(feed)
        except Exception as exc:  # tek akışın hatası tüm hattı durdurmasın
            errors.append(f'{feed["name"]}: {exc}')
            continue
        fresh = [i for i in items
                 if (i.published is None or i.published >= since)
                 and canon_url(i.url) not in seen_urls]
        fresh.sort(key=lambda i: i.published or now, reverse=True)
        per_topic.setdefault(feed["topic"], []).append(fresh[:run["per_feed_cap"]])

    candidates: dict[str, list[Item]] = {}
    for topic, lists in per_topic.items():
        candidates[topic] = _merge(lists, run["candidates_per_topic"])
    return candidates, errors, len(feeds)


def _merge(lists: list[list[Item]], cap: int) -> list[Item]:
    """Akışları sırayla karıştırır (gürültülü akış diğerlerini ezmesin) ve tekilleştirir."""
    out: list[Item] = []
    urls: set[str] = set()
    titles: list[str] = []
    depth = max((len(l) for l in lists), default=0)
    for i in range(depth):
        for lst in lists:
            if i >= len(lst):
                continue
            item = lst[i]
            cu = canon_url(item.url)
            if cu in urls:
                continue
            lowered = item.title.lower()
            if any(difflib.SequenceMatcher(None, lowered, t).quick_ratio() > 0.92
                   and difflib.SequenceMatcher(None, lowered, t).ratio() > 0.9
                   for t in titles):
                continue
            urls.add(cu)
            titles.append(lowered)
            out.append(item)
            if len(out) >= cap:
                return out
    return out


def fetch_article_text(url: str, fallback: str, limit: int = 6000) -> str:
    """Seçilen madde için sayfa metnini çeker; olmazsa akıştaki özete döner."""
    try:
        html = _read(url).decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = re.sub(r"\s+", " ", " ".join(p for p in paras if len(p) > 40)).strip()
        return text[:limit] if len(text) > 200 else fallback
    except Exception:
        return fallback
