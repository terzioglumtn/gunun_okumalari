"""data/*.json dosyalarından statik siteyi (HTML, arşiv, Atom, CSS) üretir.

Sayfalar sunucu tarafında önceden çizilir: JavaScript gerekmez, RSS ve önizlemeler çalışır.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from jinja2 import Environment, FileSystemLoader

from .util import safe_url, tr_date, tr_weekday

ROOT = Path(__file__).resolve().parent.parent


def teaser(text: str, limit: int = 160) -> str:
    """Özetin ilk cümlesi (uzunsa kelime sınırında kısaltılır)."""
    text = (text or "").strip()
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    first = m.group(1) if m else text
    if len(first) > limit:
        first = first[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return first


def search_index(issues: list[dict], cfg: dict) -> list[dict]:
    """Arama sayfasının kullandığı düz kayıt listesi (yeniden eskiye)."""
    titles = {k: t["title"] for k, t in cfg["topics"].items()}
    rows = []
    for iss in issues:
        for it in iss["items"]:
            rows.append({"d": iss["date"], "t": it["title_tr"], "o": it.get("title_orig", ""),
                         "s": it["summary_tr"], "u": it["url"], "src": it["source"],
                         "k": it["topic"], "kt": titles.get(it["topic"], it["topic"]),
                         "tags": it.get("tags", []), "sc": it["score"]})
    return rows


def load_issues(data_dir: Path) -> list[dict]:
    issues = [json.loads(p.read_text(encoding="utf-8"))
              for p in sorted(data_dir.glob("20??-??-??.json"))]
    issues.sort(key=lambda i: i["date"], reverse=True)
    return issues


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=True,
                      trim_blocks=True, lstrip_blocks=True)
    env.filters["safe_url"] = safe_url
    env.filters["teaser"] = teaser
    return env


def _local(ts: str, tz: ZoneInfo) -> str:
    d = dt.datetime.fromisoformat(ts).astimezone(tz)
    return f"{tr_date(d.date())}, {d:%H:%M}"


def _view(issue: dict, cfg: dict, tz: ZoneInfo) -> dict:
    """Şablona verilecek, konulara göre gruplanmış görünüm."""
    date = dt.date.fromisoformat(issue["date"])
    by_id = {i["id"]: i for i in issue["items"]}
    repo = next((i for i in sorted(issue["items"], key=lambda i: -i["score"]) if i.get("kind") == "repo"), None)
    top_ids = [i for i in issue.get("top_ids", []) if i in by_id and (not repo or i != repo["id"])]
    want = len(issue.get("top_ids", []))
    for it in sorted(issue["items"], key=lambda i: -i["score"]):   # depo çıkarılınca boşalan yeri doldur
        if len(top_ids) >= want:
            break
        if it["id"] not in top_ids and it is not repo:
            top_ids.append(it["id"])
    top = [by_id[i] for i in top_ids]
    groups = []
    for key, tcfg in cfg["topics"].items():
        items = sorted((i for i in issue["items"] if i["topic"] == key and i is not repo),
                       key=lambda i: -i["score"])
        if items:
            groups.append({"key": key, "title": tcfg["title"], "entries": items})
    return {
        "date": date, "date_tr": tr_date(date), "weekday": tr_weekday(date),
        "issue_no": issue["issue_no"], "generated": _local(issue["generated_at"], tz),
        "top": top, "top_ids": top_ids, "repo": repo, "groups": groups, "count": len(issue["items"]),
        "feed_errors": len(issue.get("stats", {}).get("feed_errors", [])),
        "raw": issue,
    }


def render_all(cfg: dict, data_dir: Path, out_dir: Path) -> None:
    tz = ZoneInfo(cfg["site"]["timezone"])
    env = _env()
    issues = load_issues(data_dir)
    views = [_view(i, cfg, tz) for i in issues]

    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "arsiv").mkdir(parents=True)
    shutil.copy(ROOT / "templates" / "style.css", out_dir / "style.css")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    issue_t = env.get_template("issue.html.j2")
    archive_t = env.get_template("archive.html.j2")
    feed_t = env.get_template("feed.xml.j2")
    search_t = env.get_template("search.html.j2")
    (out_dir / "search.json").write_text(
        json.dumps(search_index(issues, cfg), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out_dir / "arama.html").write_text(search_t.render(site=cfg["site"], base=""), encoding="utf-8")

    for n, v in enumerate(views):
        newer = views[n - 1] if n > 0 else None
        older = views[n + 1] if n + 1 < len(views) else None
        page = issue_t.render(site=cfg["site"], v=v, newer=newer, older=older, base="../")
        (out_dir / "arsiv" / f"{v['date']}.html").write_text(page, encoding="utf-8")
        if n == 0:
            home = issue_t.render(site=cfg["site"], v=v, newer=None, older=older, base="")
            (out_dir / "index.html").write_text(home, encoding="utf-8")

    if not views:  # henüz sayı yoksa boş ana sayfa
        (out_dir / "index.html").write_text(
            archive_t.render(site=cfg["site"], views=[], base=""), encoding="utf-8")

    (out_dir / "arsiv" / "index.html").write_text(
        archive_t.render(site=cfg["site"], views=views, base="../"), encoding="utf-8")
    (out_dir / "feed.xml").write_text(
        feed_t.render(site=cfg["site"], views=views[:20],
                      updated=issues[0]["generated_at"] if issues else dt.datetime.now(dt.timezone.utc).isoformat()),
        encoding="utf-8")


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    render_all(cfg, ROOT / "data", ROOT / "site")
    print("site/ üretildi")


if __name__ == "__main__":
    main()
