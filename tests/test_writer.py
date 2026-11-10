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


# La carte jointe à chaque post (bot/cards.py la dessine) : ici, une phrase à copier.
CARD = {"card_type": "prompt", "card_title": "Check any *AI answer*",
        "card_lines": ["What would make this answer wrong?"],
        "card_note": "Works in ChatGPT, Claude or Gemini."}
CARD_PARSED = {"type": "prompt", "title": "Check any *AI answer*",
               "lines": ["What would make this answer wrong?"], "note": "Works in ChatGPT, Claude or Gemini."}


def post_resp(text: str, source_ids=None, used_note_ids=None, visual_url: str = "",
              poll_options=None, reason: str = "raison", **card) -> dict:
    obj = {"text": text, "source_ids": list(source_ids or []), "used_note_ids": list(used_note_ids or []),
           **CARD, "visual_url": visual_url, "poll_options": list(poll_options or []), "reason": reason}
    obj.update(card)
    return obj


def only(w: Writer, *genres: str) -> None:
    """Impose les genres de thread candidats (sinon : rotation du jour)."""
    w.thread_plan = lambda recent_kinds: list(genres)


FC_OK = {"ok": True, "issues": "", "plain": True, "hard_words": ""}

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
        "weekly": "this week, three AI changes that matter if you never read tech news. the first one is free.",
    }
    body = [
        f"step {w}: open the chat, paste your material and say what decision you need from it."
        for w in ("one", "two", "three", "four", "five", "six", "seven", "eight")
    ]
    return [hooks.get(genre, f"{genre} hook")] + [f"{genre} — {b}" for b in body[: n - 1]]


