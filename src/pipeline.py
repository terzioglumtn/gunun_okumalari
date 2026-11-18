"""İki aşamalı hat: (1) başlık/özetlerden eleme, (2) seçilenleri Türkçe özetleme."""
from __future__ import annotations

import json

from .collect import Item, fetch_article_text
from .llm import LLM

SYSTEM = (
    "Sen kıdemli bir yazılım mühendisinin günlük okuma listesini hazırlayan editörsün. "
    "Araç kullanma, dosya okuma, web araması yapma. Yalnızca geçerli JSON döndür, "
    "başka açıklama yazma."
)


def select(llm: LLM, topic_cfg: dict, items: list[Item], interests: str,
           n: int) -> list[dict]:
    """Adaylar arasından en değerli en fazla n maddeyi seçer."""
    if not items:
        return []
    cands = [{"id": i.id, "kaynak": i.source, "tur": i.kind, "baslik": i.title,
              "ozet": i.snippet} for i in items]
    prompt = (
        f"## İlgi profili\n{interests}\n\n"
        f"## Konu\n{topic_cfg['title']}: {topic_cfg['desc']}\n\n"
        f"## Görev\nAşağıdaki adaylardan okunmaya en değerli olanları seç. EN FAZLA {n} madde seç; "
        "yeterince iyi madde yoksa daha azını seç, boş liste de geçerlidir.\n"
        "Kurallar:\n"
        "- Aynı olayı anlatan birden çok aday varsa yalnızca en birincil kaynağı seç.\n"
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


def summarize(llm: LLM, items: list[Item]) -> dict[str, dict]:
    """Seçilen maddelerin sayfa metnini okuyup Türkçe özetletir."""
    if not items:
        return {}
    payload = []
    for i in items:
        payload.append({"id": i.id, "source": i.source, "title": i.title,
                        "text": fetch_article_text(i.url, i.snippet)})
    prompt = (
        "## Görev\nHer madde için Türkçe bir özet hazırla.\n"
        "Kurallar:\n"
        "- YALNIZCA verilen metne dayan. Metinde olmayan sayı, tarih, isim ve iddia yazma.\n"
        "- Metin yetersizse bunu özette açıkça belirt ('kaynak metin yetersiz').\n"
        "- Kendi cümlelerinle yaz; kaynak metni kopyalama, alıntı yapma.\n"
        "- baslik_tr: doğal Türkçe başlık. Teknik terimleri ve ürün adlarını çevirme.\n"
        "- ozet_tr: 2-3 cümle, olgu odaklı.\n"
        "- neden_onemli: bir mühendis için neden önemli, tek cümle; abartma.\n"
        'Çıktı biçimi: [{"id": "...", "baslik_tr": "...", "ozet_tr": "...", '
        '"neden_onemli": "..."}]\n\n'
        f"## Maddeler\n{json.dumps(payload, ensure_ascii=False)}"
    )
    result = llm.json("summarize", SYSTEM, prompt)
    return {r["id"]: r for r in result if isinstance(r, dict) and "id" in r}


def run_topic(llm: LLM, topic_cfg: dict, items: list[Item], interests: str,
              max_qa: int) -> list[dict]:
    """Bir konu için yayınlanacak madde sözlüklerini döndürür."""
    n = topic_cfg["max_items"]
    picks = select(llm, topic_cfg, items, interests, n + max_qa)
    by_id = {i.id: i for i in items}

    # Soru-cevap türünü sınırla
    kept, qa = [], 0
    for p in picks:
        if by_id[p["id"]].kind == "qa":
            if qa >= max_qa:
                continue
            qa += 1
        kept.append(p)
    kept = kept[:n]

    summaries = summarize(llm, [by_id[p["id"]] for p in kept])
    out = []
    for p in kept:
        it = by_id[p["id"]]
        s = summaries.get(it.id, {})
        out.append({
            "id": it.id, "topic": it.topic, "kind": it.kind, "score": p["score"],
            "source": it.source, "url": it.url, "title_orig": it.title,
            "title_tr": s.get("baslik_tr") or it.title,
            "summary_tr": s.get("ozet_tr") or "Özet üretilemedi; asıl kaynağa bakın.",
            "why_tr": s.get("neden_onemli") or p["neden"],
            "published": it.published.isoformat() if it.published else None,
        })
    return out
