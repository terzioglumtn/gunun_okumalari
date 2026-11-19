"""İki aşamalı hat: (1) başlık/özetlerden eleme, (2) seçilenleri Türkçe özetleme."""
from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from .collect import Item, _section_of, fetch_article_text
from .llm import LLM

SYSTEM = (
    "Sen kıdemli bir yazılım mühendisinin günlük okuma listesini hazırlayan editörsün. "
    "Araç kullanma, dosya okuma, web araması yapma. Yalnızca geçerli JSON döndür, "
    "başka açıklama yazma."
)


REPO_RULES = (
    "- Bu adaylar GitHub Trending haftalık listesindendir. Yazılım mühendisi için gerçek değeri olanı seç: "
    "net bir sorunu çözen, aktif ve olgun, öğrenilecek bir fikri veya tasarımı olan depolar.\n"
    "- 'awesome' listeleri, ders/kurs/mülakat koleksiyonları, kripto/moda ürünleri ve tek dosyalık "
    "oyuncakları düşük puanla.\n"
)


SPARE = 3            # metni yetersiz çıkan maddelerin yerini almak için seçimden ek adaylar
MIN_TEXT = 500       # makale için yeterli sayfa metni (karakter)
MIN_TEXT_SHORT = 200 # depo ve soru-cevap türü için
PAYWALL_MARKERS = ("subscribe to", "subscription", "unlock", "paywall", "sign in to read",
                   "sign in to continue", "become a member", "members only", "abone ol", "üyelik")


def has_full_text(item: Item, text: str) -> bool:
    """Sayfa metni özet için yeterli mi? Kısa metin veya abonelik duvarı belirtisi 'hayır'dır."""
    need = MIN_TEXT_SHORT if item.kind in ("repo", "qa", "medium", "preview") else MIN_TEXT
    if len(text) < need:
        return False
    if len(text) < 1500 and any(m in text.lower() for m in PAYWALL_MARKERS):
        return False
    return True


def fetch_texts(items: list[Item]) -> dict[str, str]:
    """Maddelerin sayfa metinlerini paralel çeker (sıra korunmaz, id ile eşlenir)."""
    if not items:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(items))) as ex:
        texts = list(ex.map(lambda i: fetch_article_text(i.url, i.snippet), items))
    return {i.id: t for i, t in zip(items, texts)}


def keep_with_full_text(items: list[Item], texts: dict[str, str], n: int) -> list[Item]:
    """İlk n yeterli-metinli maddeyi sırayla alır. Hiçbirinin metni yoksa (ağ sorunu olabilir) ilk n'i döndürür."""
    good = [i for i in items if has_full_text(i, texts.get(i.id, ""))]
    skipped = [i for i in items if i not in good]
    if skipped and good:
        print(f"metni yetersiz {len(skipped)} madde atlandı: "
              + "; ".join(f"{i.source}: {i.title[:50]}" for i in skipped), file=sys.stderr)
    return (good or items)[:n]


