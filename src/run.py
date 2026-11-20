"""Günlük sayıyı üretir: topla -> ele -> özetle -> data/YYYY-MM-DD.json -> siteyi çiz."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .collect import collect
from .llm import LLM
from . import pipeline
from .pipeline import run_topic
from .render import render_all
from .util import canon_url

ROOT = Path(__file__).resolve().parent.parent


def load_seen(data_dir: Path, today: dt.date, days: int) -> dict[str, int]:
    """Son N günde yayınlanan bağlantılar -> kaç gün önce (bugünün dosyası hariç: yeniden çalıştırma güvenli)."""
    seen: dict[str, int] = {}
    for p in data_dir.glob("20??-??-??.json"):
        d = dt.date.fromisoformat(p.stem)
        age = (today - d).days
        if d == today or age > days:
            continue
        for item in json.loads(p.read_text(encoding="utf-8")).get("items", []):
            key = canon_url(item["url"])
            seen[key] = min(age, seen.get(key, age))
    return seen


def load_recent_titles(data_dir: Path, today: dt.date, days: int) -> dict[str, list[str]]:
    """Son N günde yayımlanan Türkçe başlıklar (konuya göre); tekrar eden olayları elemek için."""
    out: dict[str, list[str]] = {}
    for p in sorted(data_dir.glob("20??-??-??.json"), reverse=True):
        d = dt.date.fromisoformat(p.stem)
        if d == today or (today - d).days > days:
            continue
        for item in json.loads(p.read_text(encoding="utf-8")).get("items", []):
            title = item.get("title_tr") or item.get("title_orig")
            if title:
                out.setdefault(item.get("topic", ""), []).append(f"{title} ({item.get('source', '')})")
    return out


def next_issue_no(data_dir: Path, today: dt.date) -> int:
    today_file = data_dir / f"{today}.json"
    if today_file.exists():
        return json.loads(today_file.read_text(encoding="utf-8"))["issue_no"]
    nums = [json.loads(p.read_text(encoding="utf-8"))["issue_no"]
            for p in data_dir.glob("20??-??-??.json")]
    return max(nums, default=0) + 1


def feed_key(source: str, via: str | None) -> str:
    """Derleme akışlarında (Morning Dew vb.) madde kaynağı alan adıdır; istatistik akış adına göre tutulur."""
    return via or source


def source_stats(candidates: dict, items: list[dict], scored: dict, skipped: set) -> dict:
    """Akış başına: aday, LLM'in puanladığı, metni yetersiz atlanan, yayımlanan sayısı ve ortalama puan."""
    rows: dict[str, dict] = {}
    for cands in candidates.values():
        for c in cands:
            r = rows.setdefault(feed_key(c.source, c.via),
                                {"candidates": 0, "scored": 0, "skipped_text": 0, "published": 0, "_sum": 0})
            r["candidates"] += 1
            if c.id in scored:
                r["scored"] += 1
                r["_sum"] += scored[c.id]
            if c.id in skipped:
                r["skipped_text"] += 1
    for it in items:
        r = rows.setdefault(feed_key(it["source"], it.get("via")),
                            {"candidates": 0, "scored": 0, "skipped_text": 0, "published": 0, "_sum": 0})
        r["published"] += 1
    for r in rows.values():
        s = r.pop("_sum")
        r["avg_score"] = round(s / r["scored"], 2) if r["scored"] else None
    return dict(sorted(rows.items()))


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    interests = (ROOT / "config" / "interests.md").read_text(encoding="utf-8")
    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)

    now = dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(ZoneInfo(cfg["site"]["timezone"])).date()
    run = cfg["run"]

    candidates, errors, n_feeds = collect(cfg, now, load_seen(data_dir, today, run.get("seen_days_max", 30)))
    for e in errors:
        print(f"UYARI okunamayan akış: {e}", file=sys.stderr)
    if n_feeds and len(errors) / n_feeds > run["max_feed_failure_ratio"]:
        print("Akışların çoğu okunamadı; sayı üretilmedi.", file=sys.stderr)
        return 2

    backend = os.environ.get("LLM_BACKEND") or cfg["llm"]["backend"]
    llm = LLM(backend, cfg["llm"]["select_model"], cfg["llm"]["summarize_model"])

    pipeline.SCORED.clear()
    pipeline.SKIPPED.clear()
    recent = load_recent_titles(data_dir, today, run["seen_days"])
    items: list[dict] = []
    for key, tcfg in cfg["topics"].items():
        items += run_topic(llm, tcfg, candidates.get(key, []), interests, run["max_qa"],
                           recent.get(key, []), run.get("max_medium", 3))

    # Aynı haber iki konuda çıkarsa yalnızca en yüksek puanlı kopya kalır.
    best: dict[str, dict] = {}
    for it in items:
        if it["id"] not in best or it["score"] > best[it["id"]]["score"]:
            best[it["id"]] = it
    items = [it for it in items if best[it["id"]] is it]

    ranked = sorted(items, key=lambda i: -i["score"])
    top_ids = [i["id"] for i in ranked if i["score"] >= 4][:run["top_n"]]

    issue = {
        "date": str(today), "issue_no": next_issue_no(data_dir, today),
        "generated_at": now.isoformat(), "backend": backend,
        "items": items, "top_ids": top_ids, "topics": list(cfg["topics"]),
        "stats": {"feeds": n_feeds, "feed_errors": errors,
                  "candidates": {k: len(v) for k, v in candidates.items()},
                  "selected": len(items),
                  "sources": source_stats(candidates, items, pipeline.SCORED, pipeline.SKIPPED)},
    }
    (data_dir / f"{today}.json").write_text(
        json.dumps(issue, ensure_ascii=False, indent=1), encoding="utf-8")
    render_all(cfg, data_dir, ROOT / "site")
    print(f"Sayı {issue['issue_no']} ({today}): {len(items)} madde, "
          f"{len(errors)} akış hatası, backend={backend}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
