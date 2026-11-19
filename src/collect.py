"""Akışları toplar, tekilleştirir ve konu başına aday listesi üretir."""
from __future__ import annotations

import calendar
import datetime as dt
import difflib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import feedparser
import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from .util import canon_url, is_http_url, item_id

UA = "gunun-okumalari/0.1 (+https://github.com/terzioglumtn/gunun_okumalari)"

# Derleme (roundup) yazılarında asıl makale olmayan, gürültü bağlantı alanları.
DEFAULT_SKIP_DOMAINS = {
    "twitter.com", "x.com", "facebook.com", "linkedin.com", "instagram.com",
    "feedburner.com", "feeds.feedburner.com", "t.co", "bit.ly", "mailto",
}
MIN_LINK_TEXT = 15  # "here", "read more" gibi kısa bağlantı metinlerini ele


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
    via: str = ""   # derleme kaynağı (ör. "Morning Dew"); boşsa doğrudan kaynak


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


def _published(entry) -> dt.datetime | None:
    ts = entry.get("published_parsed") or entry.get("updated_parsed")
    # feedparser UTC döndürür; mktime yerel saat varsayar, timegm doğru dönüşümdür.
    return dt.datetime.fromtimestamp(calendar.timegm(ts), dt.timezone.utc) if ts else None


def _entry_html(entry) -> str:
    if entry.get("content"):
        return entry["content"][0].get("value", "")
    return entry.get("summary") or ""


