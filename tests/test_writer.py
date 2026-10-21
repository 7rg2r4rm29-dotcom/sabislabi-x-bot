"""Tests du Writer (bot/writer.py) : aucun appel réseau, Claude simulé par FakeLLM."""
from __future__ import annotations

import random
from datetime import timedelta

import pytest

from bot import writer as wmod
from bot.models import Draft, NewsItem, Visual
from bot.utils import iso, now_local
from bot.writer import Writer
from tests.fakes import FakeLLM


# ---------------------------------------------------------------------------
# Aides
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _frozen(clock):
    """Horloge figée au jeudi 15/10/2026 10:00 (Paris) pour tous les tests."""
    return clock


def news(n: int, title: str, summary: str = "", url: str | None = None, source: str = "OpenAI",
         hours: float | None = 2.0, official: bool = True, kind: str = "rss", engagement: int = 0) -> NewsItem:
    return NewsItem(
        id=f"id{n}", title=title, summary=summary, url=url or f"https://openai.com/index/news-{n}",
        source=source, published=(now_local() - timedelta(hours=hours)) if hours is not None else None,
        official=official, kind=kind, engagement=engagement,
    )


@pytest.fixture
def items():
    return [
        news(1, "Voice mode is now available to all ChatGPT users",
             "OpenAI says voice conversations no longer require a paid plan.",
             url="https://openai.com/index/voice-for-everyone"),
        news(2, "Why most AI agent demos break in production",
             "A long essay on reliability problems when chaining tools together.",
             url="https://someblog.dev/agents-break", source="Hacker News", official=False, kind="hn",
             engagement=420),
        news(3, "Claude can now build small apps inside the chat",
             "Anthropic announced app building directly in conversations.",
             url="https://x.com/claudeai/status/123", source="@claudeai", kind="x", engagement=900),
    ]


def make_writer(settings, state, responses=None, seed: int = 1) -> tuple[Writer, FakeLLM]:
    llm = FakeLLM(responses or {})
    w = Writer(settings, llm, state)
    w.rng = random.Random(seed)
    return w, llm


def post_resp(text: str, source_ids=None, used_note_ids=None, visual_url: str = "", image_prompt: str = "",
              poll_options=None, reason: str = "raison") -> dict:
    return {"text": text, "source_ids": list(source_ids or []), "used_note_ids": list(used_note_ids or []),
            "visual_url": visual_url, "image_prompt": image_prompt,
            "poll_options": list(poll_options or []), "reason": reason}


FC_OK = {"ok": True, "issues": ""}

NEWS_TEXT = ("free voice mode changes who practices hard conversations.\n"
             "try this tonight: rehearse your salary talk out loud and ask it to push back like a tough manager.")
NEWS_TEXT_2 = ("the best use of free voice mode isn't search.\n"
               "it's rehearsal: ask it to play a difficult client and interrupt you. you'll find your weak spots.")
TIP_TEXT = ('before you trust an AI answer, ask it: "what would make this answer wrong?"\n'
            "you'll catch more mistakes than you think.")
TIP_TEXT_2 = ('stop asking AI to "make this better".\n'
              'paste your draft and ask: "list the weakest sentences and rewrite only those".')


def thread_tweets(genre: str, n: int = 5) -> list[str]:
    hooks = {
        "beginner": "most people use ChatGPT like a search box. snap your fridge and ask what to cook tonight.",
        "timely": "free voice mode means anyone can rehearse a hard conversation tonight. here is the method.",
        "tutorial": "turn any long report into the decisions it asks you to make, with one prompt.",
        "practical": "the boring tasks you repeat every week are the best place to start with AI.",
        "mindset": "the day you stop waiting for the perfect plan is the day you start learning.",
        "story": "the founder had one week of cash left when the first real customer called.",
    }
    body = [
        f"step {w}: open the chat, paste your material and say what decision you need from it."
        for w in ("one", "two", "three", "four", "five", "six", "seven", "eight")
    ]
    return [hooks.get(genre, f"{genre} hook")] + [f"{genre} — {b}" for b in body[: n - 1]]


def thread_resp(genre: str, n: int = 5, source_ids=None, **extra) -> dict:
    obj = {"tweets": thread_tweets(genre, n), "source_ids": list(source_ids or []), "used_note_ids": [],
           "visual_url": "", "image_prompt": "", "reason": f"raison {genre}"}
    obj.update(extra)
    return obj


def thread_responder(per_genre: dict | None = None):
    per_genre = per_genre or {}

    def _resp(label, system, user, schema):
        genre = label.split("-", 1)[1]
        if genre in per_genre:
            v = per_genre[genre]
            if isinstance(v, list):
                return v.pop(0) if v else None
            return v
        return thread_resp(genre, source_ids=[1] if genre == "timely" else [])
    return _resp


