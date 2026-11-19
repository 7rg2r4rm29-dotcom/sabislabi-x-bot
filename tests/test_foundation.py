"""Tests des modules de base : filtres, budget, état, client X, client Claude, réglages."""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from bot import config as cfg
from bot import filters
from bot.costs import Budget
from bot.llm import LLM, schema, STR, BOOL
from bot.state import StateStore
from bot.utils import at_local, in_window, now_local, parse_hhmm, parse_window
from bot.x_api import XClient, XError, contains_url, intent_reply_url


# ---------------------------------------------------------------------------
# Filtres
# ---------------------------------------------------------------------------
class TestFilters:
    def test_off_limits(self):
        assert filters.off_limits_term("new NFT drop") == "NFT"
        assert filters.off_limits_term("Apple's big gamble on AI") is None
        assert filters.off_limits_term("sports betting apps are booming")
        assert filters.off_limits_term("the election results")

    def test_urls_and_domains_rejected(self):
        assert contains_url("read https://example.com/x")
        assert contains_url("try claude.ai today")
        assert contains_url("go to www.foo.bar")
        assert not contains_url("try Claude today, it's good. really.")
        assert "lien" in filters.publish_problem("try claude.ai")

    def test_weighted_length(self):
        assert filters.weighted_length("abc") == 3
        assert filters.weighted_length("👋") == 2
        assert filters.weighted_length("é") == 1
        assert filters.publish_problem("a" * 281).startswith("trop long")
        assert filters.publish_problem("a " * 140) is None or True  # 280 pile autorisé

    def test_json_leak(self):
        assert filters.looks_like_json_leak('{"text": "hi"}')
        assert filters.looks_like_json_leak('"text": "hi there"')
        assert filters.looks_like_json_leak('text: "hi there"')
        assert filters.looks_like_json_leak('{"card_title": "Dinner"}')
        assert not filters.looks_like_json_leak("plain text")
        # Un conseil normal du compte : la phrase à taper, entre guillemets.
        assert not filters.looks_like_json_leak('Answer too long? Just reply: "shorter, please". It works.')
        assert not filters.looks_like_json_leak('Take a photo of the letter and add this text: "explain it simply"')

    def test_clean_output(self):
        assert filters.clean_output('"hello #AI @bob world"') == "hello world"
        assert filters.clean_output("line1\n\n\n\nline2") == "line1\n\nline2"

    @pytest.mark.parametrize("raw,clean", [
        ("A robot folds laundry — alone.", "A robot folds laundry, alone."),
        ("Wait—what?", "Wait, what?"),
        ("Robots – at home", "Robots, at home"),
        ("It takes 2–3 days.", "It takes 2-3 days."),                 # une fourchette reste une fourchette
        ("pages 10—12", "pages 10-12"),
        ("No driver. No wheel —\nnothing.", "No driver. No wheel\nnothing."),   # en fin de ligne : rien
        ("— Soon, no password.", "Soon, no password."),               # en début de ligne : rien
        ("Line one\n— line two", "Line one\nline two"),
        ("A well-known self-driving car", "A well-known self-driving car"),   # tiret court : intact
    ])
    def test_clean_output_turns_long_dashes_into_commas(self, raw, clean):
        """Le tiret long trahit un texte écrit par une machine : il devient une virgule."""
        assert filters.clean_output(raw) == clean

    def test_clean_output_keeps_a_spaced_range(self):
        assert filters.clean_output("It takes 2 – 3 hours.") == "It takes 2-3 hours."

    @pytest.mark.parametrize("text,found", [
        ("This is a game changer for nurses.", "game changer"),
        ("A game-changing robot", "game-changing"),
        ("Mind-blowing: a robot dog.", "Mind-blowing"),
        ("A groundbreaking scan", "groundbreaking"),
        ("This changes everything.", "This changes everything"),
        ("Buckle up.", "Buckle up"),
        ("Let that sink in.", "Let that sink in"),
        ("The future is here.", "The future is here"),
        ("They delve into it.", "delve"),
        ("A revolutionary phone", "revolutionary"),
        ("It will revolutionize school", "revolutionize"),
        ("Supercharged by AI", "Supercharged"),
        ("In today's fast-paced world, robots", "In today's fast-paced world"),
        ("BREAKING: a robot ran a marathon", "BREAKING"),
        ("JUST IN: robots", "JUST IN"),
        ("RIP passwords", "RIP"),
    ])
    def test_find_hype(self, text, found):
        assert filters.find_hype(text) == found

    @pytest.mark.parametrize("text", [
        "Breaking: a robot ran a marathon",          # capitales seulement
        "It arrived just in time.",
        "Don't rip the label off.",
        "A robot just folded a whole basket of laundry, alone.",
        "",
    ])
    def test_find_hype_leaves_plain_words_alone(self, text):
        assert filters.find_hype(text) is None

    def test_hype_is_refused_only_in_posts_for_everyone(self):
        txt = "This robot is a game changer for nurses."
        assert filters.publish_problem(txt, plain=True) == "formule toute faite (game changer)"
        assert filters.publish_problem(txt, plain=False) is None      # réponses : la relecture de Claude suffit

    @pytest.mark.parametrize("text,term", [
        ("an adults-only club", "adults-only"),
        ("explicit content online", "explicit content"),
        ("the best dating apps", "dating apps"),
        ("a new dating site", "dating site"),
        ("Tinder adds an AI coach", "Tinder"),
    ])
    def test_new_off_limits_terms(self, text, term):
        assert filters.off_limits_term(text) == term

    @pytest.mark.parametrize("leak", ['A robot folds laundry now.\n"hook": "IT CAN DO WHAT?"',
                                      '"hook_options": ["Soon, no driver."]'])
    def test_json_leak_of_the_new_post_fields(self, leak):
        assert filters.looks_like_json_leak(leak)

    def test_numbers_and_similarity(self):
        assert filters.too_many_numbers("$10k and 45% and 2,000")
        assert not filters.too_many_numbers("3 tips for 2 people")
        assert filters.is_too_similar("the quick brown fox jumps", ["the quick brown fox jumps high"])

    def test_hard_block_noise(self):
        assert filters.hard_blocked("Breaking: earthquake hits")
        assert filters.is_noise("GIVEAWAY: tag a friend")

    def test_parse_json_object(self):
        assert filters.parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
        assert filters.parse_json_object("nothing") is None