def _domain(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _skipped(url: str, skip: set[str]) -> bool:
    d = _domain(url)
    return any(d == s or d.endswith("." + s) for s in skip)


def _is_heading(tag: Tag) -> bool:
    """Başlık etiketi ya da yalnızca kalın metin içeren, bağlantısız paragraf."""
    if tag.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        return True
    if tag.name in ("p", "div") and not tag.find("a"):
        bold = tag.find(["strong", "b"])
        text = tag.get_text(" ", strip=True)
        return bool(bold) and bold.get_text(" ", strip=True) == text and len(text) < 80
    return False


def expand_roundup(feed: dict, entry, post_published) -> list[Item]:
    """Bir derleme yazısındaki (ör. Dew Drop) dış bağlantıları ayrı aday maddelere çevirir.

    Derlemenin kendisi aday OLMAZ; içindeki asıl makaleler olur. Bağlantı metni başlık,
    en yakın üst başlık bölüm, bağlantıyı izleyen '(Yazar)' ise yazar olarak alınır.
    """
    skip = DEFAULT_SKIP_DOMAINS | set(feed.get("skip_domains", []))
    if entry.get("link"):
        skip.add(_domain(entry["link"]))  # derlemenin kendi sitesine giden bağlantılar aday olmaz
    soup = BeautifulSoup(_entry_html(entry), "html.parser")
    via = feed.get("via") or feed["name"]
    skip_sections = [x.lower() for x in feed.get("skip_sections", [])]
    only_sections = {x.lower() for x in feed.get("only_sections", [])}  # tam eşleşme; verilirse yalnızca bunlar
    section = ""
    skipping = bool(only_sections)  # ilk bölüm başlığına kadar olan giriş metni, izin listesinde sayılmaz
    seen: set[str] = set()
    out: list[Item] = []
    for node in soup.descendants:
        if not isinstance(node, Tag):
            continue
        if _is_heading(node):
            section = node.get_text(" ", strip=True)
            if only_sections:
                skipping = section.lower() not in only_sections
            else:
                skipping = any(x in section.lower() for x in skip_sections)
            continue
        if node.name != "a" or skipping:
            continue
        href = (node.get("href") or "").strip()
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
        if (not is_http_url(href) or _skipped(href, skip) or len(text) < MIN_LINK_TEXT
                or canon_url(href) in seen):
            continue
        seen.add(canon_url(href))
        author = ""
        nxt = node.next_sibling
        if isinstance(nxt, NavigableString):
            m = re.match(r"\s*\(([^)]{2,60})\)", str(nxt))
            author = m.group(1).strip() if m else ""
        parts = [f"Bölüm: {section}" if section else "", f"Yazar: {author}" if author else ""]
        out.append(Item(
            id=item_id(href), topic=feed["topic"], source=_domain(href),
            kind=feed.get("kind", "article"), url=href, title=text,
            snippet=". ".join(p for p in parts if p), published=post_published, via=via))
    return out


def _section_of(item: Item) -> str:
    m = re.match(r"Bölüm: (.*?)(?:\. Yazar: .*)?$", item.snippet)
    return m.group(1) if m else ""


def _section_counts(items: list[Item]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for i in items:
        counts[_section_of(i)] = counts.get(_section_of(i), 0) + 1
    return counts


def spread_by_section(items: list[Item]) -> list[Item]:
    """Bölümleri sırayla karıştırır; cap uygulanınca sondaki bölümler (ör. .NET, Web) kesilmesin."""
    groups: dict[str, list[Item]] = {}
    for i in items:
        groups.setdefault(_section_of(i), []).append(i)
    out: list[Item] = []
    for k in range(max((len(g) for g in groups.values()), default=0)):
        out += [g[k] for g in groups.values() if k < len(g)]
    return out


def parse_trending(feed: dict, html: str) -> list[Item]:
    """github.com/trending sayfasından depo adaylarını çıkarır (resmî RSS/API yok)."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[Item] = []
    for row in soup.select("article.Box-row"):
        a = row.select_one("h2 a[href]")
        href = (a.get("href") or "").strip() if a else ""
        if not re.fullmatch(r"/[\w.-]+/[\w.-]+", href):
            continue
        name = re.sub(r"\s+", "", a.get_text()) or href.strip("/")
        desc_tag = row.select_one("p")
        desc = re.sub(r"\s+", " ", desc_tag.get_text(" ", strip=True)) if desc_tag else ""
        lang_tag = row.select_one('[itemprop="programmingLanguage"]')
        lang = lang_tag.get_text(strip=True) if lang_tag else ""
        row_text = re.sub(r"\s+", " ", row.get_text(" ", strip=True))
        m = re.search(r"([\d,.]+)\s+stars?\s+(today|this week|this month)", row_text)
        period = {"today": "Bugün", "this week": "Bu hafta", "this month": "Bu ay"}
        today = f"{period[m.group(2)]} {m.group(1)} yıldız. " if m else ""
        parts = [f"Depo: {desc}." if desc else "Depo (açıklama yok).",
                 f"Dil: {lang}." if lang else "", today]
        out.append(Item(
            id=item_id("https://github.com" + href), topic=feed["topic"], source=feed["name"],
            kind=feed.get("kind", "repo"), url="https://github.com" + href, title=name,
            snippet=" ".join(x for x in parts if x).strip(), published=None))
    print(f'trending "{feed["name"]}": {len(out)} depo', file=sys.stderr)
    if len(out) < feed.get("min_links", 3):
        raise ValueError(
            f"trending sayfası ayrıştırılamadı ({len(out)} depo, sayfa {len(html)} karakter); "
            "sayfa yapısı değişmiş olabilir")
    return out


def fetch_feed(feed: dict) -> list[Item]:
    if feed.get("html") == "trending":
        return parse_trending(feed, _read(feed["url"]).decode("utf-8", errors="replace"))
    parsed = feedparser.parse(_read(feed["url"]))
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"akış okunamadı: {parsed.bozo_exception}")
    items: list[Item] = []
    for e in parsed.entries:
        published = _published(e)
        if feed.get("roundup"):
            # Yalnızca zaman penceresine giren yazıları aç (gereksiz ayrıştırma olmasın).
            if feed.get("_since") and published and published < feed["_since"]:
                continue
            found = expand_roundup(feed, e, published)
            print(f'roundup "{feed["name"]}": {len(found)} bağlantı, '
                  f'bölümler: {_section_counts(found)}',
                  file=sys.stderr)
            if len(found) < feed.get("min_links", 5):
                raise ValueError(
                    f"derleme ayrıştırılamadı ({len(found)} bağlantı, içerik "
                    f"{len(_entry_html(e))} karakter); akışın içerik yapısını kontrol edin")
            items += found
            continue
        link = e.get("link") or ""
        title = (e.get("title") or "").strip()
        if not title or not is_http_url(link):
            continue
        items.append(Item(
            id=item_id(link), topic=feed["topic"], source=feed["name"],
            kind=feed.get("kind", "article"), url=link, title=title,
            snippet=_text(_entry_html(e), 300), published=published))
    return items


def _is_seen(url: str, seen, days: int) -> bool:
    """seen: {kanonik url: kaç gün önce yayımlandı} sözlüğü (veya eski kullanım: url kümesi)."""
    key = canon_url(url)
    if isinstance(seen, dict):
        return key in seen and seen[key] <= days
    return key in seen


def collect(cfg: dict, now: dt.datetime, seen_urls):
    """Konu -> aday listesi ve hata listesini döndürür."""
    run = cfg["run"]
    since = now - dt.timedelta(hours=run["lookback_hours"])
    per_topic: dict[str, list[list[Item]]] = {}
    errors: list[str] = []
    feeds = [f for f in cfg["feeds"] if f.get("enabled", True)]

    for feed in feeds:
        try:
            items = fetch_feed({**feed, "_since": since})
        except Exception as exc:  # tek akışın hatası tüm hattı durdurmasın
            errors.append(f'{feed["name"]}: {exc}')
            continue
        days = feed.get("seen_days", run["seen_days"])
        fresh = [i for i in items
                 if (i.published is None or i.published >= since)
                 and not _is_seen(i.url, seen_urls, days)]
        fresh.sort(key=lambda i: i.published or now, reverse=True)
        if feed.get("roundup"):
            fresh = spread_by_section(fresh)
        cap = feed.get("cap", run["per_feed_cap"])  # derleme akışları için daha yüksek olabilir
        per_topic.setdefault(feed["topic"], []).append(fresh[:cap])

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