# ---------------------------------------------------------------------------
# Posts d'actu : ancrage + vérification factuelle
# ---------------------------------------------------------------------------
class TestNewsGrounding:
    def test_grounded_news_post_is_factchecked(self, settings, state, items):
        w, llm = make_writer(settings, state, {
            "post-news": post_resp(NEWS_TEXT, source_ids=[1], reason="voix gratuite pour tous"),
            "factcheck": FC_OK,
        })
        d = w.draft_post("news", items, [], "")
        assert isinstance(d, Draft)
        assert d.kind == "news" and d.texts == [NEWS_TEXT]
        assert d.sources == [items[0]]
        assert d.reason == "voix gratuite pour tous"
        assert d.poll is None and not d.from_fallback
        assert llm.labels() == ["post-news", "factcheck"]
        user = llm.calls[0]["user"]
        assert "[1] OpenAI · OFFICIAL" in user and "[2] Hacker News" in user
        assert "420 points on Hacker News" in user and "900 likes on X" in user
        assert "a post on X: never use it as a screenshot" in user
        fc = llm.calls[1]
        assert NEWS_TEXT in fc["user"] and items[0].title in fc["user"]
        assert items[1].title not in fc["user"]          # seules les actus citées
        assert set(fc["schema"]["properties"]) == {"ok", "issues"}

    def test_factcheck_failure_retries_with_issues(self, settings, state, items):
        w, llm = make_writer(settings, state, {
            "post-news": [post_resp(NEWS_TEXT, source_ids=[1]), post_resp(NEWS_TEXT_2, source_ids=[1])],
            "factcheck": [{"ok": False, "issues": "the item never mentions salary talks being faster"}, FC_OK],
        })
        d = w.draft_post("news", items, [], "")
        assert d is not None and d.texts == [NEWS_TEXT_2]
        assert llm.labels() == ["post-news", "factcheck", "post-news", "factcheck"]
        retry_user = llm.calls[2]["user"]
        assert "fact-checker" in retry_user
        assert "the item never mentions salary talks being faster" in retry_user
        assert NEWS_TEXT.splitlines()[0] in retry_user      # le brouillon refusé est montré

    def test_factcheck_failing_twice_returns_none(self, settings, state, items):
        w, llm = make_writer(settings, state, {
            "post-news": post_resp(NEWS_TEXT, source_ids=[1]),
            "factcheck": {"ok": False, "issues": "unsupported claim"},
        })
        assert w.draft_post("news", items, [], "") is None
        assert llm.labels() == ["post-news", "factcheck", "post-news", "factcheck"]

    def test_factcheck_unavailable_returns_none_without_retry(self, settings, state, items):
        w, llm = make_writer(settings, state, {"post-news": post_resp(NEWS_TEXT, source_ids=[1])})
        assert w.draft_post("news", items, [], "") is None
        assert llm.labels() == ["post-news", "factcheck"]

    def test_news_without_citation_is_retried(self, settings, state, items):
        w, llm = make_writer(settings, state, {
            "post-news": [post_resp(NEWS_TEXT, source_ids=[]), post_resp(NEWS_TEXT_2, source_ids=[1])],
            "factcheck": FC_OK,
        })
        d = w.draft_post("news", items, [], "")
        assert d is not None and d.texts == [NEWS_TEXT_2] and d.sources == [items[0]]
        assert llm.labels() == ["post-news", "post-news", "factcheck"]
        assert "source_ids" in llm.calls[1]["user"]

    def test_news_without_items_is_still_factchecked(self, settings, state):
        w, llm = make_writer(settings, state, {"post-news": post_resp(TIP_TEXT), "factcheck": FC_OK})
        d = w.draft_post("news", [], [], "")
        assert d is not None and d.sources == []
        assert llm.labels() == ["post-news", "factcheck"]
        assert "no news items could be collected" in llm.calls[0]["user"]
        assert "no item cited" in llm.calls[1]["user"]

    def test_out_of_range_source_ids_ignored(self, settings, state, items):
        w, llm = make_writer(settings, state, {
            "post-news": post_resp(NEWS_TEXT, source_ids=[9, 0, "x", True, 2, 2]), "factcheck": FC_OK})
        d = w.draft_post("news", items, [], "")
        assert d is not None and d.sources == [items[1]]

    def test_tip_without_sources_is_still_factchecked(self, settings, state, items):
        w, llm = make_writer(settings, state, {"post-tip": post_resp(TIP_TEXT), "factcheck": FC_OK})
        d = w.draft_post("tip", items, [], "")
        assert d is not None and d.kind == "tip" and d.texts == [TIP_TEXT]
        assert llm.labels() == ["post-tip", "factcheck"]
        assert "relis bien" not in d.reason

    def test_unsourced_tip_with_factcheck_down_is_flagged_not_dropped(self, settings, state, items):
        w, llm = make_writer(settings, state, {"post-tip": post_resp(TIP_TEXT)})
        d = w.draft_post("tip", items, [], "")
        assert d is not None and "vérification des faits indisponible" in d.reason

    def test_invented_personal_story_in_tip_is_rejected(self, settings, state, items):
        w, llm = make_writer(settings, state, {
            "post-tip": [post_resp("yesterday I sorted 400 client emails in 5 min with Claude. here's how."),
                         post_resp(TIP_TEXT)],
            "factcheck": [{"ok": False, "issues": "personal claim not in notes"}, FC_OK]})
        d = w.draft_post("tip", items, [], "")
        assert d is not None and d.texts == [TIP_TEXT]

    def test_tip_citing_a_source_is_factchecked(self, settings, state, items):
        w, llm = make_writer(settings, state, {"post-tip": post_resp(NEWS_TEXT, source_ids=[1]),
                                               "factcheck": FC_OK})
        d = w.draft_post("tip", items, [], "")
        assert d is not None and d.sources == [items[0]]
        assert llm.labels() == ["post-tip", "factcheck"]

    def test_off_limits_items_never_shown(self, settings, state, items):
        settings.blacklist_keywords = ["secretcorp"]
        bad = [news(7, "Bitcoin jumps after ETF news", "crypto rally"),
               news(8, "SecretCorp launches a chatbot", "a launch")]
        w, llm = make_writer(settings, state, {"post-tip": post_resp(TIP_TEXT)})
        w.draft_post("tip", bad + items, [], "")
        user = llm.calls[0]["user"]
        assert "Bitcoin" not in user and "SecretCorp" not in user
        assert items[0].title in user


# ---------------------------------------------------------------------------
# Filtres, nouvel essai, anti-répétition
# ---------------------------------------------------------------------------
class TestFiltersAndRetry:
    @pytest.mark.parametrize("bad", [
        "try this in claude.ai today: paste your draft and ask for the weakest sentences",
        "full guide here https://example.com/guide — paste your draft and ask for the weakest part",
        "open www.perplexity.ai and ask it to compare three sources before you decide",
    ])
    def test_url_or_domain_rejected_then_retry(self, settings, state, bad):
        w, llm = make_writer(settings, state, {"post-tip": [post_resp(bad), post_resp(TIP_TEXT)]})
        d = w.draft_post("tip", [], [], "")
        assert d is not None and d.texts == [TIP_TEXT]
        assert llm.labels() == ["post-tip", "post-tip", "factcheck"]
        assert "domain name" in llm.calls[1]["user"]
        assert "REJECTED" in llm.calls[1]["user"]

    def test_url_twice_returns_none(self, settings, state):
        w, llm = make_writer(settings, state, {"post-tip": post_resp("go to chatgpt.com and paste it")})
        assert w.draft_post("tip", [], [], "") is None
        assert llm.labels() == ["post-tip", "post-tip"]

    def test_hashtags_and_mentions_removed(self, settings, state):
        w, _ = make_writer(settings, state, {"post-tip": post_resp(
            '#AI tip: ask @OpenAI models "what would make this wrong?" before you trust the answer')})
        d = w.draft_post("tip", [], [], "")
        assert d is not None
        assert "#" not in d.texts[0] and "@" not in d.texts[0]

    def test_weighted_length_counts_emojis_double(self, settings, state):
        long_text = "x" * 255 + "🚀🚀🚀"           # 258 caractères, 261 pondérés
        assert len(long_text) < 260
        w, llm = make_writer(settings, state, {"post-tip": [post_resp(long_text), post_resp(TIP_TEXT)]})
        d = w.draft_post("tip", [], [], "")
        assert d is not None and d.texts == [TIP_TEXT]
        assert "261 weighted characters, max 260" in llm.calls[1]["user"]

    def test_off_limits_text_rejected(self, settings, state):
        w, llm = make_writer(settings, state, {"post-tip": [
            post_resp("AI is the new crypto: everyone is early and nobody reads the docs"),
            post_resp(TIP_TEXT)]})
        d = w.draft_post("tip", [], [], "")
        assert d is not None and d.texts == [TIP_TEXT]
        assert "off-limits" in llm.calls[1]["user"]

    def test_anti_repetition_against_recent_posts(self, settings, state):
        state.remember_text("tip", TIP_TEXT)
        near_copy = TIP_TEXT.replace("think.", "think!")
        w, llm = make_writer(settings, state, {"post-tip": [post_resp(near_copy), post_resp(TIP_TEXT_2)]})
        d = w.draft_post("tip", [], [], "")
        assert d is not None and d.texts == [TIP_TEXT_2]
        assert "too similar to a recent post" in llm.calls[1]["user"]
        assert TIP_TEXT.splitlines()[0][:40] in llm.calls[0]["user"]   # posts récents montrés

    def test_anti_repetition_threshold_is_0_6(self, settings, state):
        from bot.filters import _normalize_words
        state.remember_text("tip", TIP_TEXT)
        partial = "before you trust AI answers, ask what would break them"
        cand = _normalize_words(partial)
        ratio = len(cand & _normalize_words(TIP_TEXT)) / len(cand)
        assert 0.6 < ratio <= 0.7                     # refusé à 0,6, accepté au seuil par défaut 0,7
        w, llm = make_writer(settings, state, {"post-tip": [post_resp(partial), post_resp(TIP_TEXT_2)]})
        d = w.draft_post("tip", [], [], "")
        assert d is not None and d.texts == [TIP_TEXT_2]
        assert "too similar to a recent post" in llm.calls[1]["user"]

    def test_malformed_output_types(self, settings, state):
        w, llm = make_writer(settings, state, {"post-tip": {"text": 123, "source_ids": "x",
                                                            "used_note_ids": None}})
        assert w.draft_post("tip", [], [], "") is None
        assert llm.labels() == ["post-tip", "post-tip"]

    def test_json_leak_rejected(self, settings, state):
        w, _ = make_writer(settings, state, {"post-tip": [post_resp('{"text": "hello"}'), post_resp(TIP_TEXT)]})
        d = w.draft_post("tip", [], [], "")
        assert d is not None and d.texts == [TIP_TEXT]

    def test_llm_none_and_exception(self, settings, state):
        w, llm = make_writer(settings, state, {})
        assert w.draft_post("tip", [], [], "") is None
        assert llm.labels() == ["post-tip"]          # pas de second essai inutile

        def boom(*a):
            raise RuntimeError("network down")
        w2, _ = make_writer(settings, state, {"post-": boom, "thread-": boom, "reactive": boom,
                                               "comment-": boom, "radar-": boom, "visual-": boom})
        assert w2.draft_post("news", [], [], "") is None
        assert w2.draft_thread([], [], "") is None
        assert w2.draft_reactive([news(1, "Fresh launch", "something new")], [], "") is None
        assert w2.draft_comment_reply("my post", "bob", "how do I do this with Claude?") is None
        assert w2.draft_radar_reply("sama", "a long enough post about building with AI tools", 5, 50) is None
        assert w2.visual_matches("post", "https://openai.com/x", "some visible text on the page here") == \
            (False, "vérification indisponible")

    def test_unknown_kind(self, settings, state):
        w, llm = make_writer(settings, state, {"post-": post_resp(TIP_TEXT)})
        assert w.draft_post("meme", [], [], "") is None
        assert llm.calls == []

    def test_effort_default_for_posts(self, settings, state, items):
        w, llm = make_writer(settings, state, {"post-news": post_resp(NEWS_TEXT, source_ids=[1]),
                                               "factcheck": FC_OK})
        w.draft_post("news", items, [], "")
        assert [c["effort"] for c in llm.calls] == [None, None]

    def test_schemas_are_strict(self, settings, state, items):
        w, llm = make_writer(settings, state, {"post-news": post_resp(NEWS_TEXT, source_ids=[1]),
                                               "factcheck": FC_OK, "post-tip": post_resp(TIP_TEXT),
                                               "post-gm": post_resp("gm. what's one task you'd hand to AI today?")})
        w.draft_post("news", items, [], "")
        w.draft_post("tip", items, [], "")
        w.draft_post("gm", items, [], "")
        by_label = {c["label"]: c["schema"] for c in llm.calls}
        for sch in by_label.values():
            assert sch["additionalProperties"] is False
            assert set(sch["required"]) == set(sch["properties"])
        assert "poll_options" not in by_label["post-news"]["properties"]
        assert "poll_options" in by_label["post-tip"]["properties"]
        gm_props = by_label["post-gm"]["properties"]
        assert "poll_options" in gm_props and "used_note_ids" in gm_props
        assert "visual_url" not in gm_props and "source_ids" not in gm_props


