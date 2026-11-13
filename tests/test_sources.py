"""Tests de bot/sources.py : flux RSS/Atom, Hacker News, comptes officiels X
(API seulement, via FakeX), filtres (ligne éditoriale, militaire, bons plans,
flux généralistes), public visé (audience_factor / appeal), dédoublonnage, score,
caches, sélection pour la rédaction (top_for) et lecture du texte des articles.
Aucun réseau."""
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

# conftest remplace sources.default_http_get par une fonction qui lève (aucun réseau).
# On garde ici la vraie, capturée à l'import (avant tout fixture), pour la tester avec
# un `requests.get` simulé.
_REAL_DEFAULT_HTTP_GET = sources.default_http_get


@pytest.fixture(autouse=True)
def _frozen(clock, settings):
    clock(10, 0)
    settings.radar_enabled = False   # ici on teste la lecture d'actu seule (sans partage avec le radar)
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


def news_item(title: str, summary: str = "") -> NewsItem:
    return NewsItem(id="x", title=title, summary=summary, url="https://e.com/a", source="s", published=None)


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

# Un item tel que ScienceAlert le publie (novembre 2026) : image, chapô d'une ligne, puis
# la signature « … stories are written, fact-checked, and edited by humans, never
# generated by AI. Don't miss a story, subscribe here. » dans un lien vers la newsletter.
SCIENCEALERT_ITEM = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>ScienceAlert</title><link>https://www.sciencealert.com</link>
<description>d</description>
<item>
  <title>This Robotic Hand Walks on Its Fingers Like Thing From The Addams Family, And It&#039;s Creepy as Heck</title>
  <link>https://www.sciencealert.com/this-robotic-hand-walks-on-its-fingers</link>
  <pubDate>{rfc822(3)}</pubDate>
  <description><![CDATA[<p><img width="1200" height="675" src="https://www.sciencealert.com/images/2026/11/WalkingHand-1200x675.png" class="attachment-large size-large wp-post-image" alt="This Robotic Hand Walks on Its Fingers" style="float:left; margin:0 15px 15px 0;" decoding="async" />A helping hand, literally.</p><p><a href="https://www.sciencealert.com/newsletter?utm_source=rss_feed&#038;utm_medium=feed&#038;utm_campaign=rss_feed_promo" target="_blank" rel="noopener">ScienceAlert stories are written, fact-checked, and edited by humans, never generated by AI. Don&#039;t miss a story, subscribe here.</a></p>]]></description>
</item>
</channel></rss>"""


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
    # parse_rss ne note pas : score et intérêt sont calculés par NewsHub
    assert a.score == 0 and a.appeal == 0 and a.body == ""


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
    # (Le routeur « …picks the cheapest model » est aujourd'hui écarté comme « bon plan » :
    # voir test_shopping_filter_misfires_on_business_and_launch_titles.)
    assert {"Notes from our infra migration", "Ask HN: How do you use Claude for code review?",
            "Nvidia unveils a new inference chip"} <= {i.title for i in items}
    assert http.calls == [hn["url"]]
    # intérêt = poids × engagement × public visé (sans la fraîcheur)
    for it in items:
        assert it.appeal == pytest.approx(2 * (1 + math.log1p(it.engagement) / 3) * sources.audience_factor(it),
                                          rel=1e-5)


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
    # comptes X : poids X_ACCOUNT_WEIGHT, likes = engagement
    assert gpt.appeal == pytest.approx(
        sources.X_ACCOUNT_WEIGHT * (1 + math.log1p(5000) / 3) * sources.audience_factor(gpt), rel=1e-5)
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
        entry("Tinder adds an AI matchmaker that writes your opener", "https://tc.com/dating", 1),
    )
    hub, _ = hub_with(settings, state, [(tc, (200, body))])
    items = hub.collect(max_age_hours=36)
    assert [i.title for i in items] == ["Claude Code adds background agents"]
    assert hub.last_stats["old"] == 1
    assert hub.last_stats["blocked"] == 7
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


def test_military_items_and_shopping_titles_are_blocked(settings, state):
    press = feed("Press", "https://press.example/feed", 2)
    body = rss_feed(
        entry("A humanoid robot can now fold your laundry", "https://press.example/robot", 1),
        entry("New hypersonic missile tested over the Pacific", "https://press.example/missile", 1),
        entry("Drone maker unveils a faster delivery drone", "https://press.example/drone", 1,
              desc="The army has already ordered 500 of them for its troops."),          # militaire dans le résumé
        entry("Pentagon picks an AI start-up to sort satellite images", "https://press.example/pentagon", 1),
        entry("Best Black Friday deals on robot vacuums", "https://press.example/bf", 1),
        entry("Save 30% on Meta's smart glasses with this promo code", "https://press.example/promo", 1),
        entry("Review: Meta's Ray-Ban Display glasses", "https://press.example/review", 1),
        entry("Hands-on with the new Galaxy foldable", "https://press.example/handson", 1),
        entry("The best smart glasses of 2026", "https://press.example/best", 1),
        # Un prix (et même une baisse de prix) dans le RÉSUMÉ ne bloque pas : seul le titre compte.
        entry("Unitree's robot dog now costs less than a laptop", "https://press.example/price", 1,
              desc="It sells for $1,600, a 40% price cut from last year's model."),
    )
    hub, _ = hub_with(settings, state, [(press, (200, body))])
    kept = {i.url.rsplit("/", 1)[1] for i in hub.collect()}
    assert kept == {"robot", "price", "handson"}         # « hands-on » : freiné (×0,3), plus bloqué
    assert hub.last_stats["blocked"] == 7
    # La raison (dans le journal) dit pourquoi l'actu a été écartée.
    assert hub._blocked_reason(news_item("Stealth warships get new missiles")).startswith("sujet militaire : ")
    assert hub._blocked_reason(news_item("Fighter jets fly without a pilot")) == "sujet militaire : Fighter jets"
    assert hub._blocked_reason(news_item("Prime Day deals on Kindles")) == "bon plan : Prime Day"
    assert hub._blocked_reason(news_item("50% off the Pixel Watch")) == "bon plan : 50% off"
    assert hub._blocked_reason(news_item("A robot dog that opens doors",
                                         "Was $2,000, now on sale with a coupon.")) is None
    # Pas de faux positif à l'intérieur d'un mot.
    for title in ("The ideal dealer for a used robot", "Armyworms threaten crops, AI spots them early",
                  "Scientists combine two quantum chips"):
        assert hub._blocked_reason(news_item(title)) is None, title


def test_topic_filter_ai_tech_or_none():
    assert sources.topic_filter({}) is None
    assert sources.topic_filter({"filter": ""}) is None
    assert sources.topic_filter({"filter": None}) is None
    assert sources.topic_filter({"filter": "sports"}) is None       # inconnu : pas de filtre
    ai = sources.topic_filter({"filter": " AI "})                     # casse et espaces indifférents
    tech = sources.topic_filter({"filter": "Tech"})
    assert callable(ai) and callable(tech)
    about_ai = ["ChatGPT can now talk to you for free", "A.I. is coming for your job", "OpenAI's new device",
                "The new GPT-5.1 model writes faster", "Deepfakes of your boss are getting better",
                "Voice cloning scams target parents"]
    about_tech = ["New robot dog can climb stairs", "Electric cars now outsell petrol cars in Norway",
                  "This new phone folds three times", "SpaceX launches Starship again",
                  "Scientists 3D-print a working heart", "Hackers hit a hospital's Wi-Fi"]
    neither = ["Scientists find a new species of frog", "Honeymoon destinations for 2027",
               "The future of cities", "Why your aid package arrived late", "Thailand's new prime minister",
               "Said the AIDS researchers"]
    for text in about_ai:
        assert ai(text) and tech(text), text                          # « tech » comprend l'IA
    for text in about_tech:
        assert tech(text) and not ai(text), text
    for text in neither:
        assert not ai(text) and not tech(text), text


def test_general_feed_keeps_only_ai_stories(settings, state):
    bbc = dict(feed("BBC News", "https://feeds.bbci.co.uk/news/technology/rss.xml"), filter="ai")
    body = rss_feed(
        entry("New folding phone survives 385,000 folds", "https://bbc.com/news/1", 1),
        entry("ChatGPT can now talk to you for free", "https://bbc.com/news/2", 1),
        entry("Console prices rise again", "https://bbc.com/news/3", 1, desc="Nothing about AI here? It says AI."),
        entry("Streaming service adds a sleep timer", "https://bbc.com/news/4", 1, desc="A new feature for viewers."),
        entry("Why your aid package arrived late", "https://bbc.com/news/5", 1),       # « aid » n'est pas « AI »
    )
    hub, _ = hub_with(settings, state, [(bbc, (200, body))])
    assert sorted(i.url for i in hub.collect()) == ["https://bbc.com/news/2", "https://bbc.com/news/3"]
    # Sans filtre, tout passe (flux déjà consacré à l'IA).
    plain = feed("BBC News", "https://feeds.bbci.co.uk/news/technology/rss.xml")
    hub2, _ = hub_with(settings, state, [(plain, (200, body))])
    assert len(hub2.collect()) == 5


def test_tech_filter_keeps_tech_and_ai_stories_only():
    guardian = dict(feed("The Guardian", "https://www.theguardian.com/technology/rss"), filter="tech")
    body = rss_feed(
        entry("Waymo's driverless taxis are coming to London", "https://g.com/1", 1),
        entry("Parliament votes on the new budget", "https://g.com/2", 1, desc="Taxes and pensions."),
        entry("A rare frog was found in Peru", "https://g.com/3", 1,
              desc="Researchers spotted it with an AI camera trap."),            # le résumé compte aussi
        entry("Gardening tips for a dry summer", "https://g.com/4", 1),
    )
    items = parse_rss(body, guardian)
    assert [i.url for i in items] == ["https://g.com/1", "https://g.com/3"]
    assert len(parse_rss(body, feed("Plain", "https://plain.example/feed"))) == 4


def test_signature_boilerplate_is_stripped_from_summaries():
    (item,) = parse_rss(SCIENCEALERT_ITEM, feed("ScienceAlert", "https://www.sciencealert.com/tech/feed"))
    assert item.title.startswith("This Robotic Hand Walks on Its Fingers")
    assert item.summary == "A helping hand, literally."
    # La signature parle d'« AI » : une fois retirée, elle ne fait plus passer le filtre IA…
    assert parse_rss(SCIENCEALERT_ITEM, dict(feed("ScienceAlert", "https://sa.example/feed"), filter="ai")) == []
    # … alors que le filtre « tech » garde bien une histoire de robot.
    assert len(parse_rss(SCIENCEALERT_ITEM, dict(feed("ScienceAlert", "https://sa.example/feed"),
                                                 filter="tech"))) == 1
    cs = sources.clean_summary
    assert cs("A drone delivered a defibrillator in four minutes. Don’t miss a story, subscribe here") == (
        "A drone delivered a defibrillator in four minutes.")
    assert cs("A robot folded 40 shirts an hour. Our articles are written, fact-checked and edited by humans."
              ) == "A robot folded 40 shirts an hour."
    assert cs("The robot folded 40 shirts an hour [&#8230;] Continue reading on Example") == (
        "The robot folded 40 shirts an hour")
    assert cs("The robot folded 40 shirts an hour... Read more") == "The robot folded 40 shirts an hour"
    assert cs("The robot folded 40 shirts an hour [...] and then it rested.") == (
        "The robot folded 40 shirts an hour … and then it rested.")
    # Une phrase qui parle d'humains et d'IA, sans la signature, reste.
    assert cs("Humans still edit what the AI writes, the study found.") == (
        "Humans still edit what the AI writes, the study found.")


# ---------------------------------------------------------------------------
# Public visé : de quoi intriguer tout le monde, actu d'initiés, bons plans
# ---------------------------------------------------------------------------
W, I_T, I_S, SH = (sources.WOW_BOOST, sources.INSIDER_PENALTY, sources.INSIDER_SUMMARY_PENALTY,
                   sources.SHOPPING_PENALTY)


@pytest.mark.parametrize("title, summary, expected", [
    # Le titre intrigue
    ("Unitree's new humanoid robot can do backflips", "", W),
    ("ChatGPT can now remember everything you tell it", "", W),
    ("A man cloned his own voice to answer his phone calls", "", W),
    ("Waymo's driverless taxis are coming to London", "", W),
    ("Scientists grow lab-grown teeth in a pig", "", W),
    # Le titre parle aux initiés
    ("Anthropic raises $13 billion at a $183 billion valuation", "", I_T),
    ("Nvidia's quarterly earnings beat expectations", "", I_T),
    ("OpenAI opens a new data center in Norway", "", I_T),
    ("New open-source model tops coding benchmarks", "", I_T),
    # Les deux : neutre ; ni l'un ni l'autre : neutre
    ("Developers can now run Claude inside their own apps", "", 1.0),
    ("OpenAI CEO unveils a new device", "", 1.0),
    ("Google updates its Pixel camera app", "", 1.0),
    # Titre muet : le début du résumé décide, plus doucement pour les initiés
    ("A new kind of battery", "Researchers built a humanoid robot that runs for a week on one charge.", W),
    ("A new kind of battery", "The start-up raised $40 million from investors to build it.", I_S),
    ("A new kind of battery", "Its robot can now run for a week, and investors are lining up.", 1.0),
    ("A new kind of battery", "Nothing unusual in this lead at all.", 1.0),
    ("A new kind of battery", "x" * 300 + " a humanoid robot", 1.0),           # au-delà de 300 caractères
    # Le titre décide avant le résumé
    ("Anthropic raises $13 billion in new funding", "Its chatbot Claude can now control a robot arm.", I_T),
    ("A humanoid robot that does your chores", "The start-up raised $40 million from investors.", W),
    # Bons plans (titre ou résumé) : ×0,3 en plus
    ("Best Black Friday deals on robot vacuums", "", W * SH),
    ("Review: the new Kindle is lighter and faster", "", SH),
    ("The best smart glasses of 2026", "", W * SH),
    ("A quiet update for a note-taking app", "Save 40% on the annual plan this week.", SH),
    # Faux amis : un mot-clé à l'intérieur d'un autre mot ne compte pas
    ("Cyclone hits the coast as forecasters brainstorm", "", 1.0),
    ("Cloudflare outage takes down half the web", "", 1.0),
    ("Hackathon winners pitch a moonshot to honeymooners", "", 1.0),
    ("The ideal dealer for a replacement chipset", "", 1.0),
    ("Geminid meteor shower peaks tonight over Marseille", "", 1.0),
    ("Shareholders question the new strategy", "", 1.0),
])
def test_audience_factor(title, summary, expected):
    assert sources.audience_factor(news_item(title, summary)) == pytest.approx(expected)


def test_audience_constants_are_ordered():
    assert sources.WOW_BOOST > 1 > sources.INSIDER_SUMMARY_PENALTY > sources.INSIDER_PENALTY > 0
    assert 0 < sources.SHOPPING_PENALTY < 1
    # Un objet sans titre ni résumé (actu mal formée) ne fait pas planter le calcul.
    assert sources.audience_factor(NewsItem(id="x", title=None, summary=None, url="", source="",
                                            published=None)) == 1.0


def test_appeal_of_is_interest_without_freshness():
    robot = news_item("A humanoid robot can now fold your laundry")
    plain = news_item("Assistant gets a redesigned settings page")
    assert sources.appeal_of(robot, 2) == pytest.approx(2 * W)
    assert sources.appeal_of(plain, 2) == pytest.approx(2.0)
    plain.engagement = 100                                                  # points HN, likes X
    assert sources.appeal_of(plain, 2) == pytest.approx(2 * (1 + math.log1p(100) / 3))
    plain.engagement = -5                                                   # jamais négatif
    assert sources.appeal_of(plain, 2) == pytest.approx(2.0)
    assert sources.appeal_of(robot, 0) == pytest.approx(0.1 * W)           # poids plancher
    # La date ne compte pas.
    robot.published = now_local() - timedelta(days=6)
    assert sources.appeal_of(robot, 2) == pytest.approx(2 * W)


def test_hub_sets_appeal_and_audience_weighted_score(settings, state):
    press = feed("Press", "https://press.example/feed", 2)
    body = rss_feed(
        entry("A humanoid robot can now fold your laundry", "https://press.example/robot", 30),
        entry("Assistant gets a redesigned settings page", "https://press.example/settings", 1),
    )
    hub, _ = hub_with(settings, state, [(press, (200, body))])
    items = {i.url.rsplit("/", 1)[1]: i for i in hub.collect(max_age_hours=48)}
    robot, page = items["robot"], items["settings"]
    assert robot.appeal == pytest.approx(2 * W)                            # poids × public, sans fraîcheur
    assert robot.score == pytest.approx(0.5 ** 3 * 2 * W, rel=1e-4)        # 30 h = 3 demi-vies de 10 h
    assert page.appeal == pytest.approx(2.0)
    assert page.score == pytest.approx(0.5 ** 0.1 * 2, rel=1e-4)
    # le score préfère le frais, l'intérêt préfère la belle histoire
    assert page.score > robot.score and robot.appeal > page.appeal
    assert robot.appeal == sources.appeal_of(robot, 2)


def test_wow_news_outranks_insider_news(settings, state):
    press = feed("Press", "https://press.example/feed", 2)
    hub, _ = hub_with(settings, state, [(press, (200, rss_feed(
        entry("Chip maker raises $2bn in new funding round", "https://press.example/funding", 1),
        entry("Gemini can now read your handwritten notes, for free", "https://press.example/notes", 3),
        entry("Assistant gets a redesigned settings page", "https://press.example/settings", 2),
    )))])
    assert [i.url.rsplit("/", 1)[1] for i in hub.collect()] == ["notes", "settings", "funding"]


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
    # (L'annonce « pour développeurs » passe derrière : le compte parle au grand public.)
    assert [i.source for i in items] == ["Ars Technica AI", "OpenAI"]
    assert items[1].url == "https://openai.com/index/gpt-6/"       # des 3 doublons, l'officiel est gardé
    assert hub.last_stats["dupes"] == 2


def test_scoring_formula_and_order(settings, state):
    hn = feed("Hacker News", "https://hn.algolia.com/api/v1/search?tags=front_page", 2, type_="hn")
    blog = feed("OpenAI", "https://openai.com/news/rss.xml", 3, official=True)
    small = feed("Small blog", "https://small.example/feed", 1)
    hits = {"hits": [{"title": "Agents in production: our new API for developers", "url": "https://hn.example/agents",
                      "points": 100, "created_at_i": int(NOW_UTC.timestamp()), "objectID": "1"}]}
    hub, _ = hub_with(settings, state, [
        (hn, (200, json.dumps(hits))),
        (blog, (200, rss_feed(entry("Codex now reviews pull requests", "https://openai.com/codex-review", 10)))),
        (small, (200, rss_feed(
            entry("A fresh prompt trick for meetings", "https://small.example/fresh", 1),
            entry("A day-old note-taking tutorial", "https://small.example/old", 20),
            entry("A robot dog learns to open doors", "https://small.example/dog", 20),
        ))),
    ])
    items = hub.collect()
    scores = {i.title: i.score for i in items}
    # score = fraîcheur (demi-vie 10 h) × poids × engagement × public visé
    assert scores["Codex now reviews pull requests"] == pytest.approx(0.5 * 3, rel=1e-4)
    assert scores["Agents in production: our new API for developers"] == pytest.approx(
        2 * (1 + math.log1p(100) / 3) * sources.INSIDER_PENALTY, rel=1e-4)      # actu d'initiés : freinée
    assert scores["A fresh prompt trick for meetings"] == pytest.approx(0.5 ** 0.1, rel=1e-4)
    assert scores["A day-old note-taking tutorial"] == pytest.approx(0.25, rel=1e-4)
    assert scores["A robot dog learns to open doors"] == pytest.approx(0.25 * W, rel=1e-4)
    assert [i.title for i in items] == ["Agents in production: our new API for developers",
                                        "Codex now reviews pull requests", "A fresh prompt trick for meetings",
                                        "A robot dog learns to open doors", "A day-old note-taking tutorial"]
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
    items[0].body = "texte lu par la rédaction"
    again = hub.collect()
    assert again[0].title == "Claude for Excel" and again[0].score > 0 and again[0].body == ""


def test_collect_never_raises_on_internal_errors(settings, state, monkeypatch):
    tc = feed("TechCrunch AI", "https://techcrunch.com/feed/", 2)
    hub, _ = hub_with(settings, state, [(tc, (200, rss_feed(entry("Claude for Excel", "https://tc.com/x"))))])
    monkeypatch.setattr(sources, "dedupe", lambda items: (_ for _ in ()).throw(RuntimeError("bug")))
    assert hub.collect() == []


# ---------------------------------------------------------------------------
# top_for : ce que la rédaction en chef (Claude) voit, selon le type de contenu
# ---------------------------------------------------------------------------
def _item(title: str, source: str, hours_ago: float | None, score: float, official: bool = False) -> NewsItem:
    """Actu construite sans NewsHub (appeal = 0) : le score seul sert au classement."""
    pub = now_local() - timedelta(hours=hours_ago) if hours_ago is not None else None
    return NewsItem(id=title, title=title, summary="", url=f"https://e.com/{title}", source=source,
                    published=pub, official=official, score=score)


def _story(title: str, source: str = "Press", hours_ago: float | None = 1.0, appeal: float = 1.0,
           official: bool = False) -> NewsItem:
    """Actu comme la rend NewsHub : appeal = poids × engagement × public visé, et
    score = appeal × fraîcheur (demi-vie 10 h, 24 h pour une actu sans date)."""
    pub = now_local() - timedelta(hours=hours_ago) if hours_ago is not None else None
    age = sources.UNDATED_AGE_HOURS if hours_ago is None else hours_ago
    return NewsItem(id=title, title=title, summary="", url=f"https://e.com/{title}", source=source,
                    published=pub, official=official, appeal=appeal,
                    score=round(appeal * 0.5 ** (age / sources.HALF_LIFE_HOURS), 6))


def _titles(items: list[NewsItem]) -> list[str]:
    return [i.title for i in items]


def test_top_for_news_fresh_first_ranked_by_interest_then_the_rest():
    items = [
        _story("minor-update", "A", 1, appeal=1.0),        # frais mais tiède
        _story("robot-story", "B", 10, appeal=3.2),        # frais et fort
        _story("insider-memo", "C", 3, appeal=0.45),       # frais, pour initiés
        _story("yesterday-big", "D", 30, appeal=6.0),      # gros, mais d'hier
        _story("undated", "E", None, appeal=2.0),          # sans date : jamais « frais »
    ]
    # Le frais d'abord, la plus forte histoire de la journée en tête (pas la dernière parue)…
    assert _titles(top_for("news", items, 5)) == ["robot-story", "minor-update", "insider-memo",
                                                  "yesterday-big", "undated"]
    # … même si le score brut (fraîcheur 10 h) classerait autrement.
    by_score = sorted(items, key=lambda i: i.score, reverse=True)
    assert _titles(by_score)[2] == "yesterday-big"
    assert _titles(top_for("news", items, 2)) == ["robot-story", "minor-update"]


def test_top_for_reactive_keeps_only_fresh_dated_items():
    items = [
        _story("minor-update", "A", 1, appeal=1.0),
        _story("robot-story", "B", 10, appeal=3.2),
        _story("insider-memo", "C", 3, appeal=0.45),
        _story("yesterday-big", "D", 30, appeal=60.0),
        _story("undated", "E", None, appeal=20.0),
    ]
    reactive = top_for("reactive", items, 10)
    assert _titles(reactive) == ["robot-story", "minor-update", "insider-memo"]
    assert NewsHub.top_for("reactive", items, 1) == [items[1]]
    assert top_for("reactive", [items[3], items[4]], 10) == []


def test_top_for_tip_thread_slow_freshness_and_weekly_interest_only():
    items = [
        _story("two-days-ago-wow", "A", 40, appeal=3.2),
        _story("this-morning-tepid", "B", 1, appeal=1.5),
        _story("fresh-insider", "C", 2, appeal=0.3),
        _story("last-week-wow", "D", 150, appeal=4.0),
    ]
    # « le futur arrive » et les threads : demi-vie de 48 h, la belle histoire d'avant-hier
    # passe devant l'actu tiède de ce matin.
    slow = ["two-days-ago-wow", "this-morning-tepid", "last-week-wow", "fresh-insider"]
    assert _titles(top_for("tip", items, 4)) == slow
    assert _titles(top_for("thread", items, 4)) == slow
    assert _titles(top_for("future", items, 4)) == slow                  # tout autre type : idem
    # La semaine en revue : l'intérêt seul.
    assert _titles(top_for("weekly", items, 4)) == ["last-week-wow", "two-days-ago-wow",
                                                    "this-morning-tepid", "fresh-insider"]
    # L'actu du jour, elle, reste au frais.
    assert _titles(top_for("news", items, 4)) == ["this-morning-tepid", "fresh-insider",
                                                  "two-days-ago-wow", "last-week-wow"]


def test_top_for_falls_back_on_score_without_appeal():
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
    # Le frais d'abord, 3 titres par source au plus (frais ET reste compris), puis le reste.
    assert _titles(news) == ["fresh-1", "fresh-2", "fresh-3", "fresh-b", "undated", "old-official"]
    # Une seule source : on complète quand même, le frais d'abord.
    only_a = [i for i in items if i.source == "A"]
    assert _titles(top_for("news", only_a, 5)) == ["fresh-1", "fresh-2", "fresh-3", "fresh-4", "old-big"]
    reactive = top_for("reactive", items, 10)
    assert {i.title for i in reactive} == {"fresh-1", "fresh-2", "fresh-3", "fresh-4", "fresh-b"}
    assert _titles(top_for("tip", items, 3)) == ["old-big", "fresh-1", "fresh-2"]
    assert len(top_for("thread", items)) == len(items)               # n par défaut = 15
    assert top_for("gm", [], 5) == [] and top_for("news", items, 0) == [] and top_for("tip", items, -3) == []


def test_top_for_caps_each_source_and_keeps_the_ranking_order():
    big = [_story(f"big-{k}", "Big Site", 1, appeal=10 - k) for k in range(6)]
    others = [_story(f"other-{k}", f"Site {k}", 1, appeal=2 - k / 100) for k in range(13)]
    # n = 16 : max(3, 16 // 8) = 3 titres par source.
    picked = top_for("tip", big + others, 16)
    assert _titles(picked) == ["big-0", "big-1", "big-2"] + [f"other-{k}" for k in range(13)]
    # Pas assez d'autres sources : on complète quand même, dans l'ordre du classement.
    picked = top_for("tip", big + others[:2], 6)
    assert _titles(picked) == ["big-0", "big-1", "big-2", "big-3", "other-0", "other-1"]
    # Le plafond ne dépend pas de la casse du nom de la source.
    same = [_story("verge-1", "The Verge", 1, 5), _story("verge-2", "the verge", 1, 4.9),
            _story("verge-3", "THE VERGE", 1, 4.8), _story("verge-4", "The Verge", 1, 4.7)]
    picked = top_for("tip", same + others[:3], 6)
    assert _titles(picked) == ["verge-1", "verge-2", "verge-3", "other-0", "other-1", "other-2"]


def test_top_for_caps_official_blogs_small_and_large_n():
    official = [_story(f"official-{k}", f"Company {k}", 1, appeal=50 - k, official=True) for k in range(10)]
    press = [_story(f"press-{k}", f"Press {k % 40}", 1, appeal=3 - k / 1000) for k in range(200)]
    items = official + press
    # n = 30 : max(3, 30 // 15) = 3 blogs officiels en tout.
    picked = top_for("tip", items, 30)
    assert len(picked) == 30 and sum(i.official for i in picked) == 3
    assert _titles(picked)[:3] == ["official-0", "official-1", "official-2"]
    # n = 60 : max(3, 60 // 15) = 4 ; n = 120 : 8 (les réglages du vrai bot).
    assert sum(i.official for i in top_for("thread", items, 60)) == 4
    assert sum(i.official for i in top_for("weekly", items, 120)) == 8
    # Petit n : les 3 premiers, officiels compris (plafond minimum de 3).
    assert _titles(top_for("tip", items, 3)) == ["official-0", "official-1", "official-2"]
    assert _titles(top_for("tip", items, 1)) == ["official-0"]
    # Grand n : max(3, 120 // 8) = 15 titres par source.
    big = [_story(f"big-{k}", "Big Site", 1, appeal=5 - k / 100) for k in range(20)]
    picked = top_for("tip", big + press, 120)
    assert len(picked) == 120 and sum(i.source == "Big Site" for i in picked) == 15
    # Pas assez d'actus non officielles : on complète avec les officielles.
    picked = top_for("tip", official + press[:2], 6)
    assert sum(i.official for i in picked) == 4 and len(picked) == 6


def test_top_for_reactive_does_not_cap_official_news():
    official = [_story(f"official-{k}", f"Company {k}", 1, appeal=5 - k / 10, official=True) for k in range(5)]
    press = [_story(f"press-{k}", f"Press {k}", 1, appeal=1) for k in range(5)]
    picked = top_for("reactive", official + press, 10)
    assert sum(i.official for i in picked) == 5      # une annonce officielle peut valoir un post immédiat


def test_top_for_news_official_cap_is_shared_between_fresh_and_rest():
    fresh = [_story(f"fresh-official-{k}", f"Company {k}", 2, appeal=3, official=True) for k in range(3)]
    fresh.append(_story("fresh-press", "Press", 2, appeal=1))
    rest = [_story(f"old-official-{k}", f"Lab {k}", 30, appeal=50, official=True) for k in range(2)]
    rest += [_story(f"old-press-{k}", f"Paper {k}", 30, appeal=2) for k in range(6)]
    picked = top_for("news", fresh + rest, 10)
    assert len(picked) == 10
    assert sum(i.official for i in picked) == 3
    assert not any(i.title.startswith("old-official") for i in picked)
    assert _titles(picked)[:4] == ["fresh-official-0", "fresh-official-1", "fresh-official-2", "fresh-press"]


# ---------------------------------------------------------------------------
# HTTP par défaut (requests simulé, aucun réseau)
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, chunks: list[bytes], ctype: str, status: int = 200, location: str | None = None) -> None:
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        if location:
            self.headers["Location"] = location
        self.is_redirect = bool(location) and status in (301, 302, 303, 307, 308)
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

    assert _REAL_DEFAULT_HTTP_GET.__module__ == "bot.sources"      # la vraie, pas celle du conftest
    seen: dict = {}

    def fake_get(url, timeout=None, headers=None, stream=False, allow_redirects=True):
        seen.update(url=url, timeout=timeout, ua=(headers or {}).get("User-Agent"),
                    accept=(headers or {}).get("Accept"), stream=stream, redirects=allow_redirects)
        body = '<?xml version="1.0" encoding="ISO-8859-1"?><rss><title>Café</title></rss>'.encode("latin-1")
        return _FakeResp([body], "application/rss+xml")

    monkeypatch.setattr(requests, "get", fake_get)
    status, text = _REAL_DEFAULT_HTTP_GET("https://feed.example/rss")
    assert status == 200 and "Café" in text
    assert seen["timeout"] == 15 and seen["stream"] is True and "Mozilla" in seen["ua"]
    assert seen["redirects"] is False                              # suivies une à une, jamais vers X
    assert seen["accept"] == sources.FEED_ACCEPT and "application/rss+xml" in seen["accept"]
    # Une page d'article se demande comme une page web.
    _REAL_DEFAULT_HTTP_GET("https://news.example/story", accept=sources.HTML_ACCEPT)
    assert seen["accept"] == sources.HTML_ACCEPT and seen["accept"].startswith("text/html")

    monkeypatch.setattr(sources, "MAX_FEED_BYTES", 100)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp([b"a" * 60] * 5, "text/xml; charset=utf-8"))
    status, text = _REAL_DEFAULT_HTTP_GET("https://feed.example/big")
    assert text == "a" * 60

    monkeypatch.setattr(sources, "HTTP_MAX_SECONDS", -1)   # serveur qui distille : on coupe après ce qui est arrivé
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp([b"<rss>", b"more"], "text/xml; charset=utf-8"))
    status, text = _REAL_DEFAULT_HTTP_GET("https://feed.example/slow")
    assert status == 200 and text == "<rss>"


def test_default_http_get_follows_redirects_but_never_to_x(monkeypatch):
    import requests

    asked: list[str] = []
    routes = {
        "https://short.example/a": _FakeResp([], "text/html", 302, "https://news.example/story"),
        "https://news.example/story": _FakeResp([b"<p>story</p>"], "text/html; charset=utf-8"),
        "https://short.example/x": _FakeResp([], "text/html", 301, "https://x.com/someone/status/1"),
        "https://short.example/x-dot": _FakeResp([], "text/html", 302, "https://X.com./someone/status/1"),
        "https://short.example/rel": _FakeResp([], "text/html", 302, "/story"),
        "https://short.example/story": _FakeResp([b"<p>relative</p>"], "text/html; charset=utf-8"),
        "https://loop.example/a": _FakeResp([], "text/html", 302, "https://loop.example/a"),
    }

    def fake_get(url, **kw):
        asked.append(url)
        return routes[url]

    monkeypatch.setattr(requests, "get", fake_get)
    assert _REAL_DEFAULT_HTTP_GET("https://short.example/a") == (200, "<p>story</p>")
    assert _REAL_DEFAULT_HTTP_GET("https://short.example/rel") == (200, "<p>relative</p>")
    asked.clear()
    assert _REAL_DEFAULT_HTTP_GET("https://short.example/x") == (0, "")
    assert _REAL_DEFAULT_HTTP_GET("https://short.example/x-dot") == (0, "")
    assert not any("x.com" in u.lower() for u in asked)             # X n'est jamais demandé
    assert _REAL_DEFAULT_HTTP_GET("https://loop.example/a") == (0, "")      # boucle : abandon
    assert sources._is_x_url("https://x.com./someone/status/1")


def test_radar_accounts_are_not_bought_twice(settings, state):
    """Un compte d'actu que le radar lit déjà n'est pas relu par l'actu : ses posts
    arrivent par le cache partagé."""
    from bot.sources import NewsHub, share_radar_posts
    from tests.fakes import FakeX, make_tweet
    settings.radar_enabled = True
    settings.radar_accounts = ["claudeai"]
    settings.x_news_accounts = ["OpenAI", "claudeai"]
    x = FakeX()
    x.ids_map = {"openai": "1", "claudeai": "2"}
    created = (now_local() - timedelta(minutes=20)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    share_radar_posts(state, "claudeai", [make_tweet("901", "Claude now has project memory for everyone.",
                                                     author_id="2", created_at=created, likes=500)], "901")
    hub = NewsHub(settings, state, x, http_get=lambda *a, **k: None)
    items = hub.collect(use_x=True)
    assert [c for c in x.calls if c[0] == "user_posts"] == [("user_posts", "1", None)]   # seul OpenAI est lu
    assert any("project memory" in i.title or "project memory" in i.summary for i in items)


def test_news_rereads_an_account_the_radar_stopped_reading(settings, state):
    from bot.sources import NewsHub
    from tests.fakes import FakeX
    settings.radar_enabled = True
    settings.radar_accounts = ["OpenAI"]
    settings.x_news_accounts = ["OpenAI"]
    x = FakeX()
    x.ids_map = {"openai": "1"}
    state.data["news_x_fetched_at"] = {"openai": (now_local() - timedelta(hours=3)).isoformat()}
    NewsHub(settings, state, x, http_get=lambda *a, **k: None).collect(use_x=True)
    assert ("user_posts", "1", None) in x.calls     # le radar n'a rien lu depuis 3 h : l'actu relit


# ---------------------------------------------------------------------------
# Configuration livrée
# ---------------------------------------------------------------------------
def test_shipped_feeds_are_well_formed():
    import json as _json
    from pathlib import Path
    shipped = _json.loads((Path(__file__).resolve().parent.parent / "config" / "settings.json").read_text())
    feeds = shipped["news_feeds"]
    assert len(feeds) >= 30 and len({f["url"] for f in feeds}) == len(feeds)
    for f in feeds:
        assert f["url"].startswith("https://") and f["name"], f
        assert 1 <= f["weight"] <= 3, f
        assert f.get("filter") in (None, "ai", "tech"), f
        assert (sources.topic_filter(f) is None) == (f.get("filter") is None), f
        assert "hn.algolia.com" not in f["url"]                 # plus de Hacker News : c'est pour les développeurs
        assert not sources._is_x_url(f["url"])                 # x.com ne se lit que par l'API
    official = [f for f in feeds if f.get("official")]
    assert 1 <= len(official) <= len(feeds) // 3                # blogs d'entreprises : une minorité
    assert len(shipped["radar_accounts"]) <= 20 and len(shipped["x_news_accounts"]) <= 10
    for handle in shipped["radar_accounts"] + shipped["x_news_accounts"]:
        assert handle.isascii() and "@" not in handle and " " not in handle


# ---------------------------------------------------------------------------
# Résumés : bio d'auteur, légende d'image, double échappement
# ---------------------------------------------------------------------------
def test_summary_prefers_the_article_over_the_author_bio(settings, state):
    """Flux façon TechRadar / Tom's Guide : chapô d'une ligne, article dans <dc:content>, et la
    bio de l'auteur (échappée deux fois) dans <content:encoded>."""
    bio = ("&amp;lt;p&amp;gt;Eric Hal Schwartz is a freelance writer for TechRadar with more than 15 years of "
           "experience covering the intersection of the world and technology.&amp;lt;/p&amp;gt;")
    body = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
            "<channel><title>F</title><link>https://example.com/</link><description>d</description><item>"
            "<title>I turned my messy handwritten notes into a polished PDF with Gemini</title>"
            "<link>https://www.techradar.com/notes</link>"
            f"<pubDate>{rfc822(2)}</pubDate>"
            "<description>Gemini can read your handwriting now</description>"
            "<dc:content><![CDATA[<p>Take a photo of a page of notes, upload it and ask for a clean PDF. "
            "It kept my headings and fixed my spelling.</p>]]></dc:content>"
            f"<content:encoded>{bio}</content:encoded>"
            "</item></channel></rss>")
    hub, _ = hub_with(settings, state, [(feed("TechRadar", "https://www.techradar.com/feeds/tag/ai"), (200, body))])
    (item,) = hub.collect()
    assert item.summary == ("Take a photo of a page of notes, upload it and ask for a clean PDF. It kept my "
                            "headings and fixed my spelling.")
    assert "freelance writer" not in item.summary and "<" not in item.summary


def test_double_escaped_tags_and_author_bios():
    assert sources.strip_html("&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;") == "Hello & welcome"
    assert sources.strip_html("5 < 7 and 9 > 3") == "5 < 7 and 9 > 3"              # pas des balises
    bio = sources._AUTHOR_BIO_RE
    assert bio.match("Eric Hal Schwartz is a freelance writer for TechRadar with 15 years of experience")
    assert bio.match("Jane O’Neil covers consumer tech for the site and writes about phones")
    assert not bio.match("Google is rolling out a new editor for Docs today")
    assert not bio.match("ChatGPT is a writer's best friend, says a new study")
    assert not bio.match("OpenAI wants to build more data centers. Sam Smith is a reporter.")


def test_image_caption_is_not_a_summary(settings, state):
    """Flux façon blog Google : <description> = légende de l'image, le vrai résumé est dans le contenu."""
    body = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" '
            'xmlns:content="http://purl.org/rss/1.0/modules/content/">'
            "<channel><title>F</title><link>https://blog.google/</link><description>d</description><item>"
            "<title>Gemini Intelligence: New features, on more devices</title>"
            "<link>https://blog.google/gemini-devices/</link>"
            f"<pubDate>{rfc822(1)}</pubDate>"
            "<description>A man speaks into a phone.</description>"
            '<content:encoded><![CDATA[<img src="https://storage.googleapis.com/x.webp">Gemini Intelligence is '
            "rolling out to more devices.]]></content:encoded>"
            "</item></channel></rss>")
    hub, _ = hub_with(settings, state, [(feed("Google Gemini", "https://blog.google/products/gemini/rss/"),
                                         (200, body))])
    assert hub.collect()[0].summary == "Gemini Intelligence is rolling out to more devices."


