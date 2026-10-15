"""Tests de bot/sources.py : flux RSS/Atom, Hacker News, comptes officiels X
(API seulement, via FakeX), filtres, dédoublonnage, score, caches. Aucun réseau."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from html import escape

import pytest

from bot import sources
from bot.models import NewsItem
from bot.sources import NewsHub, hard_block_hit, parse_hn, parse_rss, top_for
from bot.utils import now_local
from bot.x_api import XError
from tests.fakes import FakeX, make_tweet

# L'horloge du fixture `clock` : jeudi 15/10/2026 10:00 à Paris = 08:00 UTC.
NOW_UTC = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _frozen(clock):
    clock(10, 0)
    yield


def rfc822(hours_ago: float) -> str:
    return (NOW_UTC - timedelta(hours=hours_ago)).strftime("%a, %d %b %Y %H:%M:%S +0000")


def iso_utc(hours_ago: float) -> str:
    return (NOW_UTC - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def rss_feed(*entries: dict, title: str = "Feed") -> str:
    items = []
    for e in entries:
        items.append(
            "<item>"
            f"<title>{escape(e['title'])}</title>"
            f"<link>{escape(e['link'])}</link>"
            + (f"<pubDate>{e['date']}</pubDate>" if e.get("date") else "")
            + f"<description>{escape(e.get('desc', ''))}</description>"
            "</item>"
        )
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            f"<title>{escape(title)}</title><link>https://example.com/</link><description>d</description>"
            + "".join(items) + "</channel></rss>")


def entry(title: str, link: str, hours_ago: float | None = 1, desc: str = "") -> dict:
    return {"title": title, "link": link, "date": rfc822(hours_ago) if hours_ago is not None else None,
            "desc": desc}


class FakeHttp:
    """http_get injectable : {url: (status, text) | Exception | callable}."""

    def __init__(self, routes: dict | None = None) -> None:
        self.routes = dict(routes or {})
        self.calls: list[str] = []

    def __call__(self, url: str):
        self.calls.append(url)
        r = self.routes.get(url)
        if isinstance(r, BaseException):
            raise r
        if callable(r):
            return r(url)
        if r is None:
            return 404, ""
        return r


def feed(name: str, url: str, weight: float = 1, official: bool = False, type_: str | None = None) -> dict:
    f = {"name": name, "url": url, "weight": weight, "official": official}
    if type_:
        f["type"] = type_
    return f


def hub_with(settings, state, feeds_and_bodies: list[tuple[dict, object]], x=None, use_accounts=False):
    settings.news_feeds = [f for f, _ in feeds_and_bodies]
    if not use_accounts:
        settings.x_news_accounts = []
    http = FakeHttp({f["url"]: body for f, body in feeds_and_bodies})
    return NewsHub(settings, state, x=x, http_get=http), http


# ---------------------------------------------------------------------------
# Échantillons réalistes
# ---------------------------------------------------------------------------
LONG_PARAGRAPH = ("Teams can now attach shell scripts that run before and after every tool call, "
                  "which makes it possible to block edits to protected folders, run the linter "
                  "automatically, or log every command for review. ") * 6

RSS_SAMPLE = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>TechCrunch AI</title>
  <atom:link href="https://techcrunch.com/category/artificial-intelligence/feed/" rel="self" type="application/rss+xml"/>
  <link>https://techcrunch.com/category/artificial-intelligence/</link>
  <description>Artificial intelligence news</description>
  <lastBuildDate>{rfc822(1)}</lastBuildDate>
  <item>
    <title>Anthropic&#8217;s Claude Code gets hooks for teams</title>
    <link>https://techcrunch.com/2026/10/15/claude-code-hooks/?utm_source=rss&amp;utm_medium=rss</link>
    <dc:creator><![CDATA[Jane Doe]]></dc:creator>
    <pubDate>{rfc822(2)}</pubDate>
    <category><![CDATA[AI]]></category>
    <guid isPermaLink="false">https://techcrunch.com/?p=2900001</guid>
    <description><![CDATA[<p>Read more&#8230;</p>]]></description>
    <content:encoded><![CDATA[<p>{LONG_PARAGRAPH}</p><script>alert(1)</script>]]></content:encoded>
  </item>
  <item>
    <title>Startup raises money to build AI bookkeeping agents</title>
    <link>https://techcrunch.com/2026/10/14/ai-bookkeeping-agents/</link>
    <pubDate>{rfc822(12)}</pubDate>
    <description><![CDATA[<p>The company says its agents close the books for small shops in a day instead of a week, and an accountant reviews every entry.</p>
<p>The post <a rel="nofollow" href="https://techcrunch.com/2026/10/14/ai-bookkeeping-agents/">Startup raises money to build AI bookkeeping agents</a> appeared first on <a rel="nofollow" href="https://techcrunch.com">TechCrunch</a>.</p>]]></description>
  </item>
</channel>
</rss>"""