# ---------------------------------------------------------------------------
# Post GM
# ---------------------------------------------------------------------------
class TestGm:
    def test_gm_requires_the_word_gm(self, settings, state, items):
        w, llm = make_writer(settings, state, {"post-gm": [
            post_resp("good morning builders, what are you shipping today?"),
            post_resp("gm. what's one boring task you'd hand to AI today if you trusted it?")]})
        d = w.draft_post("gm", items, [], "")
        assert d is not None and d.texts[0].startswith("gm.")
        assert d.visual is None and d.sources == []
        assert '"gm"' in llm.calls[1]["user"]
        assert items[0].title not in llm.calls[0]["user"]          # pas d'actu dans le gm
        assert llm.calls[0]["system"] == wmod.GM_SYSTEM_PROMPT

    def test_gm_max_200(self, settings, state):
        long_gm = "gm. " + "a little idea about learning in public " * 6
        assert 200 < len(long_gm) <= 260
        w, llm = make_writer(settings, state, {"post-gm": post_resp(long_gm)})
        assert w.draft_post("gm", [], [], "") is None
        assert "max 200" in llm.calls[1]["user"]

    def test_gm_shows_recent_gm_posts(self, settings, state):
        state.remember_text("gm", "gm. coffee first, then the hard task")
        w, llm = make_writer(settings, state, {"post-gm": post_resp("gm. which tab do you open first at work?")})
        assert w.draft_post("gm", [], [], "") is not None
        assert "coffee first, then the hard task" in llm.calls[0]["user"]


# ---------------------------------------------------------------------------
# Sondages
# ---------------------------------------------------------------------------
class TestPolls:
    POLL_TEXT = "quick one: where do you draft your emails these days?"

    def test_poll_returned_without_visual(self, settings, state):
        w, llm = make_writer(settings, state, {"post-tip": post_resp(
            self.POLL_TEXT, poll_options=["Claude", "ChatGPT", "Gemini", "by hand"],
            visual_url="https://openai.com/chatgpt")})
        d = w.draft_post("tip", [], [], "")
        assert d.poll == {"options": ["Claude", "ChatGPT", "Gemini", "by hand"], "duration_minutes": 1440}
        assert d.visual is None
        assert state.data.get("poll_dates", []) == []   # compté à la publication, pas au brouillon
        assert "POLL (optional)" in llm.calls[0]["user"]

    def test_gm_poll(self, settings, state):
        w, _ = make_writer(settings, state, {"post-gm": post_resp(
            "gm. emails this morning: who writes yours?", poll_options=["Claude", "ChatGPT"])})
        d = w.draft_post("gm", [], [], "")
        assert d.poll and d.poll["options"] == ["Claude", "ChatGPT"]

    def test_poll_limit_two_per_rolling_week(self, settings, state):
        now = now_local()
        state.data["poll_dates"] = [iso(now - timedelta(days=1)), iso(now - timedelta(days=6))]
        w, llm = make_writer(settings, state, {"post-tip": post_resp(
            self.POLL_TEXT, poll_options=["Claude", "ChatGPT"], visual_url="https://openai.com/chatgpt")})
        assert w.polls_left() == 0
        d = w.draft_post("tip", [], [], "")
        assert d is not None and d.poll is None
        assert d.visual == Visual("screenshot", "https://openai.com/chatgpt")
        assert "POLL: not available" in llm.calls[0]["user"]
        assert len(state.data["poll_dates"]) == 2

    def test_old_polls_do_not_count(self, settings, state):
        now = now_local()
        state.data["poll_dates"] = [iso(now - timedelta(days=8)), iso(now - timedelta(days=10)),
                                    "2026-10-14", "garbage", 42]
        w, _ = make_writer(settings, state, {"post-tip": post_resp(self.POLL_TEXT,
                                                                   poll_options=["Claude", "ChatGPT"])})
        assert w.polls_left() == 1                          # seul le 14/10 compte
        d = w.draft_post("tip", [], [], "")
        assert d.poll is not None
        state.new_item("tip", d.texts, status="awaiting", poll=d.poll)   # carte en attente
        assert w.polls_left() == 0

    def test_two_polls_then_blocked(self, settings, state):
        texts = [self.POLL_TEXT, "honest question: which assistant writes your meeting notes?",
                 "which tool do you open first when a client email lands?"]
        w, llm = make_writer(settings, state, {"post-tip": [post_resp(t, poll_options=["Claude", "ChatGPT"])
                                                            for t in texts]})
        for _ in range(2):
            d = w.draft_post("tip", [], [], "")
            assert d.poll is not None
            state.new_item("tip", d.texts, status="approved", poll=d.poll)
        third = w.draft_post("tip", [], [], "")
        assert third.poll is None
        assert "POLL: not available" in [c for c in llm.calls if c["label"] == "post-tip"][-1]["user"]

    def test_rejected_poll_draft_does_not_use_the_quota(self, settings, state):
        w, _ = make_writer(settings, state, {"post-tip": post_resp(self.POLL_TEXT,
                                                                   poll_options=["Claude", "ChatGPT"])})
        d = w.draft_post("tip", [], [], "")
        state.new_item("tip", d.texts, status="rejected", poll=d.poll)
        assert w.polls_left() == 2

    @pytest.mark.parametrize("opts", [
        ["only one"],
        ["Claude", "an option that is far too long for X"],
        ["a", "b", "c", "d", "e"],
        ["Claude", "claude"],                                # doublon -> une seule option
        ["Claude", "chatgpt.com"],
    ])
    def test_invalid_poll_options_ignored(self, settings, state, opts):
        w, _ = make_writer(settings, state, {"post-tip": post_resp(self.POLL_TEXT, poll_options=opts)})
        d = w.draft_post("tip", [], [], "")
        assert d is not None and d.poll is None
        assert state.data.get("poll_dates", []) == []

    def test_news_never_has_poll(self, settings, state, items):
        w, _ = make_writer(settings, state, {"post-news": post_resp(NEWS_TEXT, source_ids=[1],
                                                                    poll_options=["a b", "c d"]),
                                             "factcheck": FC_OK})
        d = w.draft_post("news", items, [], "")
        assert d.poll is None
        assert state.data.get("poll_dates", []) == []

    def test_writer_never_records_poll_dates(self, settings, state):
        w, _ = make_writer(settings, state, {"post-tip": [
            post_resp("poll at claude.ai: which one?", poll_options=["Claude", "ChatGPT"]),
            post_resp(self.POLL_TEXT, poll_options=["Claude", "ChatGPT"])]})
        assert w.draft_post("tip", [], [], "").poll is not None
        assert state.data.get("poll_dates", []) == []

        w2, _ = make_writer(settings, state, {"post-gm": post_resp("no greeting here", poll_options=["a", "b"])})
        assert w2.draft_post("gm", [], [], "") is None
        assert state.data.get("poll_dates", []) == []