def thread_resp(genre: str, n: int = 5, source_ids=None, **extra) -> dict:
    obj = {"tweets": thread_tweets(genre, n), "source_ids": list(source_ids or []), "used_note_ids": [],
           "card_type": "list", "card_title": "5 things to *try tonight*",
           "card_lines": ["Dinner from a fridge photo", "A letter explained simply"], "card_note": "",
           "visual_url": "", "reason": f"raison {genre}"}
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
        assert "a post on X: it can't be shown" in user
        assert "would my neighbour care?" in user        # l'actu doit parler à tout le monde
        assert d.card == CARD_PARSED and d.visual is None
        fc = llm.calls[1]
        assert NEWS_TEXT in fc["user"] and items[0].title in fc["user"]
        assert items[1].title not in fc["user"]          # seules les actus citées
        assert "IMAGE CARD posted with it" in fc["user"] and "What would make this answer wrong?" in fc["user"]
        assert set(fc["schema"]["properties"]) == {"ok", "issues", "plain", "hard_words"}

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

    def test_news_slot_without_news_for_everyday_people_is_an_everyday_post(self, settings, state, items):
        """Aucune actu ne parle au grand public : le créneau « actu » devient un post
        du quotidien (aucune source citée), toujours relu."""
        w, llm = make_writer(settings, state, {"post-news": post_resp(TIP_TEXT, source_ids=[]),
                                               "factcheck": FC_OK})
        d = w.draft_post("news", items, [], "")
        assert d is not None and d.texts == [TIP_TEXT] and d.sources == []
        assert llm.labels() == ["post-news", "factcheck"]
        assert "no item cited" in llm.calls[1]["user"]

    def test_too_technical_is_rewritten(self, settings, state, items):
        w, llm = make_writer(settings, state, {
            "post-news": [post_resp(NEWS_TEXT, source_ids=[1]), post_resp(NEWS_TEXT_2, source_ids=[1])],
            "factcheck": [{"ok": True, "issues": "", "plain": False, "hard_words": "voice mode latency"}, FC_OK]})
        d = w.draft_post("news", items, [], "")
        assert d is not None and d.texts == [NEWS_TEXT_2]
        retry_user = llm.calls[2]["user"]
        assert "an everyday reader would stumble on: voice mode latency" in retry_user

    def test_facts_and_plain_words_reported_together(self, settings, state, items):
        w, llm = make_writer(settings, state, {
            "post-news": [post_resp(NEWS_TEXT, source_ids=[1]), post_resp(NEWS_TEXT_2, source_ids=[1])],
            "factcheck": [{"ok": False, "issues": "no price in the item", "plain": False, "hard_words": "rollout"},
                          FC_OK]})
        assert w.draft_post("news", items, [], "") is not None
        retry_user = llm.calls[2]["user"]
        assert "no price in the item" in retry_user and "stumble on: rollout" in retry_user

    def test_still_a_bit_technical_on_second_try_is_accepted(self, settings, state, items):
        """Faits justes, relecteur encore tatillon au 2e essai : le post part quand même."""
        w, llm = make_writer(settings, state, {
            "post-news": [post_resp(NEWS_TEXT, source_ids=[1]), post_resp(NEWS_TEXT_2, source_ids=[1])],
            "factcheck": {"ok": True, "issues": "", "plain": False, "hard_words": "rehearsal"}})
        d = w.draft_post("news", items, [], "")
        assert d is not None and d.texts == [NEWS_TEXT_2]
        assert llm.labels() == ["post-news", "factcheck", "post-news", "factcheck"]

    @pytest.mark.parametrize("bad, word", [
        ("the new LLM is free for everyone. here is what to try first with it tonight.", "LLM"),
        ("this open-source release changes what you can do at home with your photos.", "open-source"),
        ("stop writing emails by hand: this workflow does it for you in one minute.", "workflow"),
        ("AI can hallucinate. ask it where it found each fact before you trust it.", "hallucinate"),
    ])
    def test_tech_jargon_rejected_by_code(self, settings, state, items, bad, word):
        w, llm = make_writer(settings, state, {"post-tip": [post_resp(bad), post_resp(TIP_TEXT)],
                                               "factcheck": FC_OK})
        d = w.draft_post("tip", items, [], "")
        assert d is not None and d.texts == [TIP_TEXT]
        assert llm.labels() == ["post-tip", "post-tip", "factcheck"]
        assert f'it used tech jargon ("{word}")' in llm.calls[1]["user"]

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
                                               "comment-": boom, "radar-": boom, "screenshot-": boom})
        assert w2.draft_post("news", [], [], "") is None
        assert w2.draft_thread([], [], "") is None
        assert w2.draft_reactive([news(1, "Fresh launch", "something new")], [], "") is None
        assert w2.draft_comment_reply("my post", "bob", "how do I do this with Claude?") is None
        assert w2.draft_radar_reply("sama", "a long enough post about building with AI tools", 5, 50) is None
        assert w2.screenshot_is_clean("capture.jpg") == (False, "vérification de l'image indisponible")

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
        assert "visual_url" not in gm_props and "source_ids" not in gm_props and "card_type" not in gm_props
        for label in ("post-news", "post-tip"):
            props = by_label[label]["properties"]
            assert {"card_type", "card_title", "card_lines", "card_note", "visual_url"} <= set(props)
            assert props["card_type"]["enum"] == ["prompt", "steps", "list", "fact", "phone", "none"]
            assert "image_prompt" not in props


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
            self.POLL_TEXT, poll_options=["Claude", "ChatGPT", "Gemini", "by hand"], card_type="none")})
        d = w.draft_post("tip", [], [], "")
        assert d.poll == {"options": ["Claude", "ChatGPT", "Gemini", "by hand"], "duration_minutes": 1440}
        assert d.visual is None and d.card is None          # un sondage n'a pas d'image
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
            self.POLL_TEXT, poll_options=["Claude", "ChatGPT"])})
        assert w.polls_left() == 0
        d = w.draft_post("tip", [], [], "")
        assert d is not None and d.poll is None
        assert d.card == CARD_PARSED                        # plus de sondage : le post garde sa carte
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
        ["Claude", "Any LLM"],                               # jargon
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
                         "card_type": "none", "card_title": "", "card_lines": [], "card_note": "",
                         "visual_url": ""}})
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
# Cartes (le visuel de chaque post)
# ---------------------------------------------------------------------------
class TestCards:
    def _draft(self, settings, state, items, **resp):
        w, _ = make_writer(settings, state, {"post-news": post_resp(NEWS_TEXT, **resp), "factcheck": FC_OK})
        return w.draft_post("news", items, [], "")

    def _problem(self, settings, state, items, **resp):
        """Le motif renvoyé à Claude quand la carte du 1er essai est refusée."""
        w, llm = make_writer(settings, state, {
            "post-news": [post_resp(NEWS_TEXT, **resp), post_resp(NEWS_TEXT)], "factcheck": FC_OK})
        d = w.draft_post("news", items, [], "")
        assert d is not None and d.card == CARD_PARSED
        assert llm.labels() == ["post-news", "post-news", "factcheck"]
        retry = llm.calls[1]["user"]
        assert "the image card was rejected" in retry and "Follow the CARD rules" in retry
        return retry

    PHONE = {"card_type": "phone", "card_title": "ChatGPT voice is now *free*",
             "card_lines": ["No paid plan needed", "Talk instead of typing"], "card_note": ""}

    def test_phone_card_shows_the_cited_page(self, settings, state, items):
        d = self._draft(settings, state, items, source_ids=[1], visual_url="[1]", **self.PHONE)
        assert d.visual == Visual("phone", "https://openai.com/index/voice-for-everyone")
        assert d.card["type"] == "phone" and d.card["lines"] == ["No paid plan needed", "Talk instead of typing"]

    def test_phone_card_on_any_cited_site(self, settings, state, items):
        d = self._draft(settings, state, items, source_ids=[2], visual_url="[2]", **self.PHONE)
        assert d.visual == Visual("phone", "https://someblog.dev/agents-break")

    @pytest.mark.parametrize("url", ["", "[2]", "[9]", "https://techcrunch.com/other", "nonsense"])
    def test_phone_card_never_leaves_the_cited_items(self, settings, state, items, url):
        """Page non citée, numéro inconnu, adresse au hasard : on montre la page de l'actu citée."""
        d = self._draft(settings, state, items, source_ids=[1], visual_url=url, **self.PHONE)
        assert d.visual == Visual("phone", items[0].url)

    def test_phone_card_picks_the_designated_cited_item(self, settings, state, items):
        d = self._draft(settings, state, items, source_ids=[1, 2], visual_url="[2]", **self.PHONE)
        assert d.visual == Visual("phone", items[1].url)
        d = self._draft(settings, state, items, source_ids=[1, 2], visual_url=items[1].url + "/", **self.PHONE)
        assert d.visual == Visual("phone", items[1].url)

    @pytest.mark.parametrize("source_ids, url", [([3], "[3]"), ([3], "https://x.com/claudeai/status/123"),
                                                  ([], "[1]"), ([], "https://openai.com/x")])
    def test_phone_without_showable_page_becomes_a_drawn_card(self, settings, state, items, source_ids, url):
        """Post X ou aucune actu citée : jamais de capture, une carte dessinée avec les mêmes textes."""
        d = self._draft(settings, state, items, source_ids=source_ids, visual_url=url, **self.PHONE)
        assert d is not None and d.visual is None
        assert d.card["type"] == "list" and d.card["title"] == "ChatGPT voice is now *free*"

    def test_phone_with_one_line_becomes_a_fact_card(self, settings, state, items):
        d = self._draft(settings, state, items, card_type="phone", card_title="Voice is free now",
                        card_lines=["No paid plan needed"], card_note="")
        assert d.visual is None and d.card["type"] == "fact" and d.card["lines"] == ["No paid plan needed"]

    def test_screenshots_disabled(self, settings, state, items):
        settings.screenshots_enabled = False
        w, llm = make_writer(settings, state, {
            "post-news": post_resp(NEWS_TEXT, source_ids=[1], visual_url="[1]", **self.PHONE), "factcheck": FC_OK})
        d = w.draft_post("news", items, [], "")
        assert d.visual is None and d.card["type"] == "list"
        assert 'the "phone" card is not available right now' in llm.calls[0]["user"]
        settings.screenshots_enabled = True
        w.draft_post("news", items, [], "")
        assert "not available right now" not in [c for c in llm.calls if c["label"] == "post-news"][1]["user"]

    def test_card_text_is_cleaned(self, settings, state, items):
        d = self._draft(settings, state, items, card_type="steps", card_title='"Fix a bad *answer*" 🚀',
                        card_lines=["1. Open ChatGPT → tap the mic 🎤", "- Say what you need", "• Ask for a shorter one",
                                    "  ", "Step 4: double-check the result"],
                        card_note="  Free versions   too.  ")
        assert d.card == {"type": "steps", "title": "Fix a bad *answer*",
                          "lines": ["Open ChatGPT › tap the mic", "Say what you need", "Ask for a shorter one",
                                    "double-check the result"],
                          "note": "Free versions too."}

    def test_prompt_card_is_one_message(self, settings, state, items):
        d = self._draft(settings, state, items, card_type="prompt", card_title="Dinner from a *fridge photo*",
                        card_lines=['"Here is a photo of my fridge.', 'Give me 3 dinners I can make tonight."'],
                        card_note="x" * 200)
        assert d.card["lines"] == ["Here is a photo of my fridge. Give me 3 dinners I can make tonight."]
        assert d.card["note"] == ""                         # note trop longue : on s'en passe
        keep = self._draft(settings, state, items, card_type="prompt", card_title="A plan in one message",
                           card_lines=["3 dinners for 4 people, under 30 minutes each."], card_note="")
        assert keep.card["lines"] == ["3 dinners for 4 people, under 30 minutes each."]   # le chiffre du début reste

    def test_fact_card_extra_lines_are_dropped(self, settings, state, items):
        d = self._draft(settings, state, items, card_type="fact", card_title="Voice is free now",
                        card_lines=["one", "two", "three", "four", "five"], card_note="")
        assert d.card["lines"] == ["one", "two", "three"]
        bare = self._draft(settings, state, items, card_type="fact", card_title="Voice is free now",
                           card_lines=[], card_note="")
        assert bare.card == {"type": "fact", "title": "Voice is free now", "lines": [], "note": ""}

    def test_card_is_required_without_a_poll(self, settings, state, items):
        retry = self._problem(settings, state, items, card_type="none", card_title="", card_lines=[])
        assert "a card is required" in retry

    @pytest.mark.parametrize("card, why", [
        ({"card_type": "poster"}, "unknown card_type"),
        ({"card_title": "  "}, "card_title is empty"),
        ({"card_title": "A title that goes on and on until it no longer fits on any card at all"},
         "card_title is too long"),
        ({"card_type": "steps", "card_lines": ["Only one step"]}, 'a "steps" card needs 2 to 4 card_lines (got 1)'),
        ({"card_type": "steps", "card_lines": ["a", "b", "c", "d", "e"]}, "needs 2 to 4 card_lines (got 5)"),
        ({"card_type": "list", "card_lines": []}, 'a "list" card needs 2 to 5 card_lines (got 0)'),
        ({"card_type": "prompt", "card_lines": []}, 'a "prompt" card needs exactly 1 card_lines (got 0)'),
        ({"card_type": "list", "card_lines": ["short", "x" * 81]}, "a card line is too long (81 characters, max 60)"),
        ({"card_type": "prompt", "card_lines": ["y" * 241]}, "a card line is too long (241 characters, max 190)"),
        ({"card_lines": ["Paste it into chatgpt.com and ask"]}, "no links or domain names on the card"),
        ({"card_title": "Your first casino night plan"}, "off-limits word or topic on the card (casino)"),
        ({"card_note": "Works with any LLM."}, 'tech jargon on the card ("LLM")'),
        ({"card_type": "list", "card_lines": ["Pick a use case", "Then try it"]}, 'tech jargon on the card ("use case")'),
    ])
    def test_bad_card_is_sent_back_once(self, settings, state, items, card, why):
        assert why in self._problem(settings, state, items, **card)

    def test_card_refused_twice_the_post_goes_out_as_text(self, settings, state, items):
        """Le texte est bon : on ne perd pas le créneau pour une carte ratée."""
        w, llm = make_writer(settings, state, {
            "post-news": post_resp(NEWS_TEXT, source_ids=[1], card_type="steps", card_lines=["One step"]),
            "factcheck": FC_OK})
        d = w.draft_post("news", items, [], "")
        assert d is not None and d.texts == [NEWS_TEXT] and d.card is None and d.visual is None
        assert llm.labels() == ["post-news", "post-news", "factcheck"]
        assert "IMAGE CARD" not in llm.calls[2]["user"]

    def test_card_never_checked_when_text_is_rejected(self, settings, state, items):
        w, llm = make_writer(settings, state, {
            "post-news": [post_resp("read it on claude.ai tonight", card_type="none"), post_resp(NEWS_TEXT)],
            "factcheck": FC_OK})
        assert w.draft_post("news", items, [], "") is not None
        assert "domain name" in llm.calls[1]["user"] and "image card" not in llm.calls[1]["user"]

    def test_url_helpers(self):
        assert wmod.is_capturable_url("https://www.theverge.com/a")
        assert wmod.is_capturable_url("https://someblog.dev/post")
        for bad in ("https://x.com/a/status/1", "https://mobile.twitter.com/a", "ftp://openai.com/x", "", "openai.com"):
            assert not wmod.is_capturable_url(bad), bad
        assert wmod.is_x_url("https://twitter.com/a") and not wmod.is_x_url("https://openai.com")

    def test_card_text_helper(self):
        assert wmod.card_text("→ Dinner ideas") == "Dinner ideas"
        assert wmod.card_text("Photo -> recipe") == "Photo › recipe"
        assert wmod.card_text("5:30 wake-up plan") == "5:30 wake-up plan"
        assert wmod.card_text("2) Paste the text") == "Paste the text"
        assert wmod.card_text("Café déjà vu — it’s “fine”…") == "Café déjà vu — it’s “fine”…"
        assert wmod.card_text("#AI tip for @you") == "tip for"
        assert wmod.card_text(None) == "" and wmod.card_text(42) == "42"
        assert wmod.card_as_text(CARD_PARSED).splitlines() == [
            "[prompt] Check any AI answer", "- What would make this answer wrong?",
            "(Works in ChatGPT, Claude or Gemini.)"]
        assert wmod.card_as_text(None) == ""