# ---------------------------------------------------------------------------
# Texte des articles retenus par la rédaction (article_text / fetch_article_text)
# ---------------------------------------------------------------------------
_HAND_1 = ("Engineers at ETH Zurich taught a robotic hand to walk on its fingertips, get back up after a fall "
           "and press keys on a keyboard while it balances on its other fingers.")
_HAND_2 = "The idea is to give a robot’s hand a way to reach places its body can't go & then come back on its own."
_HAND_H2 = "Ten years of practice in a single afternoon of testing"
_HAND_3 = ("The team trained the hand in a simulator for the equivalent of ten years of practice, then moved "
           "the software to the real hand in a single afternoon.")
_HAND_4 = ("“A future robot could send its hand into a space its body cannot enter,” said Robert Katzschmann, "
           "who leads the lab.")
_HAND_5 = "The hand weighs about as much as a bag of sugar and runs on a small battery."
_OTHER_STORY = ("Humanoid robots are learning to fold laundry in record time, and a start-up in Shenzhen says "
                "its machines will be in homes next year. ") * 4

ARTICLE_PAGE = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>This robotic hand walks on its fingers | Example Science</title>
<script>window.dataLayer = window.dataLayer || []; var t = "<p>Never article text, it lives in a script.</p>";</script>
<style>.x {{ color: red }}</style></head>
<body class="single single-post has-sidebar">
<header class="site-header"><nav><ul><li><a href="/">Home</a></li><li><a href="/tech">Technology</a></li></ul>
<p>Science and technology news you can trust, every single day of the week.</p></nav></header>
<div class="consent"><p>We use cookies to improve your experience and to show you personalised advertising.</p></div>
<main id="content">
<article class="post tag-robots">
  <header><h1>This robotic hand walks on its fingers</h1>
  <p>The hand can leave the robot, do a job and come back on its own, like a pet.</p></header>
  <figure><img src="/hand.jpg" alt=""><figcaption>The hand walks on three fingers while the other two keep it steady.</figcaption></figure>
  <p>Engineers at ETH Zurich taught a robotic hand to walk on its fingertips, get back up after a fall and press
  keys on a keyboard while it balances on its other fingers.</p>
  <p>The idea is to give a robot&#8217;s hand a way to reach places its body can&#39;t go &amp; then come back on&nbsp;its own.</p>
  <div class="newsletter-box"><p>Get the best science stories in your inbox every week: sign up for our free newsletter today.</p></div>
  <h2>{_HAND_H2}</h2>
  <p>The team trained the hand in a simulator for the equivalent of <strong>ten years of <em>practice</em></strong>,
  then moved the software to the <a href="/real">real hand</a> in a single afternoon.</p>
  <p>Sign up for our newsletter to get more stories like this one in your inbox.</p>
  <p><a href="/dogs">Robot dogs have learned to open doors, and nobody taught them how</a></p>
  <blockquote><p>&ldquo;A future robot could send its hand into a space its body cannot enter,&rdquo; said Robert
  Katzschmann, who leads the lab.</p></blockquote>
  <ul><li>The hand weighs about as much as a bag of sugar and runs on a small battery.</li></ul>
  <aside class="pullquote"><p>This aside repeats a quote and is not part of the running text of the article.</p></aside>
  <form action="/comment"><p>Leave a comment and tell us what you think about walking robot hands.</p></form>
  <div class="author-bio"><p>Jane Doe is a science reporter covering robotics and space for Example Science.</p></div>
  <p>Jane Doe is a science reporter covering robotics and space for Example Science since 2019.</p>
  <h2>More on robots</h2>
  <p>{_OTHER_STORY}</p>