# ---------------------------------------------------------------------------
# Notes perso et notes de performance
# ---------------------------------------------------------------------------
NOTES = ["I tested Claude Projects for my invoices: it caught two duplicates",
         "my side project got its first paying user yesterday"]
HEADER = "OWNER'S RECENT REAL NOTES (true; use one only if it fits naturally)"


class TestNotesAndPerf:
    def test_notes_listed_and_used_notes_returned(self, settings, state, items):
        w, llm = make_writer(settings, state, {"post-tip": post_resp(TIP_TEXT, used_note_ids=[2, 7])})
        d = w.draft_post("tip", items, NOTES, "")
        assert d.used_notes == [NOTES[1]]
        user = llm.calls[0]["user"]
        assert HEADER in user
        assert f"[1] {NOTES[0]}" in user and f"[2] {NOTES[1]}" in user
        assert "used_note_ids" in llm.calls[0]["schema"]["properties"]

    def test_no_notes_no_header(self, settings, state):
        w, llm = make_writer(settings, state, {"post-tip": post_resp(TIP_TEXT)})
        d = w.draft_post("tip", [], [None, "  "], "")          # notes vides ignorées
        assert d.used_notes == []
        assert HEADER not in llm.calls[0]["user"]

    def test_used_notes_passed_to_factcheck(self, settings, state, items):
        w, llm = make_writer(settings, state, {
            "post-news": post_resp(NEWS_TEXT, source_ids=[1], used_note_ids=[1]), "factcheck": FC_OK})
        d = w.draft_post("news", items, NOTES, "")
        assert d.used_notes == [NOTES[0]]
        assert NOTES[0] in llm.calls[1]["user"]

    def test_gm_notes(self, settings, state):
        w, llm = make_writer(settings, state, {"post-gm": post_resp(
            "gm. tested AI on my invoices yesterday. what's your most boring task?", used_note_ids=[1])})
        d = w.draft_post("gm", [], NOTES, "")
        assert d.used_notes == [NOTES[0]] and HEADER in llm.calls[0]["user"]

    def test_perf_in_user_message(self, settings, state, items):
        perf = "Tutorials with a copyable prompt got 2x more bookmarks than news takes."
        w, llm = make_writer(settings, state, {"post-tip": post_resp(TIP_TEXT), "post-gm": post_resp(
            "gm. what's one task you'd hand to AI today?"), "thread-": thread_responder(),
            "factcheck": FC_OK, "judge": {"winner": 1, "reason": "ok"},
            "reactive": {"post": False, "reason": "rien", "text": "", "source_ids": [], "used_note_ids": [],
                         "visual_url": "", "image_prompt": ""}})
        w.draft_post("tip", items, [], perf)
        w.draft_post("gm", [], [], perf)
        w.draft_thread(items, [], perf)
        w.draft_reactive(items, [], perf)
        for c in llm.calls:
            if c["label"] in ("factcheck", "judge"):
                continue
            assert perf in c["user"], c["label"]
            assert "WHAT WORKED RECENTLY" in c["user"]