# ---------------------------------------------------------------------------
# Les « moments de la vie » des astuces
# ---------------------------------------------------------------------------
class TestLifeAreas:
    def test_theme_rotates_without_repeating(self, settings, state):
        w, llm = make_writer(settings, state, {"post-tip": post_resp(TIP_TEXT), "factcheck": FC_OK})
        seen = []
        for _ in range(300):
            key, text = w._next_life_area()
            assert key not in seen[-wmod.LIFE_AREAS_MEMORY:], (key, seen)
            assert text and key in dict(wmod.LIFE_AREAS)
            seen.append(key)
        assert set(seen) == set(dict(wmod.LIFE_AREAS))      # tous les thèmes finissent par passer
        assert len(state.data["life_areas_recent"]) == wmod.LIFE_AREAS_MEMORY

    def test_theme_is_in_the_tip_prompt_only(self, settings, state, items):
        w, llm = make_writer(settings, state, {"post-": post_resp(TIP_TEXT), "factcheck": FC_OK})
        w.draft_post("tip", items, [], "")
        w.draft_post("news", items, [], "")
        tip_user, news_user = [c["user"] for c in llm.calls if c["label"].startswith("post-")]
        area = dict(wmod.LIFE_AREAS)[state.data["life_areas_recent"][-1]]
        assert "EVERYDAY USE" in tip_user and f"Theme of this post: {area}" in tip_user
        assert "NEWS IN PLAIN WORDS" in news_user and "Theme of this post" not in news_user
        assert len(state.data["life_areas_recent"]) == 1

    def test_areas_are_clean(self):
        from bot.filters import find_tech_jargon, off_limits_term
        keys = [k for k, _ in wmod.LIFE_AREAS]
        assert len(keys) == len(set(keys)) > wmod.LIFE_AREAS_MEMORY
        for _key, text in wmod.LIFE_AREAS:
            assert not off_limits_term(text) and not find_tech_jargon(text), text