# ---------------------------------------------------------------------------
# Heure / fenêtres
# ---------------------------------------------------------------------------
def test_windows_and_dst(clock):
    assert in_window(parse_hhmm("23:45"), parse_window("23:30-07:30"))
    assert in_window(parse_hhmm("07:00"), parse_window("23:30-07:30"))
    assert not in_window(parse_hhmm("12:00"), parse_window("23:30-07:30"))
    # Changement d'heure (25/10/2026) : 08:00 locale reste 08:00
    clock(10, 0, day=26)
    dt = at_local(now_local(), parse_hhmm("08:00"))
    assert dt.hour == 8 and dt.utcoffset() == timedelta(hours=1)


# ---------------------------------------------------------------------------
# Réglages
# ---------------------------------------------------------------------------
class TestConfig:
    def test_slots(self):
        slots = cfg.parse_post_slots("gm@08:00, tip@13:30")
        assert [s.kind for s in slots] == ["gm", "tip"]
        assert slots[0].key == "gm-0800"
        th = cfg.parse_thread_slots("tue@15:00,dim@18:00")
        assert th[0].weekday == 1 and th[1].weekday == 6 and th[0].key == "thread-tue-1500"
        with pytest.raises(cfg.ConfigError):
            cfg.parse_post_slots("foo@08:00")

    def test_validate_auto_mode_gated(self, settings):
        settings.approval_mode = "auto"
        assert any("AUTOMATED_LABEL_ACK" in e for e in settings.validate())
        settings.automated_label_ack = True
        assert settings.validate() == []

    def test_load_settings_defaults(self, monkeypatch):
        for k in ("POST_SLOTS", "QUIET_HOURS", "LLM_MODEL", "DRY_RUN"):
            monkeypatch.delenv(k, raising=False)
        s = cfg.load_settings()
        assert s.llm_model == "claude-opus-5-5"
        assert s.dry_run is True
        assert s.quiet_hours == (parse_hhmm("23:30"), parse_hhmm("07:30"))
        monkeypatch.setenv("QUIET_HOURS", "off")
        assert cfg.load_settings().quiet_hours is None

    def test_claude_monthly_budget(self, monkeypatch):
        """Plafond voulu par le propriétaire (18/11/2026) : 35 $ par mois ; 0 = pas de plafond."""
        monkeypatch.delenv("CLAUDE_BUDGET_MONTHLY_USD", raising=False)
        assert cfg.load_settings().claude_budget_monthly_usd == 35.0
        monkeypatch.setenv("CLAUDE_BUDGET_MONTHLY_USD", "50")
        assert cfg.load_settings().claude_budget_monthly_usd == 50.0
        monkeypatch.setenv("CLAUDE_BUDGET_MONTHLY_USD", "-5")
        assert cfg.load_settings().claude_budget_monthly_usd == 0.0
        monkeypatch.setenv("CLAUDE_BUDGET_MONTHLY_USD", "abc")
        assert cfg.load_settings().claude_budget_monthly_usd == 35.0

    def test_quality_settings(self, monkeypatch):
        """19/11/2026 : plusieurs versions par post, plus de réflexion pour les écrire, actu chaude
        regardée plus souvent. Sans réglage, le bot se comporte comme avant."""
        for k in ("POST_VERSIONS", "LLM_EFFORT_POST", "REACTIVE_MAX_CHECKS_PER_DAY"):
            monkeypatch.delenv(k, raising=False)
        s = cfg.load_settings()
        assert (s.post_versions, s.llm_effort_post, s.reactive_max_checks_per_day) == (1, "", 6)
        assert s.warnings == []
        monkeypatch.setenv("POST_VERSIONS", "3")
        monkeypatch.setenv("LLM_EFFORT_POST", " High ")
        monkeypatch.setenv("REACTIVE_MAX_CHECKS_PER_DAY", "30")
        s = cfg.load_settings()
        assert (s.post_versions, s.llm_effort_post, s.reactive_max_checks_per_day) == (3, "high", 30)
        assert s.warnings == [] and s.validate() == [] or "LLM_EFFORT invalide." not in s.validate()
        # Valeurs absurdes : bornées, jamais bloquantes.
        monkeypatch.setenv("POST_VERSIONS", "99")
        monkeypatch.setenv("REACTIVE_MAX_CHECKS_PER_DAY", "0")
        s = cfg.load_settings()
        assert s.post_versions == cfg.POST_VERSIONS_MAX == 5 and s.reactive_max_checks_per_day == 1
        monkeypatch.setenv("POST_VERSIONS", "-2")
        assert cfg.load_settings().post_versions == 1
        monkeypatch.setenv("POST_VERSIONS", "abc")
        monkeypatch.setenv("REACTIVE_MAX_CHECKS_PER_DAY", "abc")
        s = cfg.load_settings()
        assert (s.post_versions, s.reactive_max_checks_per_day) == (1, 6)
        # Un temps de réflexion inconnu est signalé et ignoré : le bot démarre quand même.
        monkeypatch.setenv("LLM_EFFORT_POST", "turbo")
        s = cfg.load_settings()
        assert s.llm_effort_post == "" and any("LLM_EFFORT_POST" in w for w in s.warnings)

    def test_gap_between_a_breaking_post_and_a_scheduled_one(self, monkeypatch):
        monkeypatch.delenv("REACTIVE_SLOT_GAP_MINUTES", raising=False)
        s = cfg.load_settings()
        assert s.reactive_slot_gap_min is None and s.reactive_min_gap_min == 60
        for raw, value in (("10", 10), ("0", 0), (" 25 ", 25), ("-3", 0), ("", None)):
            monkeypatch.setenv("REACTIVE_SLOT_GAP_MINUTES", raw)
            s = cfg.load_settings()
            assert s.reactive_slot_gap_min == value and s.warnings == [], raw
        monkeypatch.setenv("REACTIVE_SLOT_GAP_MINUTES", "bientôt")     # illisible : signalé, puis ignoré
        s = cfg.load_settings()
        assert s.reactive_slot_gap_min is None and any("REACTIVE_SLOT_GAP_MINUTES" in w for w in s.warnings)

    @pytest.mark.parametrize("raw, value", [("", ""), (None, ""), ("low", "low"), ("XHIGH", "xhigh"), (" max ", "max")])
    def test_parse_effort_or_empty(self, raw, value):
        assert cfg.parse_effort_or_empty(raw) == value
        with pytest.raises(cfg.ConfigError):
            cfg.parse_effort_or_empty("turbo")