# ---------------------------------------------------------------------------
# Visuels
# ---------------------------------------------------------------------------
class TestVisuals:
    def _draft(self, settings, state, items, **resp):
        w, _ = make_writer(settings, state, {"post-tip": post_resp(TIP_TEXT, **resp), "factcheck": FC_OK})
        return w.draft_post("tip", items, [], "")

    def test_reference_to_cited_item(self, settings, state, items):
        d = self._draft(settings, state, items, source_ids=[1], visual_url="[1]")
        assert d.visual == Visual("screenshot", "https://openai.com/index/voice-for-everyone")

    def test_cited_item_on_unlisted_domain_allowed(self, settings, state, items):
        d = self._draft(settings, state, items, source_ids=[2], visual_url="[2]")
        assert d.visual == Visual("screenshot", "https://someblog.dev/agents-break")

    def test_uncited_unlisted_domain_refused(self, settings, state, items):
        d = self._draft(settings, state, items, visual_url="https://someblog.dev/agents-break")
        assert d.visual is None

    @pytest.mark.parametrize("url", ["[3]", "https://x.com/claudeai/status/123",
                                     "https://twitter.com/a/status/1", "[9]", "ftp://openai.com/x",
                                     "https://mobile.x.com/a"])
    def test_x_and_bad_urls_refused(self, settings, state, items, url):
        d = self._draft(settings, state, items, source_ids=[3], visual_url=url)
        assert d.visual is None

    def test_allowed_domain_page(self, settings, state, items):
        d = self._draft(settings, state, items, visual_url="https://techcrunch.com/2026/10/15/voice/")
        assert d.visual == Visual("screenshot", "https://techcrunch.com/2026/10/15/voice/")

    def test_ai_photo_only_when_enabled(self, settings, state, items):
        prompt = "photo of a person rehearsing a speech with a phone on a kitchen table, morning light"
        assert self._draft(settings, state, items, image_prompt=prompt).visual is None
        settings.image_ai_enabled = True
        assert self._draft(settings, state, items, image_prompt=prompt).visual == Visual("ai", prompt)

    def test_screenshot_beats_photo(self, settings, state, items):
        settings.image_ai_enabled = True
        d = self._draft(settings, state, items, source_ids=[1], visual_url="[1]", image_prompt="a photo")
        assert d.visual.kind == "screenshot"

    def test_bad_photo_prompt_refused(self, settings, state, items):
        settings.image_ai_enabled = True
        assert self._draft(settings, state, items, image_prompt="https://openai.com/a.png").visual is None
        assert self._draft(settings, state, items, image_prompt="a casino table at night").visual is None

    def test_screenshots_disabled(self, settings, state, items):
        settings.screenshots_enabled = False
        settings.image_ai_enabled = True
        d = self._draft(settings, state, items, source_ids=[1], visual_url="[1]", image_prompt="a photo")
        assert d.visual == Visual("ai", "a photo")

    def test_visual_line_in_prompt(self, settings, state, items):
        w, llm = make_writer(settings, state, {"post-tip": post_resp(TIP_TEXT)})
        w.draft_post("tip", items, [], "")
        assert "AI photos are disabled" in llm.calls[0]["user"]
        settings.screenshots_enabled = False
        w.draft_post("tip", items, [], "")
        assert "none available" in [c for c in llm.calls if c["label"] == "post-tip"][1]["user"]

    def test_allowed_url_helpers(self):
        assert wmod.is_allowed_screenshot_url("https://www.theverge.com/a")
        assert wmod.is_allowed_screenshot_url("https://news.ycombinator.com/item?id=1")
        assert not wmod.is_allowed_screenshot_url("https://x.com/a/status/1", ["https://x.com/a/status/1"])
        assert not wmod.is_allowed_screenshot_url("https://evil-openai.com/a")
        assert wmod.is_allowed_screenshot_url("https://evil.dev/a/", ["https://evil.dev/a"])
        assert wmod.is_x_url("https://twitter.com/a") and not wmod.is_x_url("https://openai.com")