ATOM_SAMPLE = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="en-us">
  <title>Simon Willison's Weblog</title>
  <link href="https://simonwillison.net/" rel="alternate"/>
  <link href="https://simonwillison.net/atom/everything/" rel="self"/>
  <id>https://simonwillison.net/</id>
  <updated>{iso_utc(0.5)}</updated>
  <author><name>Simon Willison</name></author>
  <entry>
    <title>Trying the new Gemini Flash image model</title>
    <link href="/2026/Oct/15/gemini-flash-image/" rel="alternate"/>
    <published>{iso_utc(0.5)}</published>
    <updated>{iso_utc(0.25)}</updated>
    <id>https://simonwillison.net/2026/Oct/15/gemini-flash-image/</id>
    <summary type="html">&lt;p&gt;I ran my usual &lt;strong&gt;pelican on a bicycle&lt;/strong&gt; test &amp;amp; it did well.&lt;/p&gt;</summary>
  </entry>
  <entry>
    <title>Quoting a release note</title>
    <link href="https://simonwillison.net/2026/Oct/14/quote/" rel="alternate"/>
    <updated>{iso_utc(5)}</updated>
    <id>https://simonwillison.net/2026/Oct/14/quote/</id>
    <content type="html">&lt;blockquote&gt;&lt;p&gt;We made the model faster.&lt;/p&gt;&lt;/blockquote&gt;</content>
  </entry>