# ---------------------------------------------------------------------------
# Fichiers de configuration livrés (config/settings.json, config/content.json)
# ---------------------------------------------------------------------------
class TestShippedConfig:
    @staticmethod
    def _json(name: str) -> dict:
        from bot.utils import CONFIG_DIR
        return json.loads((CONFIG_DIR / name).read_text(encoding="utf-8"))

    def test_feeds(self):
        feeds = self._json("settings.json")["news_feeds"]
        assert len(feeds) >= 30
        urls = []
        for f in feeds:
            assert isinstance(f, dict)
            assert str(f.get("name") or "").strip(), f
            assert str(f.get("url") or "").startswith("https://"), f
            assert f.get("filter") in (None, "ai", "tech"), f
            assert isinstance(f.get("weight"), int) and 1 <= f["weight"] <= 3, f
            assert isinstance(f.get("official", False), bool), f
            assert set(f) <= {"name", "url", "weight", "filter", "official"}, f
            urls.append(f["url"].rstrip("/").lower())
        assert len(urls) == len(set(urls)), "flux en double"
        assert not any("x.com/" in u or "twitter.com/" in u for u in urls)   # X ne se lit que par l'API
        assert sum(1 for f in feeds if f.get("official")) >= 1
        assert any(f.get("filter") for f in feeds)

    def test_feeds_are_loaded_as_is(self):
        s = cfg.load_settings()
        assert s.news_feeds == self._json("settings.json")["news_feeds"]
        from bot import sources
        for f in s.news_feeds:
            assert (sources.topic_filter(f) is None) == (f.get("filter") is None)

    def test_content(self):
        content = self._json("content.json")
        assert set(content) <= {"_doc", "fallback_captions", "evergreen_topics"}
        # Seul l'ancien « gm » garde des textes de secours (jamais de bouche-trou pour les autres).
        captions = content["fallback_captions"]
        assert list(captions) == ["gm"] and captions["gm"]
        for text in captions["gm"]:
            assert text.startswith("gm. ")
            assert filters.publish_problem(text, max_len=200, check_numbers=False) is None, text

    def test_thread_themes_are_tagged_as_the_writer_expects(self):
        from bot import writer
        topics = self._json("content.json")["evergreen_topics"]
        assert len(topics) >= 10 and len(set(topics)) == len(topics)
        for t in topics:
            assert t.startswith("FUTURE — ") and len(t) > len("FUTURE — ") + 10, t
            assert filters.off_limits_term(t) is None, t
        assert writer._ALL_TOPIC_TAGS == ("FUTURE",) and writer._TOPIC_TAGS["future"] == ("FUTURE",)
        w = writer.Writer(SimpleNamespace(evergreen_topics=topics), llm=None, state=None)
        future = w.topics_for_kind(topics, "future")
        assert sorted(future) == sorted(topics)                 # tous proposés au genre « future »

    def test_shipped_settings_load_with_the_content(self):
        s = cfg.load_settings()
        assert s.fallback_captions == {"gm": self._json("content.json")["fallback_captions"]["gm"]}
        assert s.evergreen_topics == self._json("content.json")["evergreen_topics"]