# ---------------------------------------------------------------------------
# Threads : candidats, juge, rotation
# ---------------------------------------------------------------------------
class TestThreads:
    def test_candidates_judge_and_rotation(self, settings, state, items):
        settings.thread_candidates = 3
        w, llm = make_writer(settings, state, {"thread-": thread_responder(), "factcheck": FC_OK,
                                               "judge": {"winner": 2, "reason": "le plus utile cette semaine"}})
        d = w.draft_thread(items, [], "")
        assert d is not None and d.kind == "thread" and d.angle == "timely"
        assert d.texts == thread_tweets("timely")
        assert d.sources == [items[0]]
        assert "choisi parmi 3" in d.reason and "le plus utile cette semaine" in d.reason
        assert llm.labels() == ["thread-beginner", "factcheck", "thread-timely", "factcheck",
                                "thread-tutorial", "factcheck", "judge"]
        for c in llm.calls:
            expected = settings.llm_effort_thread if c["label"].startswith("thread-") else None
            assert c["effort"] == expected, c["label"]
        judge_user = llm.calls[-1]["user"]
        assert "planned for a BEGINNER thread (thread 1)" in judge_user
        assert "THREAD 2 [kind: timely]" in judge_user
        assert set(llm.calls[-1]["schema"]["properties"]) == {"winner", "reason"}
        assert "ONLY TWEET 1 REACHES NEW PEOPLE" in llm.calls[0]["system"]
        assert wmod.THREAD_ANGLES["beginner"] in llm.calls[0]["user"]

    def test_rotation_after_soft_and_beginner(self, settings, state, items):
        settings.thread_candidates = 3
        state.data["thread_recent_kinds"] = ["tutorial", "beginner", "story"]
        w, llm = make_writer(settings, state, {"thread-": thread_responder(), "factcheck": FC_OK,
                                               "judge": {"winner": 1, "reason": "ok"}})
        d = w.draft_thread(items, [], "")
        assert d is not None
        assert [c["label"] for c in llm.calls if c["label"].startswith("thread-")] == \
            ["thread-timely", "thread-tutorial", "thread-practical"]
        judge_user = llm.calls[-1]["user"]
        assert "do NOT pick a mindset or story" in judge_user
        assert "Previous threads published (oldest → newest): tutorial, beginner, story" in judge_user

    def test_thread_plan_properties(self, settings, state):
        w, _ = make_writer(settings, state)
        for seed in range(40):
            w.rng = random.Random(seed)
            settings.thread_candidates = 6
            picks, forced = w.thread_plan([])
            assert forced and picks[:3] == ["beginner", "timely", "tutorial"]
            assert len(picks) == 5                    # practical + un seul genre « doux »
            assert len(set(picks) & wmod.THREAD_SOFT_KINDS) == 1
            picks, forced = w.thread_plan(["mindset", "tutorial"])
            assert not set(picks) & wmod.THREAD_SOFT_KINDS
            picks, forced = w.thread_plan(["tutorial", "timely"])
            assert forced and picks[0] == "beginner"
            picks, forced = w.thread_plan(["beginner", "timely"])
            assert not forced and "beginner" not in picks
            settings.thread_candidates = 1
            assert w.thread_plan(["beginner", "timely"])[0] == ["timely"]

    def test_recent_thread_kinds_synced_from_published_items(self, settings, state):
        w, _ = make_writer(settings, state)
        state.data["thread_recent_kinds"] = ["tutorial"]
        state.new_item("thread", ["a"], status="published", meta={"angle": "story"})
        state.new_item("thread", ["b"], status="rejected", meta={"angle": "mindset"})
        state.new_item("tip", ["c"], status="published", meta={"angle": "beginner"})
        assert w.recent_thread_kinds() == ["tutorial", "story"]
        assert w.recent_thread_kinds() == ["tutorial", "story"]      # idempotent
        state.new_item("thread", ["d"], status="published", meta={"angle": "beginner"})
        assert w.recent_thread_kinds() == ["tutorial", "story", "beginner"]
        assert state.data["thread_recent_kinds"] == ["tutorial", "story", "beginner"]

    def test_single_candidate_no_judge(self, settings, state, items):
        settings.thread_candidates = 1
        state.data["thread_recent_kinds"] = ["beginner"]
        w, llm = make_writer(settings, state, {"thread-": thread_responder(), "factcheck": FC_OK})
        d = w.draft_thread(items, [], "")
        assert d is not None and d.angle == "timely"
        assert "judge" not in llm.labels()

    @pytest.mark.parametrize("verdict", [None, {"winner": 9, "reason": "?"}, {"winner": "abc", "reason": ""}])
    def test_bad_judge_falls_back_to_first(self, settings, state, items, verdict):
        settings.thread_candidates = 2
        state.data["thread_recent_kinds"] = ["beginner"]
        w, _ = make_writer(settings, state, {"thread-": thread_responder(), "factcheck": FC_OK,
                                             "judge": verdict})
        d = w.draft_thread(items, [], "")
        assert d is not None and d.angle == "timely"

    def test_numbering_stripped_and_truncated(self, settings, state, items):
        settings.thread_candidates = 1
        state.data["thread_recent_kinds"] = ["beginner"]
        tweets = [f"{i}/ " + t for i, t in enumerate(thread_tweets("timely", 9), start=1)]
        w, _ = make_writer(settings, state, {"thread-": thread_responder(
            {"timely": thread_resp("timely", source_ids=[1], tweets=tweets)}), "factcheck": FC_OK})
        d = w.draft_thread(items, [], "")
        expected = thread_tweets("timely", 9)
        assert d.texts == expected[:6] + [expected[-1]]
        assert not any(t.startswith(("1/", "2/")) for t in d.texts)

    def test_bad_tweet_triggers_retry(self, settings, state, items):
        settings.thread_candidates = 1
        state.data["thread_recent_kinds"] = ["beginner"]
        bad = thread_tweets("timely")
        bad[2] = "step three: sign up at claude.ai and open a new project for this."
        w, llm = make_writer(settings, state, {"thread-": thread_responder({"timely": [
            thread_resp("timely", source_ids=[1], tweets=bad), thread_resp("timely", source_ids=[1])]}),
            "factcheck": FC_OK})
        d = w.draft_thread(items, [], "")
        assert d is not None and d.texts == thread_tweets("timely")
        assert llm.labels() == ["thread-timely", "thread-timely", "factcheck"]
        assert "tweet 3 was rejected: it contained a link or a domain name" in llm.calls[1]["user"]

    def test_too_short_everywhere_returns_none(self, settings, state, items):
        settings.thread_candidates = 3
        short = {g: thread_resp(g, n=3, source_ids=[1]) for g in wmod.THREAD_ANGLE_KINDS}
        w, llm = make_writer(settings, state, {"thread-": thread_responder(short)})
        assert w.draft_thread(items, [], "") is None
        assert "judge" not in llm.labels()
        assert "only 3 usable tweets" in llm.calls[1]["user"]

    def test_timely_must_cite_and_is_factchecked(self, settings, state, items):
        settings.thread_candidates = 1
        state.data["thread_recent_kinds"] = ["beginner"]
        w, llm = make_writer(settings, state, {
            "thread-": thread_responder({"timely": [thread_resp("timely"), thread_resp("timely", source_ids=[1]),
                                                    ]}),
            "factcheck": [{"ok": False, "issues": "nothing in the items about apps"}]})
        assert w.draft_thread(items, [], "") is None
        assert llm.labels() == ["thread-timely", "thread-timely", "factcheck"]
        assert "source_ids" in llm.calls[1]["user"]

    def test_timely_without_items_still_factchecked(self, settings, state):
        settings.thread_candidates = 1
        state.data["thread_recent_kinds"] = ["beginner"]
        w, llm = make_writer(settings, state, {"thread-": thread_responder({"timely": thread_resp("timely")}),
                                               "factcheck": FC_OK})
        d = w.draft_thread([], [], "")
        assert d is not None and d.sources == []
        assert llm.labels() == ["thread-timely", "factcheck"]

    def test_tutorial_without_sources_is_factchecked_too(self, settings, state, items):
        settings.thread_candidates = 2
        state.data["thread_recent_kinds"] = ["beginner"]
        w, llm = make_writer(settings, state, {"thread-": thread_responder({"timely": None}),
                                               "judge": {"winner": 1, "reason": "ok"}})
        d = w.draft_thread(items, [], "")
        assert d is not None and d.angle == "tutorial"
        assert "factcheck" in llm.labels() and "judge" not in llm.labels()
        assert "relis bien" in d.reason      # vérification indisponible ici : signalée sur la carte

    def test_beginner_visual_photo_only(self, settings, state, items):
        settings.thread_candidates = 1
        both = thread_resp("beginner", visual_url="[1]",
                           image_prompt="a hand photographing an open fridge with a phone, kitchen light")
        w, llm = make_writer(settings, state, {"thread-": thread_responder({"beginner": both})})
        d = w.draft_thread(items, [], "")
        assert d.angle == "beginner" and d.visual is None          # IA désactivée : jamais de capture
        assert "none available" in llm.calls[0]["user"]
        settings.image_ai_enabled = True
        w2, llm2 = make_writer(settings, state, {"thread-": thread_responder({"beginner": dict(both)})})
        d2 = w2.draft_thread(items, [], "")
        assert d2.visual == Visual("ai", both["image_prompt"])
        assert "VISUAL:" not in llm2.calls[0]["user"]

    def test_timely_screenshot(self, settings, state, items):
        settings.thread_candidates = 1
        state.data["thread_recent_kinds"] = ["beginner"]
        w, _ = make_writer(settings, state, {"thread-": thread_responder(
            {"timely": thread_resp("timely", source_ids=[1], visual_url="[1]")}), "factcheck": FC_OK})
        d = w.draft_thread(items, [], "")
        assert d.visual == Visual("screenshot", items[0].url)

    def test_hook_too_similar_to_recent_thread(self, settings, state, items):
        settings.thread_candidates = 1
        state.data["thread_recent_kinds"] = ["beginner"]
        state.remember_text("thread", thread_tweets("timely")[0])
        w, llm = make_writer(settings, state, {"thread-": thread_responder(), "factcheck": FC_OK})
        assert w.draft_thread(items, [], "") is None
        assert "too similar to a recent post" in llm.calls[1]["user"]

    def test_thread_notes(self, settings, state, items):
        settings.thread_candidates = 1
        state.data["thread_recent_kinds"] = ["beginner"]
        w, llm = make_writer(settings, state, {"thread-": thread_responder(
            {"timely": thread_resp("timely", source_ids=[1], used_note_ids=[2])}), "factcheck": FC_OK})
        d = w.draft_thread(items, NOTES, "")
        assert d.used_notes == [NOTES[1]]
        assert HEADER in llm.calls[0]["user"]

    def test_topics_for_kind_ordering(self, settings, state):
        w, _ = make_writer(settings, state)
        topics = settings.evergreen_topics
        assert w.topics_for_kind(topics, "tutorial")[0] == "TUTORIAL — topic A"
        assert w.topics_for_kind(topics, "beginner")[0] == "BEGINNER — topic B"
        assert w.topics_for_kind(topics, "story")[0] == "STORY — topic C"
        assert w.topics_for_kind(topics, "practical")[0] == "plain topic D"
        for seed in range(10):
            w.rng = random.Random(seed)
            out = w.topics_for_kind(topics, "timely")
            assert set(out[:2]) == {"TUTORIAL — topic A", "plain topic D"}
            assert sorted(out) == sorted(topics)
        assert w.topics_for_kind(topics, "tutorial", limit=2)[0] == "TUTORIAL — topic A"
        assert len(w.topics_for_kind(topics, "tutorial", limit=2)) == 2

    def test_topics_shown_in_thread_prompt(self, settings, state, items):
        settings.thread_candidates = 2
        state.data["thread_recent_kinds"] = ["beginner"]
        w, llm = make_writer(settings, state, {"thread-": thread_responder(), "factcheck": FC_OK,
                                               "judge": {"winner": 1, "reason": "ok"}})
        w.draft_thread(items, [], "")
        tut = next(c for c in llm.calls if c["label"] == "thread-tutorial")["user"]
        topics_part = tut.split("Evergreen topics", 1)[1]
        assert topics_part.index("TUTORIAL — topic A") < topics_part.index("STORY — topic C")


# ---------------------------------------------------------------------------
# Post réactif
# ---------------------------------------------------------------------------
REACT_TEXT = ("free voice mode for everyone is the quiet big one this week.\n"
              "the people who win: anyone who practices out loud before a hard conversation.")