# ---------------------------------------------------------------------------
# Threads : candidats, juge, rotation
# ---------------------------------------------------------------------------
class TestThreads:
    def test_candidates_judge_and_rotation(self, settings, state, items):
        settings.thread_candidates = 3
        w, llm = make_writer(settings, state, {"thread-": thread_responder(), "factcheck": FC_OK,
                                               "judge": {"winner": 3, "reason": "le plus utile cette semaine"}})
        d = w.draft_thread(items, [], "")
        assert d is not None and d.kind == "thread" and d.angle == "timely"
        assert d.texts == thread_tweets("timely")
        assert d.sources == [items[0]]
        assert d.card["type"] == "list" and d.card["title"] == "5 things to *try tonight*"
        assert "choisi parmi 3" in d.reason and "le plus utile cette semaine" in d.reason
        assert llm.labels() == ["thread-beginner", "factcheck", "thread-tutorial", "factcheck",
                                "thread-timely", "factcheck", "judge"]
        for c in llm.calls:
            expected = settings.llm_effort_thread if c["label"].startswith("thread-") else None
            assert c["effort"] == expected, c["label"]
        judge_user = llm.calls[-1]["user"]
        assert "THREAD 3 [kind: timely]" in judge_user and "Previous threads" not in judge_user
        assert set(llm.calls[-1]["schema"]["properties"]) == {"winner", "reason"}
        assert "ONLY TWEET 1 REACHES NEW PEOPLE" in llm.calls[0]["system"]
        assert wmod.THREAD_ANGLES["beginner"] in llm.calls[0]["user"]
        assert "IMAGE CARD posted with it" in llm.calls[1]["user"]      # la couverture est relue aussi

    def test_last_published_kind_goes_last(self, settings, state, items):
        settings.thread_candidates = 3
        state.data["thread_recent_kinds"] = ["tutorial", "beginner"]
        w, llm = make_writer(settings, state, {"thread-": thread_responder(), "factcheck": FC_OK,
                                               "judge": {"winner": 1, "reason": "ok"}})
        d = w.draft_thread(items, [], "")
        assert d is not None and d.angle == "tutorial"
        assert [c["label"] for c in llm.calls if c["label"].startswith("thread-")] == \
            ["thread-tutorial", "thread-timely", "thread-beginner"]
        judge_user = llm.calls[-1]["user"]
        assert "Previous threads published (oldest → newest): tutorial, beginner" in judge_user
        assert "prefer a kind different from the last one" in judge_user

    def test_thread_plan(self, settings, state):
        w, _ = make_writer(settings, state)
        settings.thread_candidates = 6
        assert w.thread_plan([]) == ["beginner", "tutorial", "timely"]
        assert w.thread_plan(["timely"]) == ["beginner", "tutorial", "timely"]
        assert w.thread_plan(["beginner", "tutorial"]) == ["beginner", "timely", "tutorial"]
        assert w.thread_plan(["weekly", "beginner"]) == ["tutorial", "timely", "beginner"]
        settings.thread_candidates = 1
        assert w.thread_plan(["beginner"]) == ["tutorial"]
        assert w.thread_plan(["tutorial"]) == ["beginner"]
        settings.thread_candidates = 0                       # réglage absurde : un candidat quand même
        assert w.thread_plan([]) == ["beginner"]
        assert set(wmod.THREAD_ANGLES) == set(wmod.THREAD_ANGLE_KINDS) == set(wmod.GENRE_FR)

    def test_weekend_thread_is_the_weekly_recap(self, settings, state, clock):
        w, _ = make_writer(settings, state)
        clock(13, 0, day=18)                                  # dimanche 18 octobre 2026
        settings.thread_candidates = 3
        assert w.thread_plan(["tutorial"]) == ["weekly", "beginner", "tutorial"]
        clock(13, 0, day=20)                                  # mardi : pas de récap
        assert "weekly" not in w.thread_plan(["tutorial"])

    def test_recent_thread_kinds_synced_from_published_items(self, settings, state):
        w, _ = make_writer(settings, state)
        state.data["thread_recent_kinds"] = ["tutorial"]
        state.new_item("thread", ["a"], status="published", meta={"angle": "weekly"})
        state.new_item("thread", ["b"], status="rejected", meta={"angle": "timely"})
        state.new_item("tip", ["c"], status="published", meta={"angle": "beginner"})
        state.new_item("thread", ["e"], status="published", meta={"angle": "story"})   # ancien genre : ignoré
        assert w.recent_thread_kinds() == ["tutorial", "weekly"]
        assert w.recent_thread_kinds() == ["tutorial", "weekly"]      # idempotent
        state.new_item("thread", ["d"], status="published", meta={"angle": "beginner"})
        assert w.recent_thread_kinds() == ["tutorial", "weekly", "beginner"]
        assert state.data["thread_recent_kinds"] == ["tutorial", "weekly", "beginner"]

    def test_single_candidate_no_judge(self, settings, state, items):
        settings.thread_candidates = 1
        state.data["thread_recent_kinds"] = ["beginner"]
        w, llm = make_writer(settings, state, {"thread-": thread_responder(), "factcheck": FC_OK})
        d = w.draft_thread(items, [], "")
        assert d is not None and d.angle == "tutorial"
        assert "judge" not in llm.labels()

    @pytest.mark.parametrize("verdict", [None, {"winner": 9, "reason": "?"}, {"winner": "abc", "reason": ""}])
    def test_bad_judge_falls_back_to_first(self, settings, state, items, verdict):
        settings.thread_candidates = 2
        state.data["thread_recent_kinds"] = ["beginner"]
        w, _ = make_writer(settings, state, {"thread-": thread_responder(), "factcheck": FC_OK,
                                             "judge": verdict})
        d = w.draft_thread(items, [], "")
        assert d is not None and d.angle == "tutorial"

    def test_numbering_stripped_and_truncated(self, settings, state, items):
        tweets = [f"{i}/ " + t for i, t in enumerate(thread_tweets("timely", 9), start=1)]
        w, _ = make_writer(settings, state, {"thread-": thread_responder(
            {"timely": thread_resp("timely", source_ids=[1], tweets=tweets)}), "factcheck": FC_OK})
        only(w, "timely")
        d = w.draft_thread(items, [], "")
        expected = thread_tweets("timely", 9)
        assert d.texts == expected[:6] + [expected[-1]]
        assert not any(t.startswith(("1/", "2/")) for t in d.texts)

    def test_bad_tweet_triggers_retry(self, settings, state, items):
        bad = thread_tweets("timely")
        bad[2] = "step three: sign up at claude.ai and open a new project for this."
        w, llm = make_writer(settings, state, {"thread-": thread_responder({"timely": [
            thread_resp("timely", source_ids=[1], tweets=bad), thread_resp("timely", source_ids=[1])]}),
            "factcheck": FC_OK})
        only(w, "timely")
        d = w.draft_thread(items, [], "")
        assert d is not None and d.texts == thread_tweets("timely")
        assert llm.labels() == ["thread-timely", "thread-timely", "factcheck"]
        assert "tweet 3 was rejected: it contained a link or a domain name" in llm.calls[1]["user"]

    def test_jargon_in_a_tweet_triggers_retry(self, settings, state, items):
        bad = thread_tweets("tutorial")
        bad[1] = "step one: pick the best LLM for the job, then paste your material."
        w, llm = make_writer(settings, state, {"thread-": thread_responder({"tutorial": [
            thread_resp("tutorial", tweets=bad), thread_resp("tutorial")]}), "factcheck": FC_OK})
        only(w, "tutorial")
        assert w.draft_thread(items, [], "") is not None
        assert 'tweet 2 was rejected: it used tech jargon ("LLM")' in llm.calls[1]["user"]

    def test_too_short_everywhere_returns_none(self, settings, state, items):
        settings.thread_candidates = 3
        short = {g: thread_resp(g, n=3, source_ids=[1]) for g in wmod.THREAD_ANGLE_KINDS}
        w, llm = make_writer(settings, state, {"thread-": thread_responder(short)})
        assert w.draft_thread(items, [], "") is None
        assert "judge" not in llm.labels()
        assert "only 3 usable tweets" in llm.calls[1]["user"]

    def test_news_claims_without_citation_fail_the_factcheck(self, settings, state, items):
        """Plus de règle « il faut citer » côté code : un thread d'actu sans source
        est simplement recalé par la relecture (aucun fait d'actu n'est sourcé)."""
        w, llm = make_writer(settings, state, {
            "thread-": thread_responder({"timely": [thread_resp("timely"), thread_resp("timely", source_ids=[1])]}),
            "factcheck": [{"ok": False, "issues": "no item cited"},
                          {"ok": False, "issues": "nothing in the items about apps"}]})
        only(w, "timely")
        assert w.draft_thread(items, [], "") is None
        assert llm.labels() == ["thread-timely", "factcheck", "thread-timely", "factcheck"]
        assert "no item cited" in llm.calls[1]["user"]
        assert "a fact-checker found unsupported claims: no item cited" in llm.calls[2]["user"]

    @pytest.mark.parametrize("genre", ["timely", "weekly"])
    def test_news_thread_dropped_when_factcheck_is_down(self, settings, state, items, genre):
        w, llm = make_writer(settings, state, {"thread-": thread_responder({genre: thread_resp(genre)})})
        only(w, genre)
        assert w.draft_thread(items, [], "") is None
        assert llm.labels() == [f"thread-{genre}", "factcheck"]

    def test_timely_without_items_still_factchecked(self, settings, state):
        w, llm = make_writer(settings, state, {"thread-": thread_responder({"timely": thread_resp("timely")}),
                                               "factcheck": FC_OK})
        only(w, "timely")
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

    def test_cover_card_of_a_news_thread_can_be_the_page(self, settings, state, items):
        w, _ = make_writer(settings, state, {"thread-": thread_responder(
            {"timely": thread_resp("timely", source_ids=[1], visual_url="[1]", card_type="phone")}),
            "factcheck": FC_OK})
        only(w, "timely")
        d = w.draft_thread(items, [], "")
        assert d.visual == Visual("phone", items[0].url) and d.card["type"] == "phone"

    def test_bad_cover_card_retry_then_text_only(self, settings, state, items):
        bad = thread_resp("beginner", card_type="steps", card_lines=["Only one"])
        w, llm = make_writer(settings, state, {"thread-": thread_responder(
            {"beginner": [dict(bad), thread_resp("beginner")]}), "factcheck": FC_OK})
        only(w, "beginner")
        d = w.draft_thread(items, [], "")
        assert d.card["type"] == "list"
        assert "the image card was rejected" in llm.calls[1]["user"]
        w2, llm2 = make_writer(settings, state, {"thread-": thread_responder({"beginner": dict(bad)}),
                                                 "factcheck": FC_OK})
        only(w2, "beginner")
        d2 = w2.draft_thread(items, [], "")
        assert d2 is not None and d2.card is None and d2.texts == thread_tweets("beginner")

    def test_hook_too_similar_to_recent_thread(self, settings, state, items):
        state.remember_text("thread", thread_tweets("timely")[0])
        w, llm = make_writer(settings, state, {"thread-": thread_responder(), "factcheck": FC_OK})
        only(w, "timely")
        assert w.draft_thread(items, [], "") is None
        assert "too similar to a recent post" in llm.calls[1]["user"]

    def test_thread_notes(self, settings, state, items):
        w, llm = make_writer(settings, state, {"thread-": thread_responder(
            {"timely": thread_resp("timely", source_ids=[1], used_note_ids=[2])}), "factcheck": FC_OK})
        only(w, "timely")
        d = w.draft_thread(items, NOTES, "")
        assert d.used_notes == [NOTES[1]]
        assert HEADER in llm.calls[0]["user"]

    def test_topics_for_kind_ordering(self, settings, state):
        w, _ = make_writer(settings, state)
        topics = settings.evergreen_topics
        assert w.topics_for_kind(topics, "tutorial")[0] == "TUTORIAL — topic A"
        assert w.topics_for_kind(topics, "beginner")[0] == "BEGINNER — topic B"
        for seed in range(10):
            w.rng = random.Random(seed)
            out = w.topics_for_kind(topics, "timely")
            assert set(out[:2]) == {"STORY — topic C", "plain topic D"}   # sans étiquette connue d'abord
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
        topics_part = tut.split("Everyday topics you can pick from", 1)[1]
        assert topics_part.index("TUTORIAL — topic A") < topics_part.index("STORY — topic C")
        assert "Today is Thursday October 15." in tut