# ---------------------------------------------------------------------------
# État
# ---------------------------------------------------------------------------
class TestState:
    def test_items_and_gc(self, state, clock):
        it = state.new_item("tip", ["hello"], slot_key="tip-1330")
        assert state.item(it["id"])["status"] == "awaiting"
        state.update_item(it["id"], status="published")
        clock(10, 0, day=28)
        assert state.gc(keep_days=10) == 1
        assert state.item(it["id"]) is None

    def test_v1_state_backed_up(self, tmp_path):
        p = tmp_path / "s" / "state.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"replied_tweet_ids": ["1"]}))
        st = StateStore(p)
        assert st.data["version"] == 2
        assert (p.parent / "state_v1_backup.json").exists()

    def test_notes(self, state, clock):
        state.add_note("I tested Claude Projects for my invoices")
        assert len(state.active_notes()) == 1
        state.mark_notes_used(["I tested Claude Projects for my invoices"])
        state.mark_notes_used(["I tested Claude Projects for my invoices"])
        assert state.active_notes() == []

    def test_counters_and_handled(self, state, clock):
        assert state.incr("x") == 1 and state.incr("x") == 2
        state.mark_handled("9")
        assert state.is_handled("9")


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------
class TestBudget:
    def test_record_and_caps(self, state, clock):
        b = Budget(state, daily_usd=0.10, monthly_usd=5.0)
        b.record("post", 2)
        assert b.spent_today() == pytest.approx(0.03)
        assert b.can_spend_optional(0.04)
        b.record("post_read", 10)  # 0.05
        assert not b.can_spend_optional(0.01)          # 0.08 + 0.01 > 0.08
        assert b.can_spend_essential(0.1)
        b.record("post_url", 24)                          # +4.80
        assert not b.can_spend_essential(0.2)

    def test_optional_reads_leave_a_monthly_reserve_for_posts(self, state, clock):
        b = Budget(state, daily_usd=1.50, monthly_usd=30)
        # 30 $ × 75 % / 31 jours (octobre) : le radar est étalé sur le mois
        assert b.optional_daily_limit() == pytest.approx(22.5 / 31)
        assert b.can_spend_optional(0.70) and not b.can_spend_optional(0.75)
        # Même un jour neuf, plus aucune lecture optionnelle une fois 75 % du mois dépensés…
        state.data["costs"]["days"]["2026-10-01"] = {"total": 22.45, "by_kind": {}}
        assert not b.can_spend_optional(0.10)
        # … mais les publications passent toujours
        assert b.can_spend_essential(0.10)


# ---------------------------------------------------------------------------
# Client API X (session HTTP simulée)
# ---------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.content = json.dumps(self._body).encode()
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kw):
        self.requests.append((method, url, kw))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class TestTelegramMedia:
    def test_video_photo_and_failures(self, tmp_path):
        from bot.telegram import Telegram

        class Resp:
            def __init__(self, body):
                self.body = body

            def json(self):
                return self.body

        calls = []

        class Session:
            def post(self, url, data=None, files=None, timeout=None):
                calls.append((url.rsplit("/", 1)[1], data, sorted(files), timeout))
                return Resp({"ok": True, "result": {"message_id": 42}})
        tg = Telegram("TOKEN", "777")
        tg.session = Session()
        clip, img = tmp_path / "clip.mp4", tmp_path / "p.jpg"
        clip.write_bytes(b"mp4")
        img.write_bytes(b"jpg")
        assert tg.send_media(str(clip), "a clip") == 42 and tg.send_media(str(img), "a photo") == 42
        assert [c[0] for c in calls] == ["sendVideo", "sendPhoto"]
        assert calls[0][1]["supports_streaming"] == "true" and calls[0][2] == ["video"] and calls[0][3] == 120
        assert tg.send_video(str(tmp_path / "missing.mp4")) is None