def react_resp(post=True, text=REACT_TEXT, source_ids=(1,), **kw) -> dict:
    obj = {"post": post, "reason": "grosse annonce", "text": text if post else "",
           "source_ids": list(source_ids) if post else [], "used_note_ids": [], "visual_url": "",
           "image_prompt": ""}
    obj.update(kw)
    return obj


class TestReactive:
    def test_no_fresh_items_no_call(self, settings, state):
        old = [news(1, "Old launch", "old", hours=30), news(2, "No date", "x", hours=None)]
        w, llm = make_writer(settings, state, {"reactive": react_resp()})
        assert w.draft_reactive(old, [], "") is None
        assert llm.calls == []

    def test_gate_says_no(self, settings, state, items):
        w, llm = make_writer(settings, state, {"reactive": react_resp(post=False)})
        assert w.draft_reactive(items, [], "") is None
        assert llm.labels() == ["reactive"]

    def test_gate_yes_grounded(self, settings, state, items):
        stale = news(5, "Something from two days ago", "old news", hours=50)
        blocked = news(6, "AI startup hit with lawsuit over training data", "court case", hours=1)
        w, llm = make_writer(settings, state, {"reactive": react_resp(visual_url="[1]"), "factcheck": FC_OK})
        d = w.draft_reactive(items + [stale, blocked], NOTES, "")
        assert d is not None and d.kind == "reactive" and d.texts == [REACT_TEXT]
        assert d.sources == [items[0]] and d.reason == "grosse annonce"
        assert d.visual == Visual("screenshot", items[0].url)
        assert llm.labels() == ["reactive", "factcheck"]
        user = llm.calls[0]["user"]
        assert "two days ago" not in user and "lawsuit" not in user
        assert HEADER in user

    def test_yes_without_source_then_no(self, settings, state, items):
        w, llm = make_writer(settings, state, {"reactive": [react_resp(source_ids=()), react_resp(post=False)]})
        assert w.draft_reactive(items, [], "") is None
        assert llm.labels() == ["reactive", "reactive"]
        assert "post = false" in llm.calls[1]["user"]

    def test_factcheck_failure_retry(self, settings, state, items):
        w, llm = make_writer(settings, state, {
            "reactive": [react_resp(), react_resp(text=NEWS_TEXT_2)],
            "factcheck": [{"ok": False, "issues": "invented detail"}, FC_OK]})
        d = w.draft_reactive(items, [], "")
        assert d is not None and d.texts == [NEWS_TEXT_2]
        assert "invented detail" in llm.calls[2]["user"]

    def test_same_story_as_recent_post_rejected(self, settings, state, items):
        state.remember_text("news", REACT_TEXT)
        w, llm = make_writer(settings, state, {"reactive": react_resp(), "factcheck": FC_OK})
        assert w.draft_reactive(items, [], "") is None
        assert REACT_TEXT.splitlines()[0] in llm.calls[0]["user"]


# ---------------------------------------------------------------------------
# Texte de secours
# ---------------------------------------------------------------------------
class TestFallback:
    def test_avoids_recent_texts(self, settings, state):
        state.remember_text("gm", "gm. fallback one")
        for seed in range(15):
            w, llm = make_writer(settings, state, seed=seed)
            d = w.fallback_post("gm")
            assert d.texts == ["gm. fallback two"] and d.from_fallback and d.kind == "gm"
            assert d.reason
        assert llm.calls == []

    def test_least_recently_used_when_all_used(self, settings, state):
        state.remember_text("gm", "gm. fallback one")
        state.remember_text("gm", "gm. fallback two")
        w, _ = make_writer(settings, state)
        assert w.fallback_post("gm").texts == ["gm. fallback one"]

    def test_never_none(self, settings, state):
        settings.fallback_captions = {}
        w, _ = make_writer(settings, state)
        for kind in ("gm", "tip", "news", "weird", ""):
            d = w.fallback_post(kind)
            assert isinstance(d, Draft) and d.texts and d.texts[0] and d.from_fallback

    def test_invalid_captions_skipped(self, settings, state):
        settings.fallback_captions = {"tip": ["read more at claude.ai", "a valid evergreen tip about prompts"]}
        w, _ = make_writer(settings, state)
        for _ in range(10):
            assert w.fallback_post("tip").texts == ["a valid evergreen tip about prompts"]

    def test_news_uses_tip_pool_when_empty(self, settings, state):
        settings.fallback_captions = {"tip": ["fallback tip"]}
        w, _ = make_writer(settings, state)
        d = w.fallback_post("news")
        assert d.kind == "news" and d.texts == ["fallback tip"]


# ---------------------------------------------------------------------------
# Réponses aux commentaires
# ---------------------------------------------------------------------------
MY_POST = TIP_TEXT
REPLY = 'try this: paste the answer back and ask "which part of this are you least sure about?"'


def comment_resp(reply=True, text=REPLY) -> dict:
    return {"reply": reply, "reason": "vraie question", "text": text if reply else ""}


class TestCommentReply:
    @pytest.mark.parametrize("comment", [
        "GIVEAWAY time, tag a friend to win", "which crypto wallet works with this?",
        "https://t.co/abc", "🔥🔥", "", "@someone",
    ])
    def test_gate_before_llm(self, settings, state, comment):
        w, llm = make_writer(settings, state, {"comment-reply": comment_resp()})
        assert w.draft_comment_reply(MY_POST, "bob", comment) is None
        assert llm.calls == []

    def test_blacklisted_user(self, settings, state):
        settings.blacklist_users = ["spammer"]
        w, llm = make_writer(settings, state, {"comment-reply": comment_resp()})
        assert w.draft_comment_reply(MY_POST, "@Spammer", "how do I do that with Claude?") is None
        assert llm.calls == []

    def test_reply_ok(self, settings, state):
        w, llm = make_writer(settings, state, {"comment-reply": comment_resp()})
        out = w.draft_comment_reply(MY_POST, "@alice", "how would you check the answer though?")
        assert out == REPLY
        c = llm.calls[0]
        assert c["label"] == "comment-reply" and c["effort"] is None
        assert "@alice" in c["user"] and "how would you check" in c["user"] and MY_POST[:30] in c["user"]
        assert "commenter's language" in c["system"]

    def test_model_declines(self, settings, state):
        w, _ = make_writer(settings, state, {"comment-reply": comment_resp(reply=False)})
        assert w.draft_comment_reply(MY_POST, "bob", "nice post man really nice") is None

    def test_too_long_retry_and_french(self, settings, state):
        fr = 'essaie : colle la réponse et demande "quelle partie est la moins sûre ?"'
        w, llm = make_writer(settings, state, {"comment-reply": [comment_resp(text="word " * 50),
                                                                 comment_resp(text=fr)]})
        out = w.draft_comment_reply(MY_POST, "jean", "comment tu vérifies la réponse ?")
        assert out == fr
        assert "max 220" in llm.calls[1]["user"]

    def test_echo_and_recent_rejected(self, settings, state):
        comment = "how do you check if the AI answer is actually right before using it at work"
        state.remember_text("comment_reply", REPLY)
        w, llm = make_writer(settings, state, {"comment-reply": [comment_resp(text=comment),
                                                                 comment_resp(text=REPLY)]})
        assert w.draft_comment_reply(MY_POST, "bob", comment) is None
        assert REPLY in llm.calls[0]["user"]          # réponses récentes montrées

    def test_url_in_reply_rejected(self, settings, state):
        w, _ = make_writer(settings, state, {"comment-reply": [comment_resp(text="see claude.ai/docs"),
                                                               comment_resp()]})
        assert w.draft_comment_reply(MY_POST, "bob", "where can I learn more?") == REPLY


