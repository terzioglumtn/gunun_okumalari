import datetime as dt
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from src import pipeline
from src.collect import collect
from src.llm import LLM
from src.render import render_all
from src.util import canon_url, extract_json, safe_url

FIXTURE = Path(__file__).parent / "fixtures" / "sample.xml"
NOW = dt.datetime(2026, 11, 18, 5, 0, tzinfo=dt.timezone.utc)


def make_cfg():
    return {
        "site": {"title": "Test", "subtitle": "Alt", "timezone": "Europe/Istanbul",
                 "base_url": "https://example.github.io/x/"},
        "run": {"lookback_hours": 48, "seen_days": 7, "per_feed_cap": 20,
                "candidates_per_topic": 80, "top_n": 3, "max_qa": 3,
                "max_feed_failure_ratio": 0.5},
        "topics": {"dotnet": {"title": ".NET", "desc": "test", "max_items": 7}},
        "feeds": [{"name": "Örnek", "topic": "dotnet", "url": f"file://{FIXTURE}"}],
    }


def test_url_helpers():
    assert canon_url("https://Example.com/a/?utm_source=x&id=1#f") == "https://example.com/a?id=1"
    assert safe_url("javascript:alert(1)") == "#"
    assert safe_url("data:text/html,x") == "#"
    assert safe_url("https://example.com/x") == "https://example.com/x"