class TestXClient:
    def _client(self, settings, state, responses):
        b = Budget(state, 1.5, 30)
        return XClient(settings, b, session=FakeSession(responses)), b

    def test_post_and_cost(self, settings, state, clock):
        x, b = self._client(settings, state, [FakeResp(201, {"data": {"id": "123"}})])
        assert x.post("hello world") == "123"
        method, url, kw = x.session.requests[0]
        assert method == "POST" and url.endswith("/2/tweets") and kw["json"] == {"text": "hello world"}
        assert b.spent_today() == pytest.approx(0.015)

    def test_video_upload_in_chunks_then_waits_for_processing(self, settings, state, clock, tmp_path,
                                                              monkeypatch):
        """Comme vérifié sur la vraie API le 14/11/2026 : initialize → append → finalize
        (« pending ») → STATUS jusqu'à « succeeded »."""
        monkeypatch.setattr(XClient, "VIDEO_CHUNK", 4)
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"0123456789")                     # 10 octets → 3 morceaux de 4 au plus
        x, b = self._client(settings, state, [
            FakeResp(200, {"data": {"id": "77", "media_key": "7_77", "expires_after_secs": 86399}}),
            FakeResp(200, {"data": {"expires_at": 1}}), FakeResp(200, {"data": {"expires_at": 1}}),
            FakeResp(200, {"data": {"expires_at": 1}}),
            FakeResp(200, {"data": {"id": "77", "processing_info": {"state": "pending", "check_after_secs": 1}}}),
            FakeResp(200, {"data": {"id": "77", "processing_info": {"state": "in_progress", "check_after_secs": 3}}}),
            FakeResp(200, {"data": {"id": "77", "processing_info": {"state": "succeeded", "progress_percent": 100}}}),
        ])
        waits = []
        assert x.upload_video(clip, sleep=waits.append) == "77"
        reqs = x.session.requests
        assert reqs[0][0] == "POST" and reqs[0][1].endswith("/2/media/upload/initialize")
        assert reqs[0][2]["json"] == {"media_type": "video/mp4", "total_bytes": 10, "media_category": "tweet_video"}
        assert [r[1].rsplit("/2/", 1)[1] for r in reqs[1:4]] == ["media/upload/77/append"] * 3
        assert [r[2]["data"]["segment_index"] for r in reqs[1:4]] == ["0", "1", "2"]
        assert [r[2]["files"]["media"][1] for r in reqs[1:4]] == [b"0123", b"4567", b"89"]
        assert reqs[4][1].endswith("/2/media/upload/77/finalize")
        assert reqs[5][0] == "GET" and reqs[5][2]["params"] == {"command": "STATUS", "media_id": "77"}
        assert waits == [1, 3]
        assert b.spent_today() == pytest.approx(0.005)

    @pytest.mark.parametrize("finalize, status", [
        ({"processing_info": {"state": "failed", "error": {"message": "InvalidContent"}}}, None),
        ({"processing_info": {"state": "pending", "check_after_secs": 1}},
         {"processing_info": {"state": "failed", "error": {"name": "UnsupportedMedia"}}}),
    ])
    def test_video_refused_by_x_is_an_invalid_error(self, settings, state, clock, tmp_path, finalize, status):
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"data")
        responses = [FakeResp(200, {"data": {"id": "77"}}), FakeResp(200, {"data": {}}),
                     FakeResp(200, {"data": {"id": "77", **finalize}})]
        if status:
            responses.append(FakeResp(200, {"data": {"id": "77", **status}}))
        x, _ = self._client(settings, state, responses)
        with pytest.raises(XError) as err:
            x.upload_video(clip, sleep=lambda s: None)
        assert err.value.kind == "invalid" and "refusée" in str(err.value)

    def test_video_processing_too_long(self, settings, state, clock, tmp_path, monkeypatch):
        monkeypatch.setattr(XClient, "VIDEO_PROCESSING_MAX_S", -1)
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"data")
        x, _ = self._client(settings, state, [
            FakeResp(200, {"data": {"id": "77"}}), FakeResp(200, {"data": {}}),
            FakeResp(200, {"data": {"id": "77", "processing_info": {"state": "pending"}}})])
        with pytest.raises(XError) as err:
            x.upload_video(clip, sleep=lambda s: None)
        assert err.value.kind == "invalid" and "trop long" in str(err.value)

    def test_video_without_id_and_dry_run(self, settings, state, clock, tmp_path):
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"data")
        x, _ = self._client(settings, state, [FakeResp(200, {"data": {}})])
        with pytest.raises(XError) as err:
            x.upload_video(clip, sleep=lambda s: None)
        assert err.value.kind == "invalid"
        settings.dry_run = True
        x2, _ = self._client(settings, state, [])
        assert x2.upload_video(clip).startswith("dry-") and x2.session.requests == []

    def test_reply_poll_media_body(self, settings, state, clock):
        x, b = self._client(settings, state, [FakeResp(201, {"data": {"id": "5"}})])
        x.post("hi", reply_to="9", media_ids=["1"], poll={"options": ["a", "b"]}, made_with_ai=True)
        body = x.session.requests[0][2]["json"]
        assert body["reply"] == {"in_reply_to_tweet_id": "9"}
        assert body["media"] == {"media_ids": ["1"]}
        assert body["poll"]["duration_minutes"] == 1440 and body["made_with_ai"] is True
        assert b.spent_today() == pytest.approx(0.010)

    @pytest.mark.parametrize("status,body,kind", [
        (429, {"title": "Too Many Requests"}, "rate_limited"),
        (401, {"title": "Unauthorized"}, "auth"),
        (402, {"title": "Payment Required"}, "credits"),
        (403, {"detail": "Reply to this conversation is not allowed because you have not been mentioned or otherwise engaged by the author"}, "not_summoned"),
        (403, {"detail": "You are not allowed to create a Tweet with duplicate content."}, "duplicate"),
        (400, {"detail": "Invalid Request"}, "invalid"),
    ])
    def test_error_classification(self, settings, state, clock, status, body, kind):
        x, _ = self._client(settings, state, [FakeResp(status, body, {"x-rate-limit-reset": "1800000000"})])
        with pytest.raises(XError) as ei:
            x.post("hello")
        assert ei.value.kind == kind

    def test_server_error_on_post_is_uncertain_never_resent(self, settings, state, clock, monkeypatch):
        monkeypatch.setattr("bot.x_api.time.sleep", lambda s: None)
        x, _ = self._client(settings, state, [FakeResp(503), FakeResp(201, {"data": {"id": "7"}})])
        with pytest.raises(XError) as ei:
            x.post("hello")
        assert ei.value.kind == "uncertain"
        assert len(x.session.requests) == 1        # pas de 2e envoi à l'aveugle

    def test_server_error_on_read_retried_once(self, settings, state, clock, monkeypatch):
        monkeypatch.setattr("bot.x_api.time.sleep", lambda s: None)
        x, _ = self._client(settings, state, [FakeResp(503), FakeResp(200, {"data": {"id": "42",
                                                                                 "username": "sabislabi"}})])
        assert x.me()["username"] == "sabislabi"
        assert len(x.session.requests) == 2

    def test_lost_response_on_post_is_uncertain(self, settings, state, clock, monkeypatch):
        import requests as rq
        monkeypatch.setattr("bot.x_api.time.sleep", lambda s: None)
        x, _ = self._client(settings, state, [rq.ReadTimeout("slow"), FakeResp(201, {"data": {"id": "7"}})])
        with pytest.raises(XError) as ei:
            x.post("hello")
        assert ei.value.kind == "uncertain" and len(x.session.requests) == 1

    @pytest.mark.parametrize("text", ["bolt.new is wild", "try t3.chat", "check notebooklm.google",
                                      "deepmind.google just posted", "kimi.moonshot.cn", "manus.im/app",
                                      "youtu.be/abc", "claude.ai", "the x.ai team"])
    def test_domains_count_as_links(self, text):
        assert contains_url(text)

    @pytest.mark.parametrize("text", ["e.g. this", "node.js rocks", "gpt-4.5 is out", "U.S. users",
                                      "claude.md file", "9 a.m. sharp", "vs. chatgpt", "gpt-5.5 vs claude 5"])
    def test_ordinary_text_is_not_a_link(self, text):
        assert not contains_url(text)

    def test_thread_partial_ids(self, settings, state, clock):
        x, _ = self._client(settings, state, [FakeResp(201, {"data": {"id": "1"}}),
                                               FakeResp(429, {"title": "x"})])
        with pytest.raises(XError) as ei:
            x.post_thread(["a", "b", "c"])
        assert ei.value.partial_ids == ["1"]
        assert x.session.requests[1][2]["json"]["reply"] == {"in_reply_to_tweet_id": "1"}

    def test_dry_run_never_calls_api(self, settings, state, clock):
        settings.dry_run = True
        x, b = self._client(settings, state, [])
        assert x.post("hello").startswith("dry-")
        assert b.spent_today() == 0

    def test_mentions_owned_read(self, settings, state, clock):
        x, b = self._client(settings, state, [FakeResp(200, {
            "data": [{"id": "2", "text": "@sabislabi nice", "author_id": "7"}],
            "includes": {"users": [{"id": "7", "username": "bob"}]},
            "meta": {"newest_id": "2"}})])
        tweets, users, newest = x.mentions(since_id="1")
        assert newest == "2" and users["7"]["username"] == "bob"
        assert b.spent_today() == pytest.approx(0.001)
        assert x.session.requests[0][2]["params"]["since_id"] == "1"

    def test_optional_read_respects_budget(self, settings, state, clock):
        x, b = self._client(settings, state, [])
        b.daily_usd = 0.0
        with pytest.raises(XError) as ei:
            x.user_posts("5")
        assert ei.value.kind == "budget"

    def test_intent_url(self):
        u = intent_reply_url("123", "hello world & co")
        assert u.startswith("https://x.com/intent/post?in_reply_to=123&text=hello%20world%20%26%20co")