# ---------------------------------------------------------------------------
# Post réactif
# ---------------------------------------------------------------------------
REACT_TEXT = ("free voice mode for everyone is the quiet big one this week.\n"
              "the people who win: anyone who practices out loud before a hard conversation.")


def react_resp(post=True, text=REACT_TEXT, source_ids=(1,), **kw) -> dict:
    obj = {"post": post, "reason": "grosse annonce", "text": text if post else "",
           "source_ids": list(source_ids) if post else [], "used_note_ids": [],
           "card_type": "phone" if post else "none", "card_title": "ChatGPT voice is now *free*" if post else "",
           "card_lines": ["No paid plan needed", "Talk instead of typing"] if post else [], "card_note": "",
           "visual_url": "[1]" if post else ""}
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
        w, llm = make_writer(settings, state, {"reactive": react_resp(), "factcheck": FC_OK})
        d = w.draft_reactive(items + [stale, blocked], NOTES, "")
        assert d is not None and d.kind == "reactive" and d.texts == [REACT_TEXT]
        assert d.sources == [items[0]] and d.reason == "grosse annonce"
        assert d.visual == Visual("phone", items[0].url)
        assert d.card == {"type": "phone", "title": "ChatGPT voice is now *free*",
                          "lines": ["No paid plan needed", "Talk instead of typing"], "note": ""}
        assert "everyday people would care about RIGHT NOW" in llm.calls[0]["user"]
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
# Capture « smartphone » : Claude la regarde
# ---------------------------------------------------------------------------
class TestScreenshotCheck:
    def test_verdicts(self, settings, state):
        w, llm = make_writer(settings, state, {"screenshot-check": [
            {"ok": True, "issue": ""}, {"ok": False, "issue": "cookie banner over the headline"},
            {"ok": "yes", "issue": ""}]})
        assert w.screenshot_is_clean("a.jpg") == (True, "capture propre")
        assert w.screenshot_is_clean("a.jpg") == (False, "cookie banner over the headline")
        assert w.screenshot_is_clean("a.jpg") == (False, "capture pas assez propre")   # pas un vrai « oui »
        assert w.screenshot_is_clean("a.jpg") == (False, "vérification de l'image indisponible")
        call = llm.calls[0]
        assert call["images"] == ["a.jpg"] and call["effort"] == "low"
        assert "as seen on a phone" in call["system"] and "bottom edge may cut" in call["system"]


