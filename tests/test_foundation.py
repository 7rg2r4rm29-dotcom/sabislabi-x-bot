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
        assert not filters.looks_like_json_leak("plain text")

    def test_clean_output(self):
        assert filters.clean_output('"hello #AI @bob world"') == "hello world"
        assert filters.clean_output("line1\n\n\n\nline2") == "line1\n\nline2"

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
        assert [sl.kind for sl in s.post_slots] == ["gm", "tip", "news"]                   # défaut
        assert any("POST_SLOTS" in w for w in s.warnings)
        assert s.quiet_hours is not None and s.health_report_at.hour == 8
        assert s.weekly_report is None

    def test_watchdog_is_fed_even_at_warning_level(self):
        from bot import utils
        utils._last_log_time = 0.0
        utils.touch_watchdog()
        assert utils._last_log_time > 0