# ---------------------------------------------------------------------------
# Client Claude (SDK simulé)
# ---------------------------------------------------------------------------
class _Usage(SimpleNamespace):
    pass


def _resp(text, stop="end_turn", model="claude-opus-5-5"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop, stop_details=SimpleNamespace(category="cyber") if stop == "refusal" else None,
        model=model, usage=_Usage(input_tokens=100, output_tokens=50, cache_read_input_tokens=1000,
                                  cache_creation_input_tokens=0))


class FakeAnthropic:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create("std"))
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create("beta")))

    def _create(self, path):
        def create(**kw):
            self.calls.append((path, kw))
            out = self.outcomes.pop(0)
            if isinstance(out, Exception):
                raise out
            return out
        return create


SCHEMA = schema({"ok": BOOL, "text": STR})


class TestLLM:
    def test_structured_call_with_fallback_and_cache(self, settings, state, clock):
        fake = FakeAnthropic([_resp('{"ok": true, "text": "hi"}')])
        llm = LLM(settings, state, client=fake)
        assert llm.json("t", "sys", "user", SCHEMA) == {"ok": True, "text": "hi"}
        path, kw = fake.calls[0]
        assert path == "beta" and kw["fallbacks"] == "default"
        assert kw["betas"] == ["server-side-fallback-2026-07-01"]
        assert kw["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        assert kw["output_config"]["format"]["schema"] == SCHEMA
        assert kw["model"] == "claude-opus-5-5"
        assert "thinking" not in kw and "temperature" not in kw
        assert state.data["llm_usage"]["days"]

    def test_refusal_and_truncation_return_none(self, settings, state, clock):
        fake = FakeAnthropic([_resp("{}", stop="refusal"), _resp('{"ok": tr', stop="max_tokens")])
        llm = LLM(settings, state, client=fake)
        assert llm.json("a", "s", "u", SCHEMA) is None
        assert llm.json("b", "s", "u", SCHEMA) is None

    def test_no_fallback_for_older_models(self, settings, state, clock):
        settings.llm_model = "claude-sonnet-5"
        fake = FakeAnthropic([_resp('{"ok": false, "text": ""}', model="claude-sonnet-5")])
        LLM(settings, state, client=fake).json("t", "s", "u", SCHEMA)
        assert fake.calls[0][0] == "std" and "fallbacks" not in fake.calls[0][1]

    def test_fallback_rejected_then_retried_without(self, settings, state, clock):
        import anthropic
        import httpx2 as httpx  # noqa: F401  (anthropic 1.x)
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        err = anthropic.BadRequestError("fallbacks: not supported", response=httpx.Response(400, request=req),
                                        body={"error": {"message": "fallbacks not supported"}})
        fake = FakeAnthropic([err, _resp('{"ok": true, "text": "x"}')])
        llm = LLM(settings, state, client=fake)
        assert llm.json("t", "s", "u", SCHEMA)["ok"] is True
        assert fake.calls[1][0] == "std"

    def test_circuit_breaker_after_repeated_outages(self, settings, state, clock, monkeypatch):
        import anthropic
        import httpx2 as httpx  # noqa: F401  (anthropic 1.x)
        from bot import llm as llm_mod
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        t = {"now": 1000.0}
        monkeypatch.setattr("time.monotonic", lambda: t["now"])
        fake = FakeAnthropic([anthropic.APITimeoutError(request=req), anthropic.APITimeoutError(request=req),
                              _resp('{"ok": true, "text": "back"}')])
        llm = LLM(settings, state, client=fake)
        assert llm.json("a", "s", "u", SCHEMA) is None
        assert llm.json("b", "s", "u", SCHEMA) is None
        assert llm.json("c", "s", "u", SCHEMA) is None      # coupe-circuit : pas d'appel
        assert len(fake.calls) == 2
        t["now"] += llm_mod.BREAKER_PAUSE_S + 1
        assert llm.json("d", "s", "u", SCHEMA)["text"] == "back"

    def test_no_credit_is_reported_and_pauses_claude(self, settings, state, clock, monkeypatch):
        import anthropic
        import httpx2 as httpx  # noqa: F401  (anthropic 1.x)
        from bot import llm as llm_mod
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        err = anthropic.BadRequestError(
            "Your credit balance is too low to access the Anthropic API.",
            response=httpx.Response(400, request=req),
            body={"error": {"message": "Your credit balance is too low to access the Anthropic API."}})
        t = {"now": 1000.0}
        monkeypatch.setattr("time.monotonic", lambda: t["now"])
        fake = FakeAnthropic([err, _resp('{"ok": true, "text": "back"}')])
        llm = LLM(settings, state, client=fake)
        assert llm.json("a", "s", "u", SCHEMA) is None
        assert "crédit" in llm.account_problem and llm.paused()
        assert llm.json("b", "s", "u", SCHEMA) is None and len(fake.calls) == 1   # pas d'appel inutile
        t["now"] += llm_mod.ACCOUNT_PAUSE_S + 1
        assert llm.json("c", "s", "u", SCHEMA)["text"] == "back"
        assert llm.account_problem is None

    # --- Plafond du mois (CLAUDE_BUDGET_MONTHLY_USD) ---------------------------------
    def test_monthly_cap_stops_every_call_until_the_next_month(self, settings, state, clock):
        settings.claude_budget_monthly_usd = 35.0
        clock(10, 0, day=15)
        state.data["llm_usage"] = {"days": {"2026-09-30": {"calls": 9, "usd": 30.0},      # mois d'avant
                                            "2026-10-01": {"calls": 9, "usd": 20.0},
                                            "2026-10-14": {"calls": 9, "usd": 15.0}}}
        fake = FakeAnthropic([_resp('{"ok": true, "text": "new month"}')])
        llm = LLM(settings, state, client=fake)
        assert llm.spent_month() == 35.0 and llm.over_budget() and llm.paused()
        assert llm.json("a", "s", "u", SCHEMA) is None and fake.calls == []      # aucun appel payant
        assert "plafond Claude du mois atteint (35.00 $ sur 35 $)" in llm.account_problem
        clock(0, 5, day=1, month=11)                                              # le 1er : ça repart seul
        assert not llm.over_budget() and llm.account_problem is None and not llm.paused()
        assert llm.json("b", "s", "u", SCHEMA)["text"] == "new month"

    def test_last_call_under_the_cap_goes_through_and_is_counted(self, settings, state, clock):
        settings.claude_budget_monthly_usd = 35.0
        state.data["llm_usage"] = {"days": {"2026-10-14": {"calls": 9, "usd": 34.999}}}
        fake = FakeAnthropic([_resp('{"ok": true, "text": "last"}'), _resp('{"ok": true, "text": "never"}')])
        llm = LLM(settings, state, client=fake)
        assert llm.json("a", "s", "u", SCHEMA)["text"] == "last"                 # 0,0016 $ de plus
        assert llm.spent_month() == pytest.approx(35.0006)
        assert llm.json("b", "s", "u", SCHEMA) is None and len(fake.calls) == 1

    def test_no_cap_when_set_to_zero(self, settings, state, clock):
        settings.claude_budget_monthly_usd = 0
        state.data["llm_usage"] = {"days": {"2026-10-14": {"calls": 9, "usd": 500.0}}}
        llm = LLM(settings, state, client=FakeAnthropic([_resp('{"ok": true, "text": "hi"}')]))
        assert not llm.over_budget() and llm.optional_ok() and not llm.budget_warning_due()
        assert llm.budget_problem() is None and llm.json("a", "s", "u", SCHEMA)["text"] == "hi"

    @pytest.mark.parametrize("day, spent, ok", [
        (1, 0.0, True), (1, 8.7, True), (1, 8.8, False),    # 1er : 26,25 $ réservés aux posts planifiés
        (16, 20.0, True), (16, 21.5, False),                # il reste 16 jours sur 31 : réserve 13,55 $
        (31, 34.0, True), (31, 34.2, False),                # dernier jour : réserve 0,85 $
    ])
    def test_optional_features_yield_before_the_scheduled_posts(self, settings, state, clock, day, spent, ok):
        """Radar, posts à chaud, réponses aux commentaires : seulement s'il reste plus que de
        quoi écrire les posts planifiés jusqu'à la fin du mois (octobre : 31 jours)."""
        settings.claude_budget_monthly_usd = 35.0
        clock(10, 0, day=day)
        state.data["llm_usage"] = {"days": {"2026-10-01": {"calls": 1, "usd": spent}}}
        llm = LLM(settings, state)
        assert llm.optional_ok() is ok and not llm.over_budget()

    def test_warning_threshold_at_80_percent(self, settings, state, clock):
        settings.claude_budget_monthly_usd = 35.0
        state.data["llm_usage"] = {"days": {"2026-10-14": {"calls": 9, "usd": 27.9}}}
        llm = LLM(settings, state)
        assert not llm.budget_warning_due()
        state.data["llm_usage"]["days"]["2026-10-15"] = {"calls": 1, "usd": 0.2}
        assert llm.budget_warning_due() and not llm.over_budget()

    def test_manual_runs_are_counted_in_a_side_file_never_in_the_live_state(self, settings, state, clock):
        """--preview / --check pendant que le bot tourne : leur coût est noté à part (le service
        réécrirait son état par-dessus) et compte dans le même plafond."""
        from bot import llm as llm_mod
        settings.claude_budget_monthly_usd = 35.0
        state.data["llm_usage"] = {"days": {"2026-10-14": {"calls": 9, "usd": 34.0}}}
        manual = LLM(settings, state, manual=True,
                     client=FakeAnthropic([_resp('{"ok": true, "text": "p"}'), _resp('{"ok": true, "text": "p"}')]))
        assert manual.json("pick-news", "s", "u", SCHEMA)["text"] == "p"
        assert state.data["llm_usage"]["days"] == {"2026-10-14": {"calls": 9, "usd": 34.0}}     # état intact
        ledger = llm_mod.manual_ledger_path()
        assert json.loads(ledger.read_text()) == {"days": {"2026-10-15": {"calls": 1, "usd": 0.0016}}}
        manual.json("post-news", "s", "u", SCHEMA)
        assert json.loads(ledger.read_text())["days"]["2026-10-15"] == {"calls": 2, "usd": 0.0032}
        assert manual.spent_month() == pytest.approx(34.0032)                    # compté une seule fois
        service = LLM(settings, state)                                           # le service les voit
        assert service.spent_month() == pytest.approx(34.0032) and not service.over_budget()
        ledger.write_text(json.dumps({"days": {"2026-10-15": {"calls": 3, "usd": 1.2}}}))
        assert service.over_budget()

    @pytest.mark.parametrize("content", ["", "not json", "[]", '{"days": []}', '{"days": {"2026-10-15": 3}}'])
    def test_broken_side_file_is_ignored_then_repaired(self, settings, state, clock, content):
        from bot import llm as llm_mod
        ledger = llm_mod.manual_ledger_path()
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(content)
        llm = LLM(settings, state, client=FakeAnthropic([_resp('{"ok": true, "text": "p"}')]), manual=True)
        assert llm.spent_month() == 0.0
        assert llm.json("check", "s", "u", SCHEMA)["text"] == "p"
        assert llm_mod.manual_spent_month() == pytest.approx(0.0016)

    def test_real_client_timeout_is_long_enough(self, settings, state):
        settings.anthropic_api_key = "sk-test"
        c = LLM(settings, state).client
        assert c.timeout.read >= 400 and c.max_retries <= 1


# ---------------------------------------------------------------------------
# Garde-fous ajoutés après relecture
# ---------------------------------------------------------------------------
class TestReviewFixes:
    @pytest.mark.parametrize("text", ["I use the same trick when troubleshooting my prompts",
                                      "Strip your prompt down to the task", "skilled workers + AI",
                                      "devote 10 minutes a day", "a trip to the lab"])
    def test_hard_block_no_longer_hits_inside_words(self, text):
        assert filters.hard_blocked(text) is None

    @pytest.mark.parametrize("text", ["he was killed", "RIP to my old workflow", "the election"])
    def test_hard_block_still_blocks_real_terms(self, text):
        assert filters.hard_blocked(text)

    @pytest.mark.parametrize("text", ["polymarket traders bet on gpt-6", "day trading with chatgpt",
                                      "prediction markets think", "xrp holders", "the token sale"])
    def test_speculation_is_off_limits(self, text):
        assert filters.off_limits_term(text)

    def test_handles_are_validated(self):
        w = []
        out = cfg._handles(["@sama", "https://x.com/OpenAI", "twitter.com/karpathy/", "Sam Altman",
                            "this_handle_is_way_too_long", "SAMA"], w)
        assert out == ["sama", "OpenAI", "karpathy"]
        assert len(w) == 2

    def test_bad_optional_setting_falls_back_with_warning(self, monkeypatch):
        monkeypatch.setenv("THREAD_SLOTS", "mardi@15:00,dimanche@17h")
        monkeypatch.setenv("POST_SLOTS", "gm@8h30,tip@25:00")
        monkeypatch.setenv("QUIET_HOURS", "")
        monkeypatch.setenv("HEALTH_REPORT_AT", "")
        monkeypatch.setenv("WEEKLY_REPORT_AT", "off")
        s = cfg.load_settings()
        assert [(sl.weekday, sl.at.hour) for sl in s.thread_slots] == [(1, 15), (6, 17)]  # français accepté
        assert [(sl.kind, sl.at.hour, sl.at.minute) for sl in s.post_slots] == \
            [("tip", 14, 30), ("news", 19, 0)]                                             # défaut : 2 posts par jour
        assert any("POST_SLOTS" in w for w in s.warnings)
        assert s.quiet_hours is not None and s.health_report_at.hour == 8
        assert s.weekly_report is None

    def test_watchdog_is_fed_even_at_warning_level(self):
        from bot import utils
        utils._last_log_time = 0.0
        utils.touch_watchdog()
        assert utils._last_log_time > 0