</article>
<article class="post"><p>{_OTHER_STORY}</p></article>
</main>
<footer><p>© 2026 Example Science. All rights reserved. Terms of use and privacy policy apply.</p></footer>
<script>document.write("<p>Template text that must never be read as article text.</p>");</script>
</body></html>"""


def _page(body: str) -> str:
    return f"<!doctype html><html><head><title>T</title></head><body>{body}</body></html>"


def _para(k: int, size: int = 300) -> str:
    base = f"Paragraph {k} explains one more detail of how the robot works and why it matters to people. "
    return (base * (size // len(base) + 1))[:size].strip()


def test_article_text_keeps_the_article_and_drops_everything_around_it():
    text = sources.article_text(ARTICLE_PAGE)
    assert text.split("\n") == [_HAND_1, _HAND_2, _HAND_H2, _HAND_3, _HAND_4, _HAND_5]
    for junk in ("Never article text", "Template text", "Home", "trust", "cookies", "newsletter", "figcaption",
                 "other two keep it steady", "like a pet", "Robot dogs", "aside repeats", "Leave a comment",
                 "Jane Doe", "laundry", "©", "rights reserved"):
        assert junk not in text, junk
    assert len(text) <= sources.ARTICLE_MAX_CHARS


def test_article_text_without_article_tag_reads_the_page_paragraphs():
    page = _page(
        "<nav><p>Menu: Science, Space, Robots, Health and Technology, all in one place for you.</p></nav>"
        f"<div class='story-body'><p>{_HAND_1}</p><p>{_HAND_3}</p><p>{_HAND_4}</p><p>{_HAND_5}</p>"
        "<p>Follow us on social media and share this story with your friends and family.</p></div>"
        "<div class='related-stories'><p>Robot dogs have learned to open doors and nobody taught them.</p></div>"
        "<footer><p>All rights reserved by Example Science and its partners around the world.</p></footer>")
    assert sources.article_text(page).split("\n") == [_HAND_1, _HAND_3, _HAND_4, _HAND_5]


def test_article_text_picks_the_story_not_the_cards_around_it():
    teaser = "<article class='card'><p>Short teaser for another story, too short to be the article.</p></article>"
    hand = f"<p>{_HAND_1}</p><p>{_HAND_3}</p><p>{_HAND_4}</p>"
    # L'<article> qui contient le titre (<h1>) : même plus court que l'histoire d'à côté.
    page = _page(f"<p>{_OTHER_STORY}</p>{teaser}<article><h1>Robot hand walks</h1>{hand}</article>"
                 f"<article><p>{_OTHER_STORY}</p></article>")
    assert sources.article_text(page).split("\n") == [_HAND_1, _HAND_3, _HAND_4]
    # Sans <h1> dans un <article> : le plus long entre le texte hors <article> et le
    # premier <article> assez long — jamais l'histoire suivante parce que le premier est court.
    page = _page(f"<div class='intro'><p>{_HAND_5}</p></div>{teaser}<article>{hand}</article>"
                 f"<article><p>{_OTHER_STORY}</p></article>")
    assert sources.article_text(page).split("\n") == [_HAND_1, _HAND_3, _HAND_4]
    # L'histoire est hors <article> (« article-body ») et des cartes « à lire aussi » sont des <article>.
    page = _page(f"<h1>Robot hand walks</h1><div class='article-body'>{hand}<p>{_HAND_5}</p></div>"
                 f"<section><article><p>{_OTHER_STORY}</p></article></section>")
    assert sources.article_text(page).split("\n") == [_HAND_1, _HAND_3, _HAND_4, _HAND_5]
    # Un court <article> glissé dans le texte (carte d'une autre histoire) n'est pas lu.
    page = _page(f"<div class='intro'><p>{_HAND_1}</p></div><article><p>{_HAND_3}</p></article>"
                 f"<p>{_HAND_4}</p><p>{_HAND_5}</p><p>{_HAND_2}</p>")
    assert sources.article_text(page).split("\n") == [_HAND_1, _HAND_4, _HAND_5, _HAND_2]
    # Rien d'assez long nulle part : pas d'article (le rédacteur garde le résumé du flux).
    assert sources.article_text(_page(f"<p>{_HAND_1}</p><article><p>{_HAND_3}</p></article>")) == ""
    # « Related » glissé tout en haut de l'article ne coupe pas la lecture.
    page = _page(f"<article><h1>Robot hand walks</h1><p>{_HAND_1}</p><h3>Related</h3>"
                 f"<ul><li><a href='/x'>Robots fold laundry</a></li></ul><p>{_HAND_3}</p><p>{_HAND_4}</p>"
                 f"<p>{_HAND_5}</p></article>")
    assert sources.article_text(page).split("\n") == [_HAND_1, _HAND_3, _HAND_4, _HAND_5]


def test_article_text_min_and_max_length():
    # Moins de 400 caractères de vrai texte : pas un article (mur d'abonnement, page vide).
    paywall = _page("<article><p>Subscribe to continue reading this article about the robot hand.</p>"
                    f"<p>{_HAND_1}</p></article>")
    assert sources.article_text(paywall) == ""
    assert sources.article_text("") == "" and sources.article_text(None) == ""
    # Long : des paragraphes entiers, jamais plus que max_chars.
    page = _page("<article>" + "".join(f"<p>{_para(k)}</p>" for k in range(40)) + "</article>")
    short = sources.article_text(page, max_chars=1000)
    assert short.split("\n") == [_para(0), _para(1), _para(2)] and len(short) <= 1000
    full = sources.article_text(page)
    assert len(full) <= sources.ARTICLE_MAX_CHARS == 8000
    assert full.split("\n") == [_para(k) for k in range(26)]     # ~300 car. chacun : 26 tiennent, pas 27
    # Un seul paragraphe plus long que la limite : coupé proprement.
    huge = _page(f"<article><p>{_para(1, 3000)}</p></article>")
    cut = sources.article_text(huge, max_chars=500)
    assert len(cut) <= 500 and cut.endswith("…")


def test_article_text_entities_nested_tags_and_line_breaks():
    page = _page(
        "<article>"
        "<p>The hand&#8217;s maker &amp; its <a href='/lab'>lab <strong>at <em>ETH</em></strong> Zurich</a> say "
        "it costs &euro;5,000&nbsp;to build, &lt;cheaper&gt; than a&nbsp;used car, they told us on Monday.</p>"
        f"<p>{_HAND_1}<br>It took the team two years.<br/>It still falls over once in a while.</p>"
        f"<p>{_HAND_3}</p>"
        "</article>")
    lines = sources.article_text(page).split("\n")
    assert lines[0] == ("The hand’s maker & its lab at ETH Zurich say it costs €5,000 to build, <cheaper> than "
                        "a used car, they told us on Monday.")
    assert lines[1] == f"{_HAND_1} It took the team two years. It still falls over once in a while."
    assert lines[2] == _HAND_3


def test_article_text_unclosed_and_stray_tags_never_lose_the_text():
    # <p> jamais fermés, </span> orphelin, <div> jamais fermé : le texte est toujours là.
    page = _page(f"<article><div class='content'><p>{_HAND_1}</span><p>{_HAND_3}<p>{_HAND_4}<p>{_HAND_5}</article>")
    text = sources.article_text(page)
    for sentence in (_HAND_1, _HAND_3, _HAND_4, _HAND_5):
        assert sentence in text
    # HTML très cassé : jamais d'exception.
    for garbage in ("<<<>>>", "<!-- jamais fermé", "<p", "</p></div></article>", "<a href='", "\x00\x01"):
        assert sources.article_text(_page(garbage)) == ""
    assert sources.article_text(b"<p>bytes</p>") == ""      # type inattendu : "" (jamais d'exception)


def test_article_text_headings_noise_duplicates_and_link_lists():
    long_heading = "How a robotic hand learned to walk on its own fingers"
    page = _page(
        "<article>"
        f"<h2>{long_heading}</h2><p>{_HAND_1}</p>"
        "<h3>A second heading that is followed by another heading</h3>"
        "<h2>A third heading that comes right after the second one</h2>"
        f"<p>{_HAND_3}</p><p>{_HAND_3}</p>"                                  # doublon
        "<p>Short line.</p>"                                                 # < 40 caractères
        "<p>This article first appeared on Example Science and is republished here.</p>"
        "<p>We may earn a commission when you buy through links on our site, at no cost to you.</p>"
        f"<p>{_HAND_4}</p>"
        "<ul><li><a href='/1'>Robot dogs learn to open doors on their own</a></li>"
        "<li><a href='/2'>A drone delivers a defibrillator in four minutes</a></li></ul>"
        f"<p>{_HAND_5}</p>"
        "<h2>A heading at the very end of the article, alone</h2>"
        "</article>")
    assert sources.article_text(page).split("\n") == [
        long_heading, _HAND_1, "A third heading that comes right after the second one", _HAND_3, _HAND_4, _HAND_5]


def test_article_text_stops_at_read_next_headings_and_skips_noisy_blocks():
    page = _page(
        "<article>"
        f"<p>{_HAND_1}</p>"
        "<div class='share-buttons'><p>Share this story on X, Facebook, LinkedIn and by email with friends.</p></div>"
        "<div class='sd-sharing-enabled sharedaddy'><p>Click to share on X and Facebook and other networks.</p></div>"
        "<section id='comments'><p>Great article, I cannot wait to buy one of these robotic hands myself!</p></section>"
        "<div class='outbrain-widget'><p>You will not believe what this robot did next, number seven!</p></div>"
        "<div class='sponsored-content'><p>This content is brought to you by our partner, a robot maker.</p></div>"
        f"<p>{_HAND_3}</p><p>{_HAND_4}</p>"
        "<h3>You might also like</h3>"
        f"<p>{_OTHER_STORY}</p>"
        "</article>")
    assert sources.article_text(page).split("\n") == [_HAND_1, _HAND_3, _HAND_4]


class _RecordingGet:
    def __init__(self, result=(200, ARTICLE_PAGE)) -> None:
        self.result = result
        self.calls: list[tuple] = []

    def __call__(self, url, *args, **kwargs):
        self.calls.append((url, args, kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.mark.parametrize("url", [
    "https://x.com/OpenAI/status/1900000000000000000",
    "https://twitter.com/OpenAI/status/1",
    "https://mobile.twitter.com/OpenAI/status/1",
    "https://www.x.com/sama/status/1",
    "HTTPS://X.COM/OpenAI",
    "https://user@x.com/OpenAI/status/1",
    "https://x.com:443/OpenAI",
    "https://t.co/abc123",
    "https://nitter.net/OpenAI/status/1",
    "",
    None,
    "ftp://news.example/story",
    "javascript:alert(1)",
    "//news.example/story",
    "news.example/story",
    "file:///etc/hostname",
])
def test_fetch_article_text_never_reads_x_or_non_http_urls(url, monkeypatch):
    custom = _RecordingGet()
    default = _RecordingGet()
    monkeypatch.setattr(sources, "default_http_get", default)
    assert sources.fetch_article_text(url, http_get=custom) == ""
    assert sources.fetch_article_text(url) == ""
    assert custom.calls == [] and default.calls == []


def test_fetch_article_text_reads_the_page_with_the_given_getter(monkeypatch):
    default = _RecordingGet()
    monkeypatch.setattr(sources, "default_http_get", default)
    custom = _RecordingGet()
    text = sources.fetch_article_text("  https://www.sciencealert.com/robot-hand  ", http_get=custom)
    assert text.startswith(_HAND_1) and "laundry" not in text
    assert custom.calls == [("https://www.sciencealert.com/robot-hand", (), {})]
    assert default.calls == []
    # Un hôte qui finit par « x.com » sans en être un sous-domaine n'est pas X.
    assert sources.fetch_article_text("https://fixx.com/story", http_get=custom).startswith(_HAND_1)


def test_fetch_article_text_default_getter_asks_for_html(monkeypatch):
    calls: list[tuple] = []

    def fake_default(url, accept=sources.FEED_ACCEPT):
        calls.append((url, accept))
        return 200, ARTICLE_PAGE

    monkeypatch.setattr(sources, "default_http_get", fake_default)
    assert sources.fetch_article_text("https://news.example/robot-hand").startswith(_HAND_1)
    assert calls == [("https://news.example/robot-hand", sources.HTML_ACCEPT)]


@pytest.mark.parametrize("result", [
    (404, ARTICLE_PAGE),
    (403, ARTICLE_PAGE),
    (500, ""),
    (301, ARTICLE_PAGE),
    ConnectionError("connexion refusée"),
    TimeoutError("trop lent"),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    RuntimeError("bug du lecteur"),
    None,                                    # réponse inattendue
    (200, None),
    (200, "<html><body><div id='app'></div><script>render()</script></body></html>"),   # page chargée après coup
])
def test_fetch_article_text_never_raises(result):
    assert sources.fetch_article_text("https://news.example/story", http_get=_RecordingGet(result)) == ""


# ---------------------------------------------------------------------------
# Défauts connus (xfail strict) : à corriger dans bot/sources.py, puis retirer le marqueur
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("title", [
    "Disney signs $1 billion deal with OpenAI to bring Mickey to Sora",
    "Apple strikes a deal with Google to put Gemini inside Siri",
    "World's first flying car goes on sale for $300,000",
    "China's cheapest humanoid robot costs less than an iPhone",
    "Show HN: An open-source LLM router that picks the cheapest model",
])
def test_shopping_filter_misfires_on_business_and_launch_titles(settings, state, title):
    hub = NewsHub(settings, state, http_get=FakeHttp())
    assert hub._blocked_reason(news_item(title)) is None


@pytest.mark.parametrize("title, summary", [
    ("Google's new AI feature helps combat phone scams", ""),
    ("Meta launches new tools to combat deepfakes", ""),
    ("Startup unveils a robot that folds your laundry", "The company was founded by two Apple veterans."),
    ("An army of humanoid robots is coming to car factories", ""),
    ("Naval Ravikant says AI will make everyone a founder", ""),
])
def test_military_filter_misfires_on_everyday_words(settings, state, title, summary):
    hub = NewsHub(settings, state, http_get=FakeHttp())
    assert hub._blocked_reason(news_item(title, summary)) is None


def test_wow_words_accept_the_typographic_apostrophe():
    straight = sources.audience_factor(news_item("World's first AI-designed drug enters human trials"))
    curly = sources.audience_factor(news_item("World’s first AI-designed drug enters human trials"))
    assert straight == pytest.approx(W)
    assert curly == pytest.approx(W)


def test_hn_generated_summary_is_not_a_wow_word():
    hits = {"hits": [{"title": "Notes from our database migration", "url": "https://openai.com/index/db-notes/",
                      "points": 150, "num_comments": 80, "created_at_i": int(NOW_UTC.timestamp()),
                      "objectID": "7"}]}
    (item,) = parse_hn(json.dumps(hits), feed("Hacker News", "https://hn.algolia.com/api/v1/search", 2, type_="hn"))
    assert item.summary.startswith("Trending on Hacker News")
    assert sources.audience_factor(item) == pytest.approx(1.0)


@pytest.mark.parametrize("title, expected", [
    ("Tesla shares video of its Optimus robot folding laundry", W),
    ("New study raises concerns about AI toys for kids", 1.0),
])
def test_insider_words_misfire_on_common_verbs(title, expected):
    assert sources.audience_factor(news_item(title)) == pytest.approx(expected)


def test_article_noise_filter_keeps_short_factual_paragraphs():
    ftc = "The Federal Trade Commission opened an investigation into the company on Tuesday."
    eu = "The European Commission fined Apple 500 million euros for blocking cheaper offers."
    subs = "ChatGPT now has 800 million weekly users and 20 million paying subscribers."
    page = _page(f"<article><p>{_HAND_1}</p><p>{ftc}</p><p>{eu}</p><p>{subs}</p><p>{_HAND_3}</p>"
                 f"<p>{_HAND_4}</p></article>")
    text = sources.article_text(page)
    assert _HAND_1 in text                                     # l'article est bien lu…
    assert ftc in text and eu in text and subs in text        # … mais ses faits courts disparaissent


@pytest.mark.parametrize("quote", [
    "<blockquote><p>A future robot could send its hand into a space its body cannot enter.</p>"
    "<p>Then it could take it back afterwards.</p></blockquote>",
    "<ul><li><p>A future robot could send its hand into a space its body cannot enter.</p>"
    "<p>Then it could take it back afterwards.</p></li></ul>",
    "<p>A future robot could send its hand into a space its body cannot enter."         # </p> omis (HTML5)
    "<p>Then it could take it back afterwards.",
])
def test_article_text_does_not_glue_nested_paragraphs(quote):
    page = _page(f"<article><p>{_HAND_1}</p>{quote}<p>{_HAND_3}</p><p>{_HAND_4}</p></article>")
    text = sources.article_text(page)
    assert "cannot enter." in text and "Then it could take it back" in text
    assert "cannot enter.Then" not in text


def test_article_text_omitted_p_end_tags_still_stop_at_read_next():
    page = _page(f"<article><p>{_HAND_1}<p>{_HAND_3}<p>{_HAND_4}<h2>More on robots</h2>"
                 f"<p>{_OTHER_STORY}</article>")
    text = sources.article_text(page)
    assert _HAND_1 in text
    assert "laundry" not in text


def test_article_text_reads_a_wordpress_post_div():
    body = f"<h1>Hand</h1><p>{_HAND_1}</p><p>{_HAND_3}</p><p>{_HAND_4}</p>"
    untagged = _page(f'<div id="post-123" class="post-123 post type-post status-publish hentry">{body}</div>')
    assert sources.article_text(untagged).startswith(_HAND_1)              # témoin : sans étiquette, lu
    page = _page('<div id="post-123" class="post-123 post type-post status-publish hentry category-science '
                 f'tag-robots tag-zurich">{body}</div>')
    assert sources.article_text(page).startswith(_HAND_1)


def test_top_for_news_caps_each_source_across_fresh_and_rest():
    fresh = [_story(f"s-fresh-{k}", "S", 2, appeal=1.0) for k in range(3)]
    rest = [_story(f"s-old-{k}", "S", 30, appeal=20.0) for k in range(3)]
    rest += [_story(f"v-old-{k}", "V", 30, appeal=4.0) for k in range(3)]
    rest += [_story(f"w-old-{k}", "W", 30, appeal=2.0) for k in range(3)]
    picked = top_for("news", fresh + rest, 9)                  # max(3, 9 // 8) = 3 par source
    assert len(picked) == 9
    assert sum(i.source == "S" for i in picked) <= 3
    assert sum(i.source == "W" for i in picked) == 3


def test_top_for_reactive_ranks_by_score_freshness_included():
    # Docstring : « classé par score (fraîcheur comprise) » — une actu fraîche mais très
    # forte peut passer devant une actu plus récente et plus faible.
    older_heavy = _story("older-heavy", "A", 8, appeal=3.0)
    newest = _story("newest", "B", 1, appeal=1.0)
    assert _titles(top_for("reactive", [older_heavy, newest], 5)) == ["older-heavy", "newest"]
    assert "classé par score (fraîcheur comprise)" in top_for.__doc__


@pytest.mark.parametrize("raw, expected", [
    ("Kids who use AI tutors keep reading longer.", "Kids who use AI tutors keep reading longer."),
    ("The study is out today; you can read more about it in Nature.",
     "The study is out today; you can read more about it in Nature."),
    ("The post office will test delivery drones in rural Wales this winter. "
     "<p>The post <a href='https://e.com/d'>Drones deliver mail</a> appeared first on <a>Example</a>.</p>",
     "The post office will test delivery drones in rural Wales this winter."),
])
def test_summary_boilerplate_keeps_real_sentences(raw, expected):
    assert sources.clean_summary(raw) == expected


@pytest.mark.parametrize("text", [
    "Bruno Mars announces a world tour",
    "Lunar New Year celebrations begin across Asia",
    "Texas Tech beats Baylor in overtime",
    "Breast implants linked to a rare cancer",
])
def test_tech_filter_misfires_on_unrelated_topics(text):
    assert not sources.topic_filter({"filter": "tech"})(text)



# ---------------------------------------------------------------------------
# Deuxième relecture : l'article du titre ou rien ; bons plans ; marine ; puces ; flux lent seul
# ---------------------------------------------------------------------------
def test_article_text_never_falls_back_on_the_next_story():
    short_main = "<article><h1>Optimus serves popcorn</h1><p>" + _HAND_1 + "</p></article>"
    nxt = "<article>" + "".join(f"<p>{_OTHER_STORY}</p>" for _ in range(2)) + "</article>"
    assert sources.article_text(_page(short_main + nxt)) == ""            # le résumé du flux restera
    in_header = ("<article><header><h1>Optimus serves popcorn</h1></header><p>" + _HAND_1 + "</p></article>")
    assert sources.article_text(_page(in_header + nxt)) == ""
    # Le texte de l'histoire dans un bloc au nom trompeur, titre hors <article> : c'est lui.
    noisy = (f"<h1>Robot hand walks</h1><div class='entry-content sharing-enabled'><p>{_HAND_1}</p>"
             f"<p>{_HAND_3}</p><p>{_HAND_4}</p><p>{_HAND_5}</p></div>{nxt}")
    assert sources.article_text(_page(noisy)).split("\n") == [_HAND_1, _HAND_3, _HAND_4, _HAND_5]


@pytest.mark.parametrize("title", [
    "Waymo strikes deals for robotaxis in three new cities",
    "Nvidia signs deals on humanoid robots with five carmakers",
    "Google's AI could save 40% of the energy used to cool data centres",
])
def test_business_deals_and_savings_are_not_shopping(settings, state, title):
    hub, _ = hub_with(settings, state, [])
    assert hub._blocked_reason(news_item(title)) is None


def test_promo_savings_are_still_slowed_down():
    promo = NewsItem(id="p", title="A quiet update for a note-taking app", summary="Save 40% on the annual plan.",
                     url="https://e.com/p", source="E", published=None)
    assert sources.audience_factor(promo) == pytest.approx(0.3)
    deal = NewsItem(id="d", title="Best robot vacuum deals this weekend", summary="", url="https://e.com/d",
                    source="E", published=None)
    assert sources._SHOPPING_BLOCK_RE.search(deal.title)


@pytest.mark.parametrize("text, blocked", [
    ("The Navy is testing robot boats", True),
    ("Navy ships get an AI copilot", True),
    ("The new Pixel comes in navy and white", False),
    ("A navy jacket that charges your phone", False),
    ("Navy blue is this year's colour for smartwatches", False),
])
def test_navy_means_the_military_not_the_colour(text, blocked):
    from bot import filters
    assert bool(filters.military_term(text)) is blocked


@pytest.mark.parametrize("title", [
    "Apple's M5 chip makes the new MacBook twice as fast",
    "The new iPad can now run desktop apps",
    "Vision Pro gets a cheaper sibling",
])
def test_tech_filter_knows_chips_and_apple_devices(title):
    assert sources.topic_filter({"filter": "tech"})(title)


def test_one_slow_feed_alone_never_blocks_collect(settings, state, monkeypatch):
    import time as _time
    feed = {"name": "Slow", "url": "https://slow.example/feed", "weight": 1}
    settings.news_feeds = [feed]
    hub = sources.NewsHub(settings, state, http_get=lambda url: (_time.sleep(2), (200, ""))[1])
    monkeypatch.setattr(sources, "FEEDS_MAX_SECONDS", 0.2)
    t0 = _time.monotonic()
    hub.collect()
    assert _time.monotonic() - t0 < 1.5
    assert hub.last_stats["failed_feeds"] == ["Slow"]