# ---------------------------------------------------------------------------
# Contenu des consignes (ligne éditoriale + algorithme X)
# ---------------------------------------------------------------------------
class TestPrompts:
    WRITERS = ("SYSTEM_PROMPT", "THREAD_SYSTEM_PROMPT", "REACTIVE_GATE_PROMPT")

    def test_growth_facts_in_prompts(self):
        for name in self.WRITERS:
            p = getattr(wmod, name)
            assert '"Not interested"' in p and "copy-link" in p and "FIRST LINE" in p, name
            assert "idiom-light" in p, name
        assert "ONLY TWEET 1 REACHES NEW PEOPLE" in wmod.THREAD_SYSTEM_PROMPT
        assert "Tweet 1 on its own" in wmod.JUDGE_SYSTEM_PROMPT
        assert "empty greeting" in wmod.GM_SYSTEM_PROMPT
        assert "copypasta" in wmod.RADAR_REPLY_PROMPT

    def test_editorial_line_in_prompts(self):
        for name in self.WRITERS:
            p = getattr(wmod, name)
            assert "Crypto, NFTs" in p and "Muslim" in p and "PERSONAL FACTS" in p, name
            assert "GROUNDING" in p and "No links and no domain names" in p and "260 characters" in p, name
        assert "max 200 characters" in wmod.GM_SYSTEM_PROMPT
        assert "Muslim" in wmod.RADAR_REPLY_PROMPT and "Muslim" in wmod.GM_SYSTEM_PROMPT

    def test_everyday_people_line_in_prompts(self):
        """Novembre 2026 : on écrit pour M. et Mme Tout-le-monde."""
        for name in self.WRITERS:
            p = getattr(wmod, name)
            assert "AI made simple, for everyday life" in p, name
            assert "WHO YOU WRITE FOR" in p and "a nurse, a teacher, a shop owner" in p, name
            assert "PLAIN WORDS ONLY" in p and "No tech jargon, ever" in p, name
            assert "CARD — every post goes out with an image card" in p, name
            assert "never makes you feel stupid" in p, name
            assert "image_prompt" not in p and "screenshot" not in p.lower(), name
        for name in ("SYSTEM_PROMPT", "REACTIVE_GATE_PROMPT"):
            p = getattr(wmod, name)
            assert "PLAYBOOK" in p and "NEWS IN PLAIN WORDS" in p and "Sentence case" in p, name
        assert "just [launched" not in wmod.SYSTEM_PROMPT            # l'ancien format « lancement »
        assert "stop a non-tech person scrolling" in wmod.JUDGE_SYSTEM_PROMPT
        assert "PLAIN-LANGUAGE CHECK" in wmod.FACTCHECK_PROMPT and "IMAGE CARD" in wmod.FACTCHECK_PROMPT
        assert "Most of the time the answer is NO" in wmod.REACTIVE_GATE_PROMPT

    def test_prompts_respect_their_own_rules(self):
        """Les exemples donnés à Claude ne contiennent ni lien ni sujet interdit."""
        from bot.filters import off_limits_term
        from bot.x_api import contains_url
        for name in ("PLAYBOOK", "AUDIENCE", "TONE", "CARD_RULES", "ORIGINALITY"):
            block = getattr(wmod, name)
            assert not off_limits_term(block), (name, off_limits_term(block))
            assert not contains_url(block), name
        for genre, angle in wmod.THREAD_ANGLES.items():
            assert not off_limits_term(angle) and not contains_url(angle), genre

    def test_fallback_texts_are_publishable(self):
        from bot.filters import publish_problem
        for kind, texts in wmod.DEFAULT_FALLBACKS.items():
            for t in texts:
                assert publish_problem(t, max_len=wmod.GM_MAX if kind == "gm" else wmod.POST_MAX,
                                       check_numbers=False, plain=True) is None, t



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



def test_regeneration_is_not_rejected_for_resembling_the_version_it_replaces(settings, state, items):
    item = state.new_item("tip", [TIP_TEXT], status="awaiting")
    w, llm = make_writer(settings, state, {"post-tip": post_resp(TIP_TEXT_2), "factcheck": FC_OK})
    w.regen_item_id = item["id"]
    d = w.draft_post("tip", items, [], "")
    assert d is not None and d.texts == [TIP_TEXT_2]
    assert TIP_TEXT.split("\n")[0] not in [c for c in llm.calls if c["label"] == "post-tip"][0]["user"]