def select(llm: LLM, topic_cfg: dict, items: list[Item], interests: str,
           n: int, recent: list[str] | None = None, must: bool = False) -> list[dict]:
    """Adaylar arasından en değerli en fazla n maddeyi seçer (must=True: tam n madde zorunlu)."""
    if not items:
        return []
    cands = [{"id": i.id, "kaynak": i.source, "tur": i.kind, "baslik": i.title,
              "ozet": i.snippet} for i in items]
    recent_block = ""
    if recent:
        recent_block = (
            "## Son günlerde ZATEN yayımlanan haberler\n"
            + "\n".join(f"- {t}" for t in recent[:80]) + "\n\n")
    prompt = (
        f"## İlgi profili\n{interests}\n\n"
        f"## Konu\n{topic_cfg['title']}: {topic_cfg['desc']}\n\n"
        f"{recent_block}"
        + (f"## Görev\nAşağıdaki adaylar arasından bugünün EN GÜÇLÜ {n} maddesini seç. Boş liste DÖNDÜRME: "
           "adaylar zayıf olsa bile en iyisini seç ve score'unu dürüstçe ver.\n"
           if must else
           f"## Görev\nAşağıdaki adaylardan okunmaya en değerli olanları seç. EN FAZLA {n} madde seç; "
           "yeterince iyi madde yoksa daha azını seç, boş liste de geçerlidir.\n")
        + "Kurallar:\n"
        + (REPO_RULES if must else "")
        + 
        "- Aynı olayı anlatan birden çok aday varsa yalnızca en birincil kaynağı seç.\n"
        "- 'Zaten yayımlanan haberler' listesindeki bir olayı, farklı bir sitede veya farklı "
        "kelimelerle anlatan adayı SEÇME; yalnızca olayda önemli yeni bir gelişme varsa "
        "(yeni sözleşme, yeni sayı, yeni sonuç) seç ve nedeni açıkla.\n"
        "- Pazarlama, listeler ve başlangıç öğreticilerini ele.\n"
        "- score: 1-5 (5 = bugün mutlaka bilinmeli).\n"
        "- neden: tek kısa Türkçe cümle.\n"
        'Çıktı biçimi: [{"id": "...", "score": 4, "neden": "..."}], score\'a göre azalan.\n\n'
        f"## Adaylar\n{json.dumps(cands, ensure_ascii=False)}"
    )
    picks = llm.json("select", SYSTEM, prompt)
    valid = {i.id for i in items}
    out, seen = [], set()
    for p in picks if isinstance(picks, list) else []:
        pid = p.get("id")
        if pid in valid and pid not in seen:
            seen.add(pid)
            out.append({"id": pid, "score": int(p.get("score", 3)),
                        "neden": str(p.get("neden", ""))})
    return out[:n]


def summarize(llm: LLM, items: list[Item], tags: list[str] | None = None,
              texts: dict[str, str] | None = None) -> dict[str, dict]:
    """Seçilen maddelerin sayfa metnini okuyup Türkçe özetletir."""
    if not items:
        return {}
    payload = []
    for i in items:
        entry = {"id": i.id, "source": i.source, "title": i.title,
                 "text": (texts or {}).get(i.id) or fetch_article_text(i.url, i.snippet)}
        if i.kind in ("medium", "preview"):
            entry["not"] = "Kaynak ön izlemesi (tam metin üyelere özel olabilir): ozet_tr en fazla 2 cümle."
        if tags and _section_of(i):
            entry["bolum_ipucu"] = _section_of(i)   # derleme kaynağındaki bölüm adı (ipucu)
        payload.append(entry)
    tag_rule = tag_fmt = ""
    if tags:
        tag_rule = ("- etiketler: şu listeden içeriğe en uygun 1, en fazla 2 etiket seç; listede olmayan "
                    f"etiket yazma: {json.dumps(tags, ensure_ascii=False)}. Hiçbiri uymuyorsa [\"Diğer\"]. "
                    "'bolum_ipucu' varsa dikkate al ama kararı içeriğe göre ver.\n")
        tag_fmt = ', "etiketler": ["..."]'
    prompt = (
        "## Görev\nHer madde için Türkçe bir özet hazırla.\n"
        "Kurallar:\n"
        "- YALNIZCA verilen metne dayan. Metinde olmayan sayı, tarih, isim ve iddia yazma.\n"
        "- Metin yetersizse bunu özette açıkça belirt ('kaynak metin yetersiz').\n"
        "- Kendi cümlelerinle yaz; kaynak metni kopyalama, alıntı yapma.\n"
        "- baslik_tr: doğal Türkçe başlık. Teknik terimleri ve ürün adlarını çevirme.\n"
        "- ozet_tr: 2-3 cümle, olgu odaklı.\n"
        "- neden_onemli: bir mühendis için neden önemli, tek cümle; abartma.\n"
        f"{tag_rule}"
        'Çıktı biçimi: [{"id": "...", "baslik_tr": "...", "ozet_tr": "...", '
        f'"neden_onemli": "..."{tag_fmt}}}]\n\n'
        f"## Maddeler\n{json.dumps(payload, ensure_ascii=False)}"
    )
    result = llm.json("summarize", SYSTEM, prompt)
    return {r["id"]: r for r in result if isinstance(r, dict) and "id" in r}


