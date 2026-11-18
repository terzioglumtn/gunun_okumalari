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
from .pipeline import run_topic
from .render import render_all
from .util import canon_url

ROOT = Path(__file__).resolve().parent.parent


def load_seen(data_dir: Path, today: dt.date, days: int) -> set[str]:
    """Son N günde yayınlanan bağlantılar (bugünün dosyası hariç: yeniden çalıştırma güvenli)."""
    seen: set[str] = set()
    for p in data_dir.glob("20??-??-??.json"):
        d = dt.date.fromisoformat(p.stem)
        if d == today or (today - d).days > days:
            continue
        for item in json.loads(p.read_text(encoding="utf-8")).get("items", []):
            seen.add(canon_url(item["url"]))
    return seen


def next_issue_no(data_dir: Path, today: dt.date) -> int:
    today_file = data_dir / f"{today}.json"
    if today_file.exists():
        return json.loads(today_file.read_text(encoding="utf-8"))["issue_no"]
    nums = [json.loads(p.read_text(encoding="utf-8"))["issue_no"]
            for p in data_dir.glob("20??-??-??.json")]
    return max(nums, default=0) + 1


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    interests = (ROOT / "config" / "interests.md").read_text(encoding="utf-8")
    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)

    now = dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(ZoneInfo(cfg["site"]["timezone"])).date()
    run = cfg["run"]

    candidates, errors, n_feeds = collect(cfg, now, load_seen(data_dir, today, run["seen_days"]))
    for e in errors:
        print(f"UYARI okunamayan akış: {e}", file=sys.stderr)
    if n_feeds and len(errors) / n_feeds > run["max_feed_failure_ratio"]:
        print("Akışların çoğu okunamadı; sayı üretilmedi.", file=sys.stderr)
        return 2

    backend = os.environ.get("LLM_BACKEND") or cfg["llm"]["backend"]
    llm = LLM(backend, cfg["llm"]["select_model"], cfg["llm"]["summarize_model"])

    items: list[dict] = []
    for key, tcfg in cfg["topics"].items():
        items += run_topic(llm, tcfg, candidates.get(key, []), interests, run["max_qa"])

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
        "items": items, "top_ids": top_ids,
        "stats": {"feeds": n_feeds, "feed_errors": errors,
                  "candidates": {k: len(v) for k, v in candidates.items()},
                  "selected": len(items)},
    }
    (data_dir / f"{today}.json").write_text(
        json.dumps(issue, ensure_ascii=False, indent=1), encoding="utf-8")
    render_all(cfg, data_dir, ROOT / "site")
    print(f"Sayı {issue['issue_no']} ({today}): {len(items)} madde, "
          f"{len(errors)} akış hatası, backend={backend}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
