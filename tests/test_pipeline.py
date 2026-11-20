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


TRENDING = Path(__file__).parent / "fixtures" / "trending.html"


def trending_cfg(path, **extra):
    cfg = make_cfg()
    cfg["feeds"] = [{"name": "GitHub Trending", "topic": "dotnet", "html": "trending",
                     "url": f"file://{path}", **extra}]
    return cfg


def test_trending_parses_repos_and_ignores_foreign_links():
    cands, errors, _ = collect(trending_cfg(TRENDING), NOW, set())
    assert errors == []
    items = cands["dotnet"]
    assert [i.title for i in items] == ["acme/fast-parser", "someone/agent-kit", "org/no-desc"]
    first = items[0]
    assert first.url == "https://github.com/acme/fast-parser" and first.kind == "repo"
    assert "Dil: C#." in first.snippet and "Bugün 1,234 yıldız" in first.snippet
    assert "very fast incremental parser" in first.snippet


def test_trending_excludes_repos_already_published_and_reports_layout_change(tmp_path):
    from src.util import canon_url
    seen = {canon_url("https://github.com/acme/fast-parser")}
    cands, _, _ = collect(trending_cfg(TRENDING), NOW, seen)
    assert "acme/fast-parser" not in [i.title for i in cands["dotnet"]]
    bad = tmp_path / "t.html"
    bad.write_text("<html><body>Sign in to GitHub</body></html>", encoding="utf-8")
    _, errors, _ = collect(trending_cfg(bad), NOW, set())
    assert len(errors) == 1 and "ayrıştırılamadı" in errors[0]


def test_per_feed_seen_days_keeps_repos_out_for_longer():
    from src.util import canon_url
    url = "https://github.com/acme/fast-parser"
    seen = {canon_url(url): 12}          # 12 gün önce yayımlandı
    cfg = trending_cfg(TRENDING, seen_days=30)
    cands, _, _ = collect(cfg, NOW, seen)
    assert "acme/fast-parser" not in [i.title for i in cands["dotnet"]]
    cfg2 = trending_cfg(TRENDING)          # varsayılan 7 gün: 12 gün önceki tekrar aday olabilir
    cfg2["run"]["seen_days"] = 7
    cands2, _, _ = collect(cfg2, NOW, seen)
    assert "acme/fast-parser" in [i.title for i in cands2["dotnet"]]


def test_trending_snippet_uses_period_label():
    html = TRENDING.read_text(encoding="utf-8").replace("stars today", "stars this week")
    items = pipeline_parse(html)
    assert "Bu hafta 1,234 yıldız" in items[0].snippet


def pipeline_parse(html):
    from src.collect import parse_trending
    return parse_trending({"name": "GH", "topic": "dotnet"}, html)