def _clean_tags(raw, allowed: list[str] | None) -> list[str]:
    """LLM'in etiketlerini yapılandırmadaki listeye indirger (en fazla 2); geçerli yoksa 'Diğer'."""
    if not allowed:
        return []
    canon = {a.lower(): a for a in allowed}
    got: list[str] = []
    for t in raw if isinstance(raw, list) else []:
        a = canon.get(str(t).strip().lower())
        if a and a not in got:
            got.append(a)
    return got[:2] or ["Diğer"]


def run_topic(llm: LLM, topic_cfg: dict, items: list[Item], interests: str,
              max_qa: int, recent: list[str] | None = None,
              max_medium: int = 3) -> list[dict]:
    """Bir konu için yayınlanacak madde sözlüklerini döndürür.

    topic_cfg["repo_slots"] > 0 ise "repo" türündeki adaylar (GitHub Trending) makalelerle yarışmaz:
    ayrı bir seçimle her gün o kadar depo ZORUNLU seçilir (makale kontenjanına ek olarak).
    """
    n = topic_cfg["max_items"]
    slots = int(topic_cfg.get("repo_slots", 0))
    repos = [i for i in items if i.kind == "repo"] if slots else []
    pool = [i for i in items if i not in repos] if slots else items
    by_id = {i.id: i for i in items}

    picks = select(llm, topic_cfg, pool, interests, n + max_qa + max_medium + SPARE, recent)

    # Soru-cevap türünü sınırla (sıra korunur)
    ordered, used = [], {"qa": 0, "medium": 0}
    limits = {"qa": max_qa, "medium": max_medium, "preview": max_medium}
    for p in picks:
        k = by_id[p["id"]].kind
        if k in limits:
            if used[k] >= limits[k]:
                continue
            used[k] += 1
        ordered.append(p)

    # Metni yetersiz (kısa/abonelik duvarlı) maddeleri ele, yerlerine sıradaki adayı al
    cand = ordered[:n + SPARE]
    texts = fetch_texts([by_id[p["id"]] for p in cand])
    good_ids = {i.id for i in keep_with_full_text([by_id[p["id"]] for p in cand], texts, n)}
    kept = [p for p in cand if p["id"] in good_ids][:n]

    if repos:
        repo_picks = select(llm, topic_cfg, repos, interests, slots + 2, recent, must=True)
        rtexts = fetch_texts([by_id[p["id"]] for p in repo_picks])
        texts.update(rtexts)
        rgood = {i.id for i in keep_with_full_text([by_id[p["id"]] for p in repo_picks], rtexts, slots)}
        repo_picks = [p for p in repo_picks if p["id"] in rgood][:slots]
        if not repo_picks:  # LLM boş döndürdüyse listenin en üst sıradaki deposu (düşük puanla)
            repo_picks = [{"id": repos[0].id, "score": 2,
                           "neden": "Haftalık Trending listesinin en üst sıradaki deposu."}]
        kept += repo_picks

    tags = topic_cfg.get("tags")
    summaries = summarize(llm, [by_id[p["id"]] for p in kept], tags, texts)
    out = []
    for p in kept:
        it = by_id[p["id"]]
        s = summaries.get(it.id, {})
        out.append({
            "id": it.id, "topic": it.topic, "kind": it.kind, "score": p["score"],
            "source": it.source, "via": it.via, "url": it.url, "title_orig": it.title,
            "title_tr": s.get("baslik_tr") or it.title,
            "summary_tr": s.get("ozet_tr") or "Özet üretilemedi; asıl kaynağa bakın.",
            "why_tr": s.get("neden_onemli") or p["neden"],
            "published": it.published.isoformat() if it.published else None,
            "tags": _clean_tags(s.get("etiketler"), tags),
        })
    return out
