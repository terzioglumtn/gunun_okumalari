"""Küçük yardımcılar: URL, tarih ve JSON ayıklama."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MONTHS_TR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
             "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
DAYS_TR = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]

_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|ref_src$)")


def canon_url(url: str) -> str:
    """İzleme parametrelerini ve parçayı atarak adresi tekilleştirmeye uygun hale getirir."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not _TRACKING.match(k)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def item_id(url: str) -> str:
    return hashlib.sha1(canon_url(url).encode("utf-8")).hexdigest()[:8]


def is_http_url(url: str) -> bool:
    """Yalnızca http(s) adreslerine izin verir (javascript:, data: vb. engellenir)."""
    try:
        return urlsplit(url).scheme in ("http", "https")
    except ValueError:
        return False


def safe_url(url: str) -> str:
    return url if is_http_url(url) else "#"


def tr_date(d: dt.date) -> str:
    return f"{d.day} {MONTHS_TR[d.month - 1]} {d.year}"


def tr_weekday(d: dt.date) -> str:
    return DAYS_TR[d.weekday()]


def extract_json(text: str):
    """LLM çıktısından JSON ayıklar (kod çiti veya öncesi/sonrası metin olsa da)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # İlk '[' veya '{' ile eşleşen son kapanışı dene.
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("Yanıttan geçerli JSON ayıklanamadı")