def test_repo_slot_always_yields_exactly_one_repo(monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_article_text", lambda url, fb, limit=6000: fb)
    prompts = []

    class EmptyLLM:  # hiçbir şey seçmeyen LLM: zorunlu depo yine de çıkmalı
        def json(self, stage, system, prompt):
            prompts.append(prompt)
            return []

    mk = lambda n, kind="repo": pipeline.Item(
        id=f"i{n}", topic="dotnet", source="GitHub Trending" if kind == "repo" else "Blog", kind=kind,
        url=f"https://github.com/o/r{n}", title=f"o/r{n}", snippet="d", published=None)
    items = [mk(1), mk(2), mk(3), mk(9, "article")]
    out = pipeline.run_topic(EmptyLLM(), {"title": "T", "desc": "D", "max_items": 7, "repo_slots": 1},
                             items, "ilgi", 3)
    assert [i["kind"] for i in out] == ["repo"] and out[0]["id"] == "i1"
    select_prompts = [p for p in prompts if "## Adaylar" in p]
    assert len(select_prompts) == 2
    assert "Boş liste DÖNDÜRME" in select_prompts[1] and '"tur": "repo"' in select_prompts[1]
    assert '"tur": "repo"' not in select_prompts[0]   # depolar makale yarışına girmez


def test_repo_slot_adds_on_top_of_article_quota(monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_article_text", lambda url, fb, limit=6000: fb)
    out = pipeline.run_topic(LLM("mock", "a", "b"),
                             {"title": "T", "desc": "D", "max_items": 2, "repo_slots": 1},
                             [pipeline.Item(id=f"0000000{n}", topic="dotnet", source="B", kind="article",
                                            url=f"https://x.example/{n}-long", title=f"t{n}",
                                            snippet="s", published=None) for n in range(4)]
                             + [pipeline.Item(id="000000ff", topic="dotnet", source="GH", kind="repo",
                                              url="https://github.com/o/r1", title="o/r1",
                                              snippet="d", published=None)],
                             "ilgi", 3)
    kinds = sorted(i["kind"] for i in out)
    assert kinds == ["article", "article", "repo"]



def test_summary_prompt_asks_for_tags_and_output_is_normalized(monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_article_text", lambda url, fb, limit=6000: fb)
    seen = {}

    class TagLLM:
        def json(self, stage, system, prompt):
            if stage == "select":
                return [{"id": "0000000a", "score": 4, "neden": "x"}]
            seen["prompt"] = prompt
            return [{"id": "0000000a", "baslik_tr": "b", "ozet_tr": "o", "neden_onemli": "n",
                     "etiketler": ["python", "ai", "Uydurma", "Web and Cloud Development"]}]

    it = pipeline.Item(id="0000000a", topic="dotnet", source="Blog", kind="article",
                       url="https://x.example/a-long", title="t", snippet="Bölüm: Python. Yazar: A",
                       published=None, via="Morning Dew")
    cfg = {"title": "T", "desc": "D", "max_items": 5, "tags": ["Python", "AI", "Web and Cloud Development"]}
    out = pipeline.run_topic(TagLLM(), cfg, [it], "ilgi", 3)
    assert out[0]["tags"] == ["Python", "AI"]                 # kanonik yazım, en fazla 2, uydurma elenir
    assert '"etiketler"' in seen["prompt"] and '"bolum_ipucu": "Python"' in seen["prompt"]
    assert pipeline._clean_tags(["yok"], cfg["tags"]) == ["Diğer"]
    assert pipeline._clean_tags(["AI"], None) == []


def test_tags_render_on_page(tmp_path):
    from src.render import render_all
    import json as _json
    data = tmp_path / "data"; data.mkdir()
    (data / "2026-11-19.json").write_text(_json.dumps({
        "date": "2026-11-19", "issue_no": 1, "generated_at": "2026-11-19T03:30:00+00:00",
        "feed_errors": [], "feeds_total": 1, "top_ids": [],
        "items": [{"id": "0000000a", "topic": "dotnet", "kind": "article", "score": 4,
                   "source": "Blog", "via": "", "url": "https://x.example/a", "title_orig": "t",
                   "title_tr": "başlık", "summary_tr": "özet", "why_tr": "neden", "published": None,
                   "tags": ["Python", "AI"]}]}), encoding="utf-8")
    cfg = make_cfg()
    cfg["topics"] = {"dotnet": {"title": "Yazılım", "desc": "d", "max_items": 5}}
    out = tmp_path / "site"
    render_all(cfg, data, out)
    html = (out / "index.html").read_text(encoding="utf-8")
    assert '<span class="tag">Python</span>' in html and '<span class="tag">AI</span>' in html


def test_has_full_text_and_filter():
    from src.pipeline import has_full_text, keep_with_full_text
    from src.collect import Item
    def mk(i, kind="article"):
        return Item(id=i, topic="t", source="s", kind=kind, url="https://x/"+i, title=i, snippet="", published=NOW, via=None)
    a, b, c = mk("aaaaaaa1"), mk("aaaaaaa2"), mk("aaaaaaa3")
    texts = {a.id: "Bu kaynağı açmak için subscribe to " + "x" * 600, b.id: "kısa", c.id: "y" * 800}
    assert not has_full_text(a, texts[a.id]) and not has_full_text(b, texts[b.id]) and has_full_text(c, texts[c.id])
    assert keep_with_full_text([a, b, c], texts, 2) == [c]
    assert keep_with_full_text([a, b], texts, 2) == [a, b]  # hepsi yetersizse düşmez


def test_site_has_search_and_compact(tmp_path):
    import json, yaml
    from pathlib import Path
    from src.render import render_all, teaser, ROOT
    assert teaser("Birinci cümle. İkinci cümle.") == "Birinci cümle."
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    d = tmp_path / "data"; d.mkdir()
    items = [{"id": f"abcdef0{i}", "topic": "dotnet", "kind": "article", "score": 5 - i, "source": "S", "via": None,
              "url": "https://e.com/%d" % i, "title_orig": "T", "title_tr": f"Başlık {i}",
              "summary_tr": "Ilk cümle. Devamı.", "why_tr": "n", "published": "2026-11-19T00:00:00+00:00", "tags": ["AI"]} for i in range(5)]
    (d / "2026-11-19.json").write_text(json.dumps({"date": "2026-11-19", "issue_no": 1, "generated_at": "2026-11-19T03:00:00+00:00",
                                                    "top_ids": ["abcdef00"], "items": items, "stats": {}}), encoding="utf-8")
    out = tmp_path / "site"; render_all(cfg, d, out)
    html = (out / "index.html").read_text(encoding="utf-8")
    assert '<span class="count">(5)</span>' in html
    assert html.count('<details class="item compact"') == 4 and "Ilk cümle." in html
    rows = json.loads((out / "search.json").read_text(encoding="utf-8"))
    assert len(rows) == 5 and rows[0]["tags"] == ["AI"]
    assert "search.json" in (out / "arama.html").read_text(encoding="utf-8")


def test_select_drops_low_scores_but_not_for_must():
    from src.pipeline import select
    from src.collect import Item
    class L:
        def json(self, *_a): return [{"id": "aaaaaaa1", "score": 4, "neden": "x"}, {"id": "aaaaaaa2", "score": 2, "neden": "y"}]
    its = [Item(id=f"aaaaaaa{k}", topic="t", source="s", kind="article", url=f"https://x/{k}", title="t", snippet="", published=NOW, via=None) for k in (1, 2)]
    cfg = {"title": "T", "desc": "d"}
    assert [p["id"] for p in select(L(), cfg, its, "", 5)] == ["aaaaaaa1"]
    assert len(select(L(), cfg, its, "", 5, must=True)) == 2


def test_repo_box_is_separate_and_top_stays_full(tmp_path):
    import json, yaml
    from src.render import render_all, ROOT
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    d = tmp_path / "data"; d.mkdir()
    mk = lambda i, kind, sc: {"id": f"abcdef0{i}", "topic": "dotnet", "kind": kind, "score": sc, "source": "S", "via": None,
                              "url": f"https://e.com/{i}", "title_orig": "T", "title_tr": f"Başlık {i}", "summary_tr": "Ilk. Ikinci.",
                              "why_tr": "n", "published": "2026-11-19T00:00:00+00:00", "tags": []}
    items = [mk(0, "repo", 5)] + [mk(i, "article", 5 - i % 3) for i in range(1, 6)]
    (d / "2026-11-19.json").write_text(json.dumps({"date": "2026-11-19", "issue_no": 1, "generated_at": "2026-11-19T03:00:00+00:00",
                                                    "top_ids": ["abcdef00", "abcdef01", "abcdef02"], "items": items, "stats": {}}), encoding="utf-8")
    out = tmp_path / "site"; render_all(cfg, d, out)
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "Bugünün deposu" in html and html.count("Başlık 0") == 1
    assert html.split("Önce bunlar")[1].split("</section>")[0].count("<article") == 3
    assert '<span class="count">(5)</span>' in html


def test_parse_kume_routes_by_title_and_type():
    from pathlib import Path
    from src.collect import parse_kume
    feed = {"name": "Küme Vakfı", "topic": "teknoloji", "routes": [
        {"match": "^Toplum ve Teknoloji", "topic": "teknoloji"}, {"match": "^Gri Bölge", "topic": "jeopolitik"},
        {"type": "Rapor", "topic": "jeopolitik"}, {"type": "Blog", "topic": "teknoloji"},
        {"topic": None}]}
    html = (Path(__file__).parent / "fixtures" / "kume.html").read_text(encoding="utf-8")
    items = parse_kume(feed, html)
    got = {i.title.split(":")[0]: i.topic for i in items}
    assert got == {"Otomatiklik, Karar ve Siyasal Olan": "teknoloji", "Toplum ve Teknoloji #87": "teknoloji",
                   "Orta Doğu'da Çift Kullanım Sorunsalı": "jeopolitik", "Gri Bölge #57": "jeopolitik"}
    assert items[1].url == "https://kumevakfi.org/toplum-ve-teknoloji-86-2"
    assert items[0].published.day == 16 and items[0].published.month == 11