# ---------------------------------------------------------------------------
# Radar : suggestion de réponse
# ---------------------------------------------------------------------------
BIG_POST = "we just shipped memory for everyone. it remembers your projects across chats."
RADAR_REPLY = "the real test: does it forget the stuff you want gone? that's the part people will care about."


def radar_resp(reply=True, text=RADAR_REPLY, angle="question qui fait réfléchir") -> dict:
    return {"reply": reply, "reason": "angle utile", "text": text if reply else "",
            "angle": angle if reply else ""}


class TestRadar:
    @pytest.mark.parametrize("post", [
        "the election results are in and everyone has opinions",
        "GIVEAWAY: like and rt to win a free subscription",
        "lol",
        "https://t.co/abcdefghijklmnop",
        "bitcoin is about to change everything for builders",
        "we are heartbroken: our cofounder died last night",
    ])
    def test_gate_before_llm(self, settings, state, post):
        w, llm = make_writer(settings, state, {"radar-reply": radar_resp()})
        assert w.draft_radar_reply("sama", post, 5, 100) is None
        assert llm.calls == []

    def test_blacklisted(self, settings, state):
        settings.blacklist_users = ["badguy"]
        w, llm = make_writer(settings, state, {"radar-reply": radar_resp()})
        assert w.draft_radar_reply("@BadGuy", BIG_POST, 5, 100) is None
        assert llm.calls == []

    def test_suggestion_ok(self, settings, state):
        w, llm = make_writer(settings, state, {"radar-reply": radar_resp()})
        out = w.draft_radar_reply("@sama", BIG_POST, 10.0, 500)
        assert out == (RADAR_REPLY, "question qui fait réfléchir")
        c = llm.calls[0]
        assert c["label"] == "radar-reply"
        assert "Posted 10 minutes ago, already 500 likes" in c["user"]
        assert "@sama" in c["user"] and BIG_POST in c["user"]
        assert "SUGGESTIONS" in c["system"] and "HIMSELF" in c["system"] and "slop" in c["system"]
        assert set(c["schema"]["properties"]) == {"reply", "reason", "text", "angle"}

    def test_no_age(self, settings, state):
        w, llm = make_writer(settings, state, {"radar-reply": radar_resp()})
        assert w.draft_radar_reply("sama", BIG_POST, None, None) is not None
        assert "Posted" not in llm.calls[0]["user"]

    def test_declines(self, settings, state):
        w, _ = make_writer(settings, state, {"radar-reply": radar_resp(reply=False)})
        assert w.draft_radar_reply("sama", BIG_POST, 5, 50) is None

    def test_too_long_then_ok(self, settings, state):
        w, llm = make_writer(settings, state, {"radar-reply": [radar_resp(text="memory " * 35), radar_resp()]})
        assert w.draft_radar_reply("sama", BIG_POST, 5, 50)[0] == RADAR_REPLY
        assert "max 200" in llm.calls[1]["user"]

    def test_echo_of_post_rejected(self, settings, state):
        w, _ = make_writer(settings, state, {"radar-reply": radar_resp(text=BIG_POST)})
        assert w.draft_radar_reply("sama", BIG_POST, 5, 50) is None

    def test_near_duplicate_of_recent_suggestion_rejected(self, settings, state):
        state.remember_text("radar", RADAR_REPLY)
        w, llm = make_writer(settings, state, {"radar-reply": radar_resp()})
        assert w.draft_radar_reply("sama", BIG_POST, 5, 50) is None
        assert RADAR_REPLY in llm.calls[0]["user"]


# ---------------------------------------------------------------------------
# Vérification des captures
# ---------------------------------------------------------------------------
class TestVisualMatches:
    def test_x_url_refused_without_llm(self, settings, state):
        w, llm = make_writer(settings, state, {"visual-check": {"matches": True, "reason": "ok"}})
        ok, why = w.visual_matches("post", "https://x.com/a/status/1", "plenty of visible text on this page")
        assert not ok and "x.com" in why
        assert w.visual_matches("post", "", "plenty of visible text")[0] is False
        assert llm.calls == []

    def test_empty_page(self, settings, state):
        w, llm = make_writer(settings, state, {"visual-check": {"matches": True, "reason": "ok"}})
        assert w.visual_matches("post", "https://openai.com/a", "  \n ") == (False, "page vide ou illisible")
        assert llm.calls == []

    def test_llm_verdict(self, settings, state):
        w, llm = make_writer(settings, state, {"visual-check": [{"matches": True, "reason": "annonce visible"},
                                                                {"matches": False, "reason": ""}]})
        page = "Introducing voice for everyone. Today we are making voice available to all users."
        assert w.visual_matches(NEWS_TEXT, "https://openai.com/index/voice", page) == (True, "annonce visible")
        assert w.visual_matches(NEWS_TEXT, "https://openai.com/index/voice", page)[0] is False
        assert llm.calls[0]["label"] == "visual-check"
        assert page in llm.calls[0]["user"] and NEWS_TEXT in llm.calls[0]["user"]


# ---------------------------------------------------------------------------
# Contenu des consignes (ligne éditoriale + algorithme X)
# ---------------------------------------------------------------------------
class TestPrompts:
    def test_growth_facts_in_prompts(self):
        for p in (wmod.SYSTEM_PROMPT, wmod.THREAD_SYSTEM_PROMPT, wmod.REACTIVE_GATE_PROMPT):
            assert '"Not interested"' in p and "copy-link" in p and "FIRST LINE" in p
            assert "idiom-light" in p
        assert "ONLY TWEET 1 REACHES NEW PEOPLE" in wmod.THREAD_SYSTEM_PROMPT
        assert "Tweet 1 on its own" in wmod.JUDGE_SYSTEM_PROMPT
        assert "empty greeting" in wmod.GM_SYSTEM_PROMPT
        assert "copypasta" in wmod.RADAR_REPLY_PROMPT

    def test_editorial_line_in_prompts(self):
        for p in (wmod.SYSTEM_PROMPT, wmod.THREAD_SYSTEM_PROMPT, wmod.REACTIVE_GATE_PROMPT):
            assert "Crypto, NFTs" in p and "Muslim" in p and "PERSONAL FACTS" in p
            assert "ORIGINALITY" in p and "GROUNDING" in p and "No links and no domain names" in p
            assert "260 characters" in p
        assert "max 200 characters" in wmod.GM_SYSTEM_PROMPT
        assert "Muslim" in wmod.RADAR_REPLY_PROMPT and "Muslim" in wmod.GM_SYSTEM_PROMPT
        assert "x.com" not in wmod._SCREENSHOT_DOMAINS_TEXT.split(", ")



def test_news_slot_cannot_repeat_todays_reactive_post(settings, state, items):
    reactive = ("free voice mode changes who practices hard conversations.\n"
                "try this tonight: rehearse your salary talk out loud and ask it to push back.")
    state.remember_text("reactive", reactive)
    w, llm = make_writer(settings, state, {"post-news": [post_resp(reactive, source_ids=[1]),
                                                         post_resp(NEWS_TEXT_2, source_ids=[1])],
                                           "factcheck": FC_OK})
    d = w.draft_post("news", items, [], "")
    assert d is not None and d.texts == [NEWS_TEXT_2]
    first_prompt = [c for c in llm.calls if c["label"] == "post-news"][0]["user"]
    assert "never cover the same story" in first_prompt and "rehearse your salary talk" in first_prompt
