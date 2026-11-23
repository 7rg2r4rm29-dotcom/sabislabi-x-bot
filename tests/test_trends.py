"""Tendances du jour (bot/trends.py) : aucun appel réseau, les sources sont simulées."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from bot import trends as tmod
from bot.trends import (TrendWatch, acceptable, parse_google_trends, parse_hn, parse_traffic,
                        parse_wiki_top, x_most_liked)
from bot.utils import iso, now_local


@pytest.fixture(autouse=True)
def _frozen(clock):
    return clock


def google_xml(*items: tuple[str, str, str]) -> str:
    body = "".join(
        f"<item><title>{term}</title><ht:approx_traffic>{traffic}</ht:approx_traffic>"
        f"<ht:news_item><ht:news_item_title>{news}</ht:news_item_title>"
        "<ht:news_item_url>https://news.example.com/a</ht:news_item_url></ht:news_item></item>"
        for term, traffic, news in items)
    return ('<?xml version="1.0" encoding="UTF-8"?><rss xmlns:ht="https://trends.google.com/trending/rss" '
            f'version="2.0"><channel><title>Daily Search Trends</title>{body}</channel></rss>')


def wiki_json(*articles: tuple[str, int]) -> str:
    return json.dumps({"items": [{"project": "en.wikipedia", "articles": [
        {"article": a, "views": v, "rank": i} for i, (a, v) in enumerate(articles, start=1)]}]})


def hn_json(*hits: tuple[str, int]) -> str:
    return json.dumps({"hits": [{"title": t, "points": p, "num_comments": 3} for t, p in hits]})


GOOGLE_US = google_xml(("gta 6", "500K+", "GTA 6 launch: what players say"), ("thanksgiving", "200000+",
                                                                              "Recipes for the big day"),
                       ("bitcoin price", "100K+", "Bitcoin jumps again"))
GOOGLE_GB = google_xml(("strictly", "50K+", "Strictly Come Dancing results"))
WIKI = wiki_json(("Main_Page", 3_000_000), ("Special:Search", 600_000), ("Grand_Theft_Auto_VI", 308_867),
                 ("Riemann_hypothesis", 205_793), ("Deaths_in_2026", 131_500), (".xxx", 113_918),
                 ("XXX_(2002_film)", 82_069), ("Wicked_(film)", 80_000))
HN = hn_json(("Ladybird is now in alpha", 830), ("Nvidia reinstates its CEO", 1336))


class FakeGet:
    def __init__(self, pages: dict[str, tuple[int, str]] | None = None, fail: bool = False) -> None:
        self.pages = pages if pages is not None else {}
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, accept: str) -> tuple[int, str]:
        self.calls.append((url, accept))
        if self.fail:
            raise ConnectionError("down")
        for key, resp in self.pages.items():
            if key in url:
                return resp
        return 404, ""


def all_sources(day: str = "2026/10/14") -> dict[str, tuple[int, str]]:
    return {"geo=US": (200, GOOGLE_US), "geo=GB": (200, GOOGLE_GB), day: (200, WIKI),
            "hn.algolia.com": (200, HN)}


@pytest.fixture
def get():
    return FakeGet(all_sources())


@pytest.fixture
def tw(settings, state, get):
    return TrendWatch(settings, state, http_get=get)


# ---------------------------------------------------------------------------
# Lecture des sources
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [("200000+", 200_000), ("2K+", 2_000), ("1M+", 1_000_000),
                                          ("1,000+", 1_000), ("500+", 500), ("", 0), ("lots", 0)])
def test_parse_traffic(raw, expected):
    assert parse_traffic(raw) == expected


def test_google_trends_are_sorted_by_searches_with_the_linked_headline():
    rows = parse_google_trends(GOOGLE_US)
    assert [r["term"] for r in rows] == ["gta 6", "thanksgiving", "bitcoin price"]
    assert rows[0] == {"term": "gta 6", "traffic": 500_000, "news": "GTA 6 launch: what players say"}


def test_unreadable_google_feed_gives_nothing():
    assert parse_google_trends("<rss><channel><item>") == []
    assert parse_google_trends("") == []


def test_wikipedia_keeps_only_articles_in_order():
    rows = parse_wiki_top(WIKI)
    assert rows[0] == {"title": "Grand Theft Auto VI", "views": 308_867}
    titles = [r["title"] for r in rows]
    assert "Main_Page" not in titles and "Main Page" not in titles
    assert not any(t.startswith("Special:") for t in titles)
    assert parse_wiki_top("{}") == [] and parse_wiki_top("not json") == []


def test_hacker_news_most_voted_first():
    assert parse_hn(HN) == [{"title": "Nvidia reinstates its CEO", "points": 1336},
                            {"title": "Ladybird is now in alpha", "points": 830}]
    assert parse_hn("[]") == [] and parse_hn("oops") == []


@pytest.mark.parametrize("text,ok", [
    ("Grand Theft Auto VI", True), ("Riemann hypothesis", True),
    ("bitcoin price", False),                 # crypto : jamais
    ("Deaths in 2026", False),                # morts : jamais
    (".xxx", False), ("XXX (2002 film)", False), ("porn ban", False),
    ("Iranian authorities flog singer who performed", False), ("Star arrested after crash", False),
    ("Thanksgiving travel forecast", True), ("", False),
])
def test_editorial_line_applies_to_trends(text, ok):
    assert acceptable(text) is ok


# ---------------------------------------------------------------------------
# Relevé : une fois par heure, une source en panne n'empêche rien
# ---------------------------------------------------------------------------
def test_refresh_reads_every_source_once_per_hour(tw, get, state, clock):
    assert tw.refresh() is True
    urls = [u for u, _a in get.calls]
    assert any(u.endswith("geo=US") for u in urls) and any(u.endswith("geo=GB") for u in urls)
    assert any("2026/10/14" in u for u in urls)          # Wikipédia : la veille (15/10 → 14/10)
    assert any("hn.algolia.com" in u for u in urls)
    assert not any("x.com" in u or "twitter.com" in u for u in urls)
    data = state.data["trends"]
    assert [r["term"] for r in data["google_us"]["rows"]] == ["gta 6", "thanksgiving"]   # sans le bitcoin
    assert [r["title"] for r in data["wiki"]["rows"]] == ["Grand Theft Auto VI", "Riemann hypothesis",
                                                        "Wicked (film)"]
    n = len(get.calls)
    clock(10, 59)
    assert tw.refresh() is False and len(get.calls) == n
    clock(11, 1)
    assert tw.refresh() is True and len(get.calls) > n


def test_wikipedia_falls_back_to_the_day_before(settings, state):
    get = FakeGet({"2026/10/13": (200, WIKI)})           # la veille n'est pas encore publiée
    TrendWatch(settings, state, http_get=get).refresh()
    assert state.data["trends"]["wiki"]["rows"][0]["title"] == "Grand Theft Auto VI"
    assert [u for u, _a in get.calls if "wikimedia" in u][-1].endswith("2026/10/13")


def test_a_failing_source_keeps_its_last_reading_for_a_day(settings, state, clock):
    TrendWatch(settings, state, http_get=FakeGet(all_sources())).refresh()
    clock(12, 0)
    down = TrendWatch(settings, state, http_get=FakeGet(fail=True))
    assert down.refresh() is True
    assert "gta 6" in down.block()                        # dernier relevé, encore frais
    clock(11, 0, day=16)
    down.refresh(force=True)
    assert down.block() == ""                             # plus de 24 h : plus rien


def test_errors_never_escape(settings, state):
    tw = TrendWatch(settings, state, http_get=FakeGet(fail=True))
    assert tw.block() == ""
    assert state.data["trends"]["checked_at"]


def test_disabled_trends_read_nothing(settings, state, get):
    settings.trends_enabled = False
    tw = TrendWatch(settings, state, http_get=get)
    assert tw.refresh() is False and tw.block() == "" and get.calls == []


def test_default_reader_is_the_guarded_one(settings, state):
    # Sans lecteur fourni : sources.default_http_get (bloqué pendant les tests) — l'erreur est avalée.
    assert TrendWatch(settings, state).block() == ""


# ---------------------------------------------------------------------------
# Le bloc donné à la rédaction en chef
# ---------------------------------------------------------------------------
def test_block_for_the_editor(tw, state):
    now = now_local()
    state.data["news_x_cache"] = {
        "1": {"handle": "OpenAI", "text": "Introducing a new voice mode https://t.co/abc", "likes": 12_000,
              "created_at": iso(now - timedelta(hours=3))},
        "2": {"handle": "GeminiApp", "text": "Gemini can now plan your week", "likes": 900,
              "created_at": iso(now - timedelta(hours=5))},
        "3": {"handle": "OpenAI", "text": "An old post", "likes": 99_000,
              "created_at": iso(now - timedelta(hours=60))},                     # trop vieux
        "4": {"handle": "claudeai", "text": "Claude on the election", "likes": 50_000,
              "created_at": iso(now - timedelta(hours=1))},                      # politique : jamais
    }
    b = tw.block()
    assert b.startswith("TRENDING TODAY")
    assert "NEVER a source of facts" in b
    assert "Rising Google searches in the US: gta 6 (500,000+ searches: GTA 6 launch" in b
    assert "Rising Google searches in the UK: strictly" in b
    assert "Grand Theft Auto VI (308,867 views)" in b
    assert "Deaths" not in b and "xxx" not in b.lower() and "bitcoin" not in b.lower()
    assert '"Nvidia reinstates its CEO" (1,336 points)' in b
    assert '@OpenAI "Introducing a new voice mode" (12,000 likes)' in b and "t.co" not in b
    assert "An old post" not in b and "election" not in b
    assert b.index("@OpenAI") < b.index("@GeminiApp")


def test_x_most_liked_reads_only_the_cache(state):
    now = now_local()
    cache = {str(i): {"handle": "OpenAI", "text": f"post {i}", "likes": i * 10,
                      "created_at": iso(now - timedelta(hours=1))} for i in range(1, 9)}
    rows = x_most_liked(cache, now)
    assert [r["text"] for r in rows] == ["post 8", "post 7", "post 6", "post 5", "post 4"]
    assert x_most_liked(None, now) == [] and x_most_liked({"x": "bad"}, now) == []


def test_block_stays_short(tw):
    assert len(tw.block()) < tmod.MAX_GOOGLE * 2 * 120 + 3000