def test_extract_json_variants():
    assert extract_json('[{"a": 1}]') == [{"a": 1}]
    assert extract_json('Tabii:\n```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert extract_json('Sonuç: {"a": 2} bitti') == {"a": 2}
    with pytest.raises(ValueError):
        extract_json("json yok")


def test_collect_filters_and_dedupes():
    cands, errors, n = collect(make_cfg(), NOW, seen_urls=set())
    titles = [i.title for i in cands["dotnet"]]
    assert errors == [] and n == 1
    assert titles.count("Avalonia 12 yayınlandı") == 1      # tekilleştirildi
    assert "Eski haber" not in titles                       # zaman penceresi dışı
    assert "Kötü bağlantı" not in titles                    # javascript: bağlantısı atıldı
    assert len(titles) == 3


def test_collect_excludes_seen():
    seen = {canon_url("https://example.com/wpf")}
    cands, _, _ = collect(make_cfg(), NOW, seen_urls=seen)
    assert all("wpf" not in i.url for i in cands["dotnet"])


def test_collect_reports_feed_errors():
    cfg = make_cfg()
    cfg["feeds"].append({"name": "Yok", "topic": "dotnet", "url": "file:///yok/yok.xml"})
    _, errors, n = collect(cfg, NOW, set())
    assert n == 2 and len(errors) == 1 and errors[0].startswith("Yok")


def test_pipeline_mock_and_render(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_article_text", lambda url, fb, limit=6000: fb)
    cfg = make_cfg()
    cands, _, _ = collect(cfg, NOW, set())
    llm = LLM("mock", "m1", "m2")
    items = pipeline.run_topic(llm, cfg["topics"]["dotnet"], cands["dotnet"], "ilgi", 3)
    assert items and all(i["title_tr"].startswith("[TR] ") for i in items)

    issue = {"date": "2026-11-18", "issue_no": 1,
             "generated_at": NOW.isoformat(), "backend": "mock",
             "items": items, "top_ids": [i["id"] for i in items][:3],
             "stats": {"feeds": 1, "feed_errors": [], "candidates": {}, "selected": len(items)}}
    # Güvenlik: kötü adres ve HTML'li başlık olan sahte madde ekle
    issue["items"].append({**items[0], "id": "deadbeef", "url": "javascript:alert(1)",
                           "title_tr": "<script>alert(1)</script>", "title_orig": "orijinal"})
    data = tmp_path / "data"
    data.mkdir()
    (data / "2026-11-18.json").write_text(json.dumps(issue, ensure_ascii=False), encoding="utf-8")

    out = tmp_path / "site"
    render_all(cfg, data, out)

    home = (out / "index.html").read_text(encoding="utf-8")
    assert "18 Kasım 2026" in home and "Sayı 1" in home
    assert "<script>alert(1)</script>" not in home           # kaçışlandı
    assert "&lt;script&gt;" in home
    assert 'href="javascript:' not in home                   # kötü bağlantı engellendi
    assert (out / "arsiv" / "2026-11-18.html").exists()
    assert "2026-11-18" in (out / "arsiv" / "index.html").read_text(encoding="utf-8")
    ET.fromstring((out / "feed.xml").read_text(encoding="utf-8"))  # geçerli XML
    assert (out / ".nojekyll").exists() and (out / "style.css").exists()


def test_render_without_issues(tmp_path):
    cfg = make_cfg()
    data = tmp_path / "data"
    data.mkdir()
    render_all(cfg, data, tmp_path / "site")
    assert "Henüz yayınlanmış sayı yok" in (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    ET.fromstring((tmp_path / "site" / "feed.xml").read_text(encoding="utf-8"))


def test_run_dedupes_items_across_topics(tmp_path, monkeypatch):
    """Aynı haber iki konuda gelirse yayında tek kez ve 'Önce bunlar'da tekrarsız olmalı."""
    import shutil
    import yaml
    from src import run as run_mod, render as render_mod

    root = tmp_path / "repo"
    shutil.copytree(Path(__file__).parent.parent, root,
                    ignore=shutil.ignore_patterns(".git", "data", "site", "__pycache__", ".pytest_cache"))
    cfg = make_cfg()
    cfg["run"]["lookback_hours"] = 24 * 400
    cfg["llm"] = {"backend": "mock", "select_model": "m", "summarize_model": "m"}
    cfg["topics"]["savunma"] = {"title": "Savunma", "desc": "test", "max_items": 5}
    cfg["feeds"] = [{"name": "A", "topic": "dotnet", "url": f"file://{FIXTURE}"},
                    {"name": "B", "topic": "savunma", "url": f"file://{FIXTURE}"}]
    (root / "config" / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(run_mod, "ROOT", root)
    monkeypatch.setattr(render_mod, "ROOT", root)
    monkeypatch.setattr(pipeline, "fetch_article_text", lambda url, fb, limit=6000: fb)

    assert run_mod.main() == 0
    issue = json.loads(next((root / "data").glob("20*.json")).read_text(encoding="utf-8"))
    ids = [i["id"] for i in issue["items"]]
    assert len(ids) == len(set(ids))
    assert len(issue["top_ids"]) == len(set(issue["top_ids"]))


ROUNDUP = Path(__file__).parent / "fixtures" / "roundup.xml"
ROUNDUP_EMPTY = Path(__file__).parent / "fixtures" / "roundup_empty.xml"


def roundup_cfg(path):
    cfg = make_cfg()
    cfg["feeds"] = [{"name": "Morning Dew", "topic": "dotnet", "roundup": True, "cap": 80,
                     "url": f"file://{path}", "skip_domains": ["alvinashcraft.example"]}]
    return cfg


def test_roundup_expands_links_and_drops_the_roundup_itself():
    cands, errors, _ = collect(roundup_cfg(ROUNDUP), NOW, set())
    assert errors == []
    items = cands["dotnet"]
    urls = [i.url for i in items]
    titles = [i.title for i in items]
    # Derlemenin kendisi ve kendi sitesine giden bağlantılar aday olmamalı
    assert not any("dew-drop" in u for u in urls)
    # Sosyal medya, çok kısa metin ve javascript: bağlantıları elenmeli
    assert not any("twitter.com" in u for u in urls)
    assert "here" not in titles
    assert not any(u.startswith("javascript:") for u in urls)
    # Asıl makaleler kaldı ve kaynak olarak alan adı, derleme adı "via" olarak işlendi
    by_url = {i.url: i for i in items}
    first = by_url["https://devblogs.microsoft.com/dotnet/avalonia-ve-dotnet-11?utm_source=dew"]
    assert first.source == "devblogs.microsoft.com" and first.via == "Morning Dew"
    assert "Bölüm: Top Links" in first.snippet and "Yazar: Jane Doe" in first.snippet
    # Kalın paragraf ve h3 bölüm başlığı olarak tanındı
    grpc = by_url["https://example.org/realtime-grpc-streaming"]
    assert "Bölüm: Web & Cloud Development" in grpc.snippet and "Yazar: Sam Sample" in grpc.snippet
    gh = by_url["https://github.com/example/tool"]
    assert "Bölüm: Source Code" in gh.snippet
    assert len(items) == 5


def test_roundup_unparseable_is_reported_not_silent():
    _, errors, _ = collect(roundup_cfg(ROUNDUP_EMPTY), NOW, set())
    assert len(errors) == 1 and "derleme ayrıştırılamadı" in errors[0]


def test_roundup_item_carries_via_into_issue(monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_article_text", lambda url, fb, limit=6000: fb)
    cfg = roundup_cfg(ROUNDUP)
    cands, _, _ = collect(cfg, NOW, set())
    items = pipeline.run_topic(LLM("mock", "a", "b"), cfg["topics"]["dotnet"], cands["dotnet"], "ilgi", 3)
    assert items and all(i["via"] == "Morning Dew" for i in items)


def test_roundup_skip_sections_drops_noise_sections():
    cfg = roundup_cfg(ROUNDUP)
    cfg["feeds"][0]["skip_sections"] = ["Source Code"]
    cfg["feeds"][0]["min_links"] = 1
    cands, _, _ = collect(cfg, NOW, set())
    assert not any("github.com/example/tool" in i.url for i in cands["dotnet"])
    assert len(cands["dotnet"]) == 4


def test_roundup_cap_keeps_every_section_represented():
    cfg = roundup_cfg(ROUNDUP)
    cfg["feeds"][0]["cap"] = 3
    cands, _, _ = collect(cfg, NOW, set())
    sections = {i.snippet.split(".")[0] for i in cands["dotnet"]}
    # cap 3 olsa da son bölümler (Web & Cloud, Source Code) ilk bölüm tarafından ezilmez
    assert len(sections) == 3


def test_select_prompt_lists_recently_published_titles_to_avoid_repeats():
    captured = {}

    class FakeLLM:
        def json(self, stage, system, prompt):
            captured["prompt"] = prompt
            return []

    items = [pipeline.Item(id="a", topic="savunma", source="uasvision.com", kind="article",
                           url="https://uasvision.com/x-long-enough", title="Aselsan neutralizes 51 drones",
                           snippet="", published=None)]
    pipeline.select(FakeLLM(), {"title": "T", "desc": "D"}, items, "ilgi", 3,
                    recent=["ASELSAN Ejderha HPEM sistemi (euro-sd.com)"])
    p = captured["prompt"]
    assert "ZATEN yayımlanan" in p and "ASELSAN Ejderha HPEM sistemi (euro-sd.com)" in p
    # liste yoksa bölüm hiç eklenmez
    pipeline.select(FakeLLM(), {"title": "T", "desc": "D"}, items, "ilgi", 3)
    assert "ZATEN yayımlanan" not in captured["prompt"]


def test_roundup_only_sections_is_an_exact_allowlist():
    cfg = roundup_cfg(ROUNDUP)
    cfg["feeds"][0]["only_sections"] = ["Web & Cloud Development", "source code"]
    cfg["feeds"][0]["min_links"] = 1
    cands, _, _ = collect(cfg, NOW, set())
    urls = {i.url for i in cands["dotnet"]}
    assert urls == {"https://example.org/realtime-grpc-streaming", "https://example.net/wpf-memory",
                    "https://github.com/example/tool"}