</feed>"""


def hn_json() -> str:
    def ts(hours_ago: float) -> int:
        return int((NOW_UTC - timedelta(hours=hours_ago)).timestamp())
    hits = [
        {"title": "Show HN: An open-source LLM router that picks the cheapest model",
         "url": "https://github.com/example/llm-router", "points": 312, "num_comments": 88,
         "created_at_i": ts(4), "created_at": iso_utc(4), "objectID": "41000001"},
        {"title": "The history of the bicycle bell", "url": "https://example.org/bells",
         "points": 900, "num_comments": 200, "created_at_i": ts(3), "objectID": "41000002"},
        {"title": "We're releasing Jev 1.14 with features for dreamers", "url": "https://typesafe.ai/blog/jev",
         "points": 261, "num_comments": 85, "created_at_i": ts(3), "objectID": "41000005"},
        {"title": "Notes from our infra migration", "url": "https://openai.com/index/infra-notes/",
         "points": 40, "num_comments": 5, "created_at_i": ts(3), "objectID": "41000006"},
        {"title": "Ask HN: How do you use Claude for code review?", "url": None, "points": 120,
         "num_comments": 140, "created_at_i": ts(6), "objectID": "41000003"},
        {"title": "Nvidia unveils a new inference chip", "url": "https://nvidianews.nvidia.com/news/chip",
         "points": "57", "num_comments": None, "created_at": iso_utc(2), "objectID": "41000004"},
        "not-a-dict",
    ]
    return json.dumps({"hits": hits, "nbHits": 5, "page": 0})


# ---------------------------------------------------------------------------
# Lecture des flux
# ---------------------------------------------------------------------------
def test_rss_parsing_cleans_text_and_dates():
    items = parse_rss(RSS_SAMPLE, feed("TechCrunch AI", "https://techcrunch.com/feed/", 2))
    assert len(items) == 2
    a, b = items
    assert a.title == "Anthropic’s Claude Code gets hooks for teams"
    assert a.url.startswith("https://techcrunch.com/2026/10/15/claude-code-hooks/")
    assert a.source == "TechCrunch AI" and a.kind == "rss" and a.official is False
    # résumé « Read more… » trop court -> contenu complet, sans HTML ni script, ≤ 600
    assert "Teams can now attach shell scripts" in a.summary
    assert "<" not in a.summary and "alert" not in a.summary
    assert len(a.summary) <= 600 and a.summary.endswith("…")
    # date RSS (RFC 822) -> datetime aware, heure locale
    assert a.published is not None and a.published.tzinfo is not None
    assert a.published == NOW_UTC - timedelta(hours=2)
    assert a.published.utcoffset() == timedelta(hours=2)  # Paris en octobre
    # boilerplate WordPress retiré
    assert "appeared first on" not in b.summary
    assert b.summary.startswith("The company says its agents close the books")


def test_atom_parsing_resolves_relative_links_and_updated_dates():
    items = parse_rss(ATOM_SAMPLE, feed("Simon Willison", "https://simonwillison.net/atom/everything/"))
    assert [i.title for i in items] == ["Trying the new Gemini Flash image model", "Quoting a release note"]
    first, second = items
    assert first.url == "https://simonwillison.net/2026/Oct/15/gemini-flash-image/"
    assert first.published == NOW_UTC - timedelta(minutes=30)          # <published> d'abord
    assert first.summary == "I ran my usual pelican on a bicycle test & it did well."
    assert second.published == NOW_UTC - timedelta(hours=5)             # sinon <updated>
    assert second.summary == "We made the model faster."


def test_declared_latin1_encoding_is_not_garbled_and_never_fetched(monkeypatch):
    import feedparser.api

    fetched: list = []
    monkeypatch.setattr(feedparser.api.http, "get", lambda *a, **k: fetched.append(a) or b"")
    xml = ('<?xml version="1.0" encoding="ISO-8859-1"?><rss version="2.0"><channel><title>T</title>'
           f'<item><title>Café déjà vu with Claude</title><link>https://ex.com/a</link>'
           f'<pubDate>{rfc822(1)}</pubDate></item></channel></rss>')
    items = parse_rss(xml, feed("Ex", "https://ex.com/feed"))
    assert items[0].title == "Café déjà vu with Claude"
    # un corps de réponse qui ressemble à une URL ou un chemin n'est JAMAIS suivi
    for body in ("https://evil.example/feed.xml", "file:///etc/hostname", "/etc/hostname"):
        with pytest.raises(ValueError):
            parse_rss(body, feed("Ex", "https://ex.com/feed"))
    assert fetched == []


def test_hn_keeps_only_ai_tech_stories():
    items = parse_hn(hn_json(), feed("Hacker News", "https://hn.algolia.com/api/v1/search?tags=front_page",
                                     2, type_="hn"))
    titles = [i.title for i in items]
    assert "The history of the bicycle bell" not in titles
    assert not any("Jev" in t for t in titles)              # « .ai » seul ne suffit pas
    assert len(items) == 4
    router, infra, ask, chip = items
    assert infra.url == "https://openai.com/index/infra-notes/"  # le domaine compte
    assert router.kind == "hn" and router.engagement == 312 and router.source == "Hacker News"
    assert router.published == NOW_UTC - timedelta(hours=4)
    assert "312 points" in router.summary
    assert ask.url == "https://news.ycombinator.com/item?id=41000003"
    assert chip.engagement == 57 and chip.published == NOW_UTC - timedelta(hours=2)  # created_at ISO


def test_hn_feed_goes_through_collect(settings, state):
    hn = feed("Hacker News", "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=40", 2, type_="hn")
    hub, http = hub_with(settings, state, [(hn, (200, hn_json()))])
    items = hub.collect()
    assert {i.kind for i in items} == {"hn"}
    assert len(items) == 4
    assert http.calls == [hn["url"]]


# ---------------------------------------------------------------------------
# Comptes officiels X (API officielle seulement)
# ---------------------------------------------------------------------------
def make_x() -> FakeX:
    x = FakeX()
    x.ids_map = {"openai": "100", "claudeai": "200"}
    x.user_posts_map = {
        "100": [make_tweet("9001", "Introducing GPT-6 for everyone &amp; every plan\n\n"
                                   "Now rolling out in ChatGPT https://t.co/abc123",
                           author_id="100", created_at=iso_utc(1), likes=5000)],
        "200": [make_tweet("9101", "Claude can now build and edit spreadsheets for you. " + "x" * 200,
                           author_id="200", created_at=iso_utc(3), likes=1200)],
    }
    return x


def test_x_official_posts_become_items(settings, state):
    x = make_x()
    settings.news_feeds = []
    hub = NewsHub(settings, state, x=x, http_get=FakeHttp())
    items = hub.collect()
    by_id = {i.url: i for i in items}
    gpt = by_id["https://x.com/OpenAI/status/9001"]
    assert gpt.title == "Introducing GPT-6 for everyone & every plan"
    assert gpt.summary == "Introducing GPT-6 for everyone & every plan Now rolling out in ChatGPT"
    assert "t.co" not in gpt.summary
    assert gpt.source == "@OpenAI" and gpt.official is True and gpt.kind == "x"
    assert gpt.engagement == 5000
    assert gpt.published == NOW_UTC - timedelta(hours=1)
    claude = by_id["https://x.com/claudeai/status/9101"]
    assert len(claude.title) <= 120 and claude.title.endswith("…")
    assert claude.source == "@claudeai"
    # lectures via l'API : max_results=5, lecture optionnelle (budget du jour)
    assert ("user_posts", "100", None) in x.calls and ("user_posts", "200", None) in x.calls


def test_x_ids_resolved_only_when_missing_and_shared_with_radar(settings, state):
    state.data["radar"]["user_ids"]["claudeai"] = "200"   # déjà résolu par le radar
    x = make_x()
    settings.news_feeds = []
    NewsHub(settings, state, x=x, http_get=FakeHttp()).collect()
    assert [c for c in x.calls if c[0] == "user_ids"] == [("user_ids", ("OpenAI",))]
    assert state.data["radar"]["user_ids"] == {"claudeai": "200", "openai": "100"}

    # Nouvelle instance (redémarrage), même état : plus aucune résolution.
    x2 = make_x()
    NewsHub(settings, state, x=x2, http_get=FakeHttp()).collect()
    assert not [c for c in x2.calls if c[0] == "user_ids"]


def test_x_since_id_and_rolling_cache(settings, state, clock):
    x = make_x()
    settings.news_feeds = []
    hub = NewsHub(settings, state, x=x, http_get=FakeHttp())
    first = hub.collect()
    assert len(first) == 2
    assert state.data["news_since"] == {"openai": "9001", "claudeai": "9101"}
    assert set(state.data["news_x_cache"]) == {"9001", "9101"}

    # 40 min plus tard (cache mémoire expiré, autre instance) : pas de relecture X, posts du cache.
    clock(10, 40)
    x2 = make_x()
    hub2 = NewsHub(settings, state, x=x2, http_get=FakeHttp())
    again = hub2.collect()
    assert not [c for c in x2.calls if c[0] == "user_posts"]
    assert {i.url for i in again} == {i.url for i in first}

    # 2 h après la 1re lecture : relecture avec since_id.
    clock(12, 5)
    hub2.clear_cache()
    hub2.collect()
    assert ("user_posts", "100", "9001") in x2.calls
    assert ("user_posts", "200", "9101") in x2.calls

    # 2 jours plus tard : les posts de plus de 48 h sortent du cache.
    clock(12, 0, day=17)
    hub3 = NewsHub(settings, state, x=make_x(), http_get=FakeHttp())
    assert hub3.collect(use_x=False) == []
    assert state.data["news_x_cache"] == {}


def test_x_disabled_or_unconfigured_makes_no_calls(settings, state):
    settings.news_feeds = []
    x = make_x()
    assert NewsHub(settings, state, x=x, http_get=FakeHttp()).collect(use_x=False) == []
    assert x.calls == []

    settings.x_news_accounts_enabled = False
    assert NewsHub(settings, state, x=x, http_get=FakeHttp()).collect() == []
    assert x.calls == []

    settings.x_news_accounts_enabled = True
    x.configured = False
    assert NewsHub(settings, state, x=x, http_get=FakeHttp()).collect() == []
    assert x.calls == []

    assert NewsHub(settings, state, x=None, http_get=FakeHttp()).collect() == []


def test_use_x_false_still_reuses_already_paid_posts(settings, state, clock):
    settings.news_feeds = []
    NewsHub(settings, state, x=make_x(), http_get=FakeHttp()).collect()
    clock(10, 30)
    x = make_x()
    items = NewsHub(settings, state, x=x, http_get=FakeHttp()).collect(use_x=False)
    assert len(items) == 2 and x.calls == []


class BudgetX(FakeX):
    def user_posts(self, user_id, since_id=None, max_results=5, optional=True):
        self.calls.append(("user_posts", user_id, since_id))
        raise XError("budget", "Budget du jour pour les lectures atteint")


def test_x_budget_error_is_tolerated(settings, state):
    x = BudgetX()
    x.ids_map = {"openai": "100", "claudeai": "200"}
    tc = feed("TechCrunch AI", "https://techcrunch.com/feed/", 2)
    settings.news_feeds = [tc]
    http = FakeHttp({tc["url"]: (200, rss_feed(entry("New Claude model is faster", "https://tc.com/a")))})
    items = NewsHub(settings, state, x=x, http_get=http).collect()
    assert [i.title for i in items] == ["New Claude model is faster"]
    # le budget coupe tout de suite : un seul essai, pas d'acharnement sur les autres comptes
    assert len([c for c in x.calls if c[0] == "user_posts"]) == 1
    assert state.data["news_since"] == {}


class FlakyX(FakeX):
    """@OpenAI introuvable (not_found), @claudeai OK ; user_ids peut lever."""

    def __init__(self, ids_error: XError | None = None) -> None:
        super().__init__()
        self.ids_error = ids_error

    def user_ids(self, usernames):
        if self.ids_error:
            self.calls.append(("user_ids", tuple(usernames)))
            raise self.ids_error
        return super().user_ids(usernames)

    def user_posts(self, user_id, since_id=None, max_results=5, optional=True):
        if user_id == "100":
            self.calls.append(("user_posts", user_id, since_id))
            raise XError("not_found", "Not Found", 404)
        return super().user_posts(user_id, since_id, max_results, optional)


def test_x_account_error_skips_only_that_account(settings, state):
    x = FlakyX()
    x.ids_map = {"openai": "100", "claudeai": "200"}
    x.user_posts_map = make_x().user_posts_map
    settings.news_feeds = []
    items = NewsHub(settings, state, x=x, http_get=FakeHttp()).collect()
    assert [i.source for i in items] == ["@claudeai"]


def test_x_user_ids_error_is_tolerated(settings, state):
    x = FlakyX(ids_error=XError("rate_limited", "Too Many Requests", 429))
    settings.news_feeds = []
    hub = NewsHub(settings, state, x=x, http_get=FakeHttp())
    assert hub.collect() == []
    assert not [c for c in x.calls if c[0] == "user_posts"]
    # comptes non résolus : pas de nouvel essai payant avant plusieurs heures
    hub.clear_cache()
    hub.collect()
    assert len([c for c in x.calls if c[0] == "user_ids"]) == 1


# ---------------------------------------------------------------------------
# Pannes de flux
# ---------------------------------------------------------------------------
def test_failing_feeds_are_skipped(settings, state):
    good = feed("Good", "https://good.example/feed")
    boom = feed("Boom", "https://boom.example/feed")
    down = feed("Down", "https://down.example/feed")
    html_page = feed("Html", "https://html.example/feed")
    bad_hn = feed("HN", "https://hn.algolia.com/api/v1/search?tags=front_page", type_="hn")
    weird = feed("Weird", "https://weird.example/feed")
    settings.news_feeds = [good, boom, down, html_page, bad_hn, weird, {"name": "no url"}, "not a dict"]
    settings.x_news_accounts = []
    http = FakeHttp({
        good["url"]: (200, rss_feed(entry("Gemini gets a new coding agent", "https://good.example/1"))),
        boom["url"]: ConnectionError("connexion refusée"),
        down["url"]: (503, "Service Unavailable"),
        html_page["url"]: (200, "<html><body><h1>Just a moment...</h1><script>x</script>"),
        bad_hn["url"]: (200, "{not json"),
        weird["url"]: ("abc", None),
    })
    hub = NewsHub(settings, state, http_get=http)
    items = hub.collect()
    assert [i.title for i in items] == ["Gemini gets a new coding agent"]
    assert set(hub.last_stats["failed_feeds"]) == {"Boom", "Down", "Html", "HN", "Weird"}


def test_x_com_feed_url_is_never_fetched(settings, state):
    bad = feed("Scrape", "https://x.com/OpenAI")
    bad2 = feed("Scrape2", "https://nitter.net/OpenAI/rss")
    hub, http = hub_with(settings, state, [(bad, (200, rss_feed())), (bad2, (200, rss_feed()))])
    assert hub.collect() == []
    assert http.calls == []


def test_stale_feed_is_reused_when_source_goes_down(settings, state, clock):
    tc = feed("TechCrunch AI", "https://techcrunch.com/feed/", 2)
    body = rss_feed(entry("OpenAI ships a new voice model", "https://tc.com/voice"))
    hub, http = hub_with(settings, state, [(tc, (200, body))])
    assert len(hub.collect()) == 1
    clock(11, 0)
    http.routes[tc["url"]] = (500, "")
    items = hub.collect()
    assert [i.title for i in items] == ["OpenAI ships a new voice model"]
    assert hub.last_stats["failed_feeds"] == ["TechCrunch AI"]
    # au-delà de 6 h, la vieille version n'est plus utilisée
    clock(17, 30)
    assert hub.collect() == []


# ---------------------------------------------------------------------------
# Filtres
# ---------------------------------------------------------------------------
def test_filters_drop_old_offlimits_noise_and_blacklist(settings, state):
    settings.blacklist_keywords = ["clickbaitcorp"]
    tc = feed("TechCrunch AI", "https://techcrunch.com/feed/", 2)
    body = rss_feed(
        entry("Claude Code adds background agents", "https://tc.com/ok", 3),
        entry("Old news about a GPT update", "https://tc.com/old", 40),
        entry("Bitcoin traders now use AI bots", "https://tc.com/crypto", 1),
        entry("AI and the presidential election ads", "https://tc.com/politics", 1),
        entry("An AI sommelier picks your wine", "https://tc.com/wine", 1),
        entry("Giveaway: win a free GPU, like and rt", "https://tc.com/noise", 1),
        entry("ClickbaitCorp launches an AI app", "https://tc.com/bl", 1),
        entry("New chatbot launch", "https://tc.com/drama", 1, desc="Two people were killed in the incident."),
    )
    hub, _ = hub_with(settings, state, [(tc, (200, body))])
    items = hub.collect(max_age_hours=36)
    assert [i.title for i in items] == ["Claude Code adds background agents"]
    assert hub.last_stats["old"] == 1
    assert hub.last_stats["blocked"] == 6
    # avec une fenêtre plus large, le vieil article revient
    hub.clear_cache()
    assert "Old news about a GPT update" in [i.title for i in hub.collect(max_age_hours=48)]


def test_hard_block_ignores_words_inside_other_words(settings, state):
    assert hard_block_hit("Researchers studied how skilled devs plan a trip with Claude") is None
    assert hard_block_hit("Strip the boilerplate from your prompts") is None
    assert hard_block_hit("Two people killed in a crash") == "killed"
    assert hard_block_hit("RIP to a legend") == "rip"
    assert hard_block_hit("Terrorism report") == "terror"
    tc = feed("TechCrunch AI", "https://techcrunch.com/feed/", 2)
    body = rss_feed(entry("We studied 500 prompts", "https://tc.com/studied", 1,
                          desc="Skilled users plan a trip in minutes."))
    hub, _ = hub_with(settings, state, [(tc, (200, body))])
    assert [i.title for i in hub.collect()] == ["We studied 500 prompts"]


# ---------------------------------------------------------------------------
# Dédoublonnage et score
# ---------------------------------------------------------------------------
def test_dedupe_same_url_and_similar_titles_keep_best_score(settings, state):
    official = feed("OpenAI", "https://openai.com/news/rss.xml", 3, official=True)
    press = feed("The Verge AI", "https://theverge.com/rss", 1)
    press2 = feed("Ars Technica AI", "https://arstechnica.com/feed", 2)
    hub, _ = hub_with(settings, state, [
        (official, (200, rss_feed(entry("Introducing GPT-6 for developers", "https://openai.com/index/gpt-6/", 2)))),
        (press, (200, rss_feed(
            entry("OpenAI introduces GPT-6 for developers", "https://www.openai.com/index/gpt-6?utm_source=verge", 1),
            entry("Introducing GPT-6 for developers today", "https://theverge.com/gpt6", 1),
        ))),
        (press2, (200, rss_feed(entry("Google ships Gemini in Chrome", "https://ars.com/gemini", 1)))),
    ])
    items = hub.collect()
    assert [i.source for i in items] == ["OpenAI", "Ars Technica AI"]
    assert items[0].url == "https://openai.com/index/gpt-6/"
    assert hub.last_stats["dupes"] == 2


def test_scoring_formula_and_order(settings, state):
    hn = feed("Hacker News", "https://hn.algolia.com/api/v1/search?tags=front_page", 2, type_="hn")
    blog = feed("OpenAI", "https://openai.com/news/rss.xml", 3, official=True)
    small = feed("Small blog", "https://small.example/feed", 1)
    hits = {"hits": [{"title": "Claude agents in production", "url": "https://hn.example/agents",
                      "points": 100, "created_at_i": int(NOW_UTC.timestamp()), "objectID": "1"}]}
    hub, _ = hub_with(settings, state, [
        (hn, (200, json.dumps(hits))),
        (blog, (200, rss_feed(entry("Codex now reviews pull requests", "https://openai.com/codex-review", 10)))),
        (small, (200, rss_feed(
            entry("A fresh prompt trick for meetings", "https://small.example/fresh", 1),
            entry("A day-old Gemini tutorial", "https://small.example/old", 20),
        ))),
    ])
    items = hub.collect()
    scores = {i.title: i.score for i in items}
    assert scores["Codex now reviews pull requests"] == pytest.approx(0.5 * 3, rel=1e-4)
    assert scores["Claude agents in production"] == pytest.approx(2 * (1 + math.log1p(100) / 3), rel=1e-4)
    assert scores["A fresh prompt trick for meetings"] == pytest.approx(0.5 ** 0.1, rel=1e-4)
    assert scores["A day-old Gemini tutorial"] == pytest.approx(0.25, rel=1e-4)
    assert [i.title for i in items] == ["Claude agents in production", "Codex now reviews pull requests",
                                        "A fresh prompt trick for meetings", "A day-old Gemini tutorial"]
    assert all(a.score >= b.score for a, b in zip(items, items[1:], strict=False))


def test_x_posts_and_feeds_are_merged_and_sorted(settings, state):
    tc = feed("TechCrunch AI", "https://techcrunch.com/feed/", 2)
    settings.news_feeds = [tc]
    http = FakeHttp({tc["url"]: (200, rss_feed(entry("Perplexity adds a shopping agent", "https://tc.com/p", 1)))})
    items = NewsHub(settings, state, x=make_x(), http_get=http).collect()
    assert [i.kind for i in items] == ["x", "x", "rss"]   # gros engagement des posts officiels
    assert items[0].source == "@OpenAI"


def test_undated_entries_limited_and_future_dates_clamped(settings, state):
    tc = feed("Undated", "https://undated.example/feed")
    entries = [entry(f"Undated model note {i}", f"https://undated.example/{i}", None) for i in range(8)]
    entries.append(entry("Scheduled AI post from the future", "https://undated.example/future", -30))
    hub, _ = hub_with(settings, state, [(tc, (200, rss_feed(*entries)))])
    items = hub.collect()
    undated = [i for i in items if i.published is None]
    assert len(undated) == sources.UNDATED_PER_FEED
    future = next(i for i in items if "future" in i.url)
    assert future.published == now_local()


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------
def test_collect_result_cached_20_minutes(settings, state, clock):
    tc = feed("TechCrunch AI", "https://techcrunch.com/feed/", 2)
    hub, http = hub_with(settings, state, [(tc, (200, rss_feed(entry("Claude for Chrome", "https://tc.com/c"))))])
    first = hub.collect()
    clock(10, 10)
    assert hub.collect() == first
    assert hub.collect(max_age_hours=24) != []      # autres arguments : flux encore en cache
    assert len(http.calls) == 1
    clock(10, 21)
    hub.collect()
    assert len(http.calls) == 2


def test_returned_items_are_copies(settings, state):
    tc = feed("TechCrunch AI", "https://techcrunch.com/feed/", 2)
    hub, _ = hub_with(settings, state, [(tc, (200, rss_feed(entry("Claude for Excel", "https://tc.com/x"))))])
    items = hub.collect()
    items[0].score = -1
    items[0].title = "changed"
    again = hub.collect()
    assert again[0].title == "Claude for Excel" and again[0].score > 0


def test_collect_never_raises_on_internal_errors(settings, state, monkeypatch):
    tc = feed("TechCrunch AI", "https://techcrunch.com/feed/", 2)
    hub, _ = hub_with(settings, state, [(tc, (200, rss_feed(entry("Claude for Excel", "https://tc.com/x"))))])
    monkeypatch.setattr(sources, "dedupe", lambda items: (_ for _ in ()).throw(RuntimeError("bug")))
    assert hub.collect() == []


# ---------------------------------------------------------------------------
# top_for
# ---------------------------------------------------------------------------
def _item(title: str, source: str, hours_ago: float | None, score: float, official: bool = False) -> NewsItem:
    pub = now_local() - timedelta(hours=hours_ago) if hours_ago is not None else None
    return NewsItem(id=title, title=title, summary="", url=f"https://e.com/{title}", source=source,
                    published=pub, official=official, score=score)


def test_top_for_news_reactive_and_broad_mix():
    items = [
        _item("old-big", "A", 30, 9.0),
        _item("fresh-1", "A", 2, 5.0),
        _item("fresh-2", "A", 3, 4.0),
        _item("fresh-3", "A", 4, 3.5),
        _item("fresh-4", "A", 5, 3.4),
        _item("fresh-b", "B", 6, 3.0),
        _item("undated", "C", None, 2.0),
        _item("old-official", "OpenAI", 28, 0.5, official=True),
    ]
    assert top_for("news", items, 4) == [items[1], items[2], items[3], items[5]]  # 3 par source max
    news = top_for("news", items, 6)
    assert news[:5] == items[1:6]                                     # tout le frais d'abord…
    assert news[5] is items[0]                                        # …puis complété par le reste
    reactive = top_for("reactive", items, 10)
    assert {i.title for i in reactive} == {"fresh-1", "fresh-2", "fresh-3", "fresh-4", "fresh-b"}
    mix = top_for("tip", items, 3)
    assert {i.title for i in mix} == {"old-big", "fresh-1", "old-official"}  # 2 par source + 1/3 officiel
    assert [i.score for i in mix] == sorted((i.score for i in mix), reverse=True)
    assert len(top_for("thread", items)) == len(items)               # n par défaut = 15
    assert top_for("gm", [], 5) == [] and top_for("news", items, 0) == []
    assert NewsHub.top_for("reactive", items, 1) == [items[1]]


# ---------------------------------------------------------------------------
# HTTP par défaut (requests simulé, aucun réseau)
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, chunks: list[bytes], ctype: str, status: int = 200) -> None:
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self.encoding = "ISO-8859-1" if "charset=" not in ctype.lower() else ctype.split("charset=")[1]
        self._chunks = chunks

    def iter_content(self, chunk_size=1):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_default_http_get_caps_size_and_decodes(monkeypatch):
    import requests

    seen: dict = {}

    def fake_get(url, timeout=None, headers=None, stream=False):
        seen.update(url=url, timeout=timeout, ua=(headers or {}).get("User-Agent"), stream=stream)
        body = '<?xml version="1.0" encoding="ISO-8859-1"?><rss><title>Café</title></rss>'.encode("latin-1")
        return _FakeResp([body], "application/rss+xml")

    monkeypatch.setattr(requests, "get", fake_get)
    status, text = sources.default_http_get("https://feed.example/rss")
    assert status == 200 and "Café" in text
    assert seen["timeout"] == 15 and seen["stream"] is True and "Mozilla" in seen["ua"]

    monkeypatch.setattr(sources, "MAX_FEED_BYTES", 100)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp([b"a" * 60] * 5, "text/xml; charset=utf-8"))
    status, text = sources.default_http_get("https://feed.example/big")
    assert text == "a" * 60

    monkeypatch.setattr(sources, "HTTP_MAX_SECONDS", -1)   # serveur qui distille : on coupe
    status, text = sources.default_http_get("https://feed.example/slow")
    assert status == 200 and text == ""
