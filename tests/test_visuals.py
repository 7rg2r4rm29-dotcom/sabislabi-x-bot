"""Visuels (bot/visuals.py) : une carte dessinée par défaut, la page de l'actu
dans un smartphone quand elle est impeccable, jamais de capture de X, réglages
respectés, délai dur, aucune exception vers l'appelant."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from bot import cards, utils, visuals
from bot.models import Draft, NewsItem, Visual

PAGE = "https://www.theverge.com/ai/123"
CARD = {"type": "prompt", "title": "Dinner from a *fridge photo*",
        "lines": ["Here is a photo of my fridge. What can I cook tonight?"], "note": "Free versions too."}
PHONE_CARD = {"type": "phone", "title": "ChatGPT voice is now *free*",
              "lines": ["No paid plan needed", "Talk instead of typing"], "note": ""}


class StubWriter:
    """Le Writer vu par visuals : l'état (compteurs) et le contrôle visuel de la capture."""

    def __init__(self, state=None, clean: bool = True, raises: bool = False) -> None:
        self.state = state
        self.clean = clean
        self.raises = raises
        self.clean_calls: list[str] = []

    def screenshot_is_clean(self, path):
        self.clean_calls.append(str(path))
        if self.raises:
            raise RuntimeError("claude down")
        return self.clean, "propre" if self.clean else "bandeau cookies au milieu"


class Renders:
    def __init__(self) -> None:
        self.raw: list[dict] = []      # une entrée par carte demandée
        self.fail: list[bool] = []     # .fail.append(True) : le prochain rendu échoue


@pytest.fixture
def rendered(monkeypatch):
    """Remplace le dessin : note chaque carte demandée, écrit un faux PNG."""
    out = Renders()

    def fake_render(card, out_path, *, theme, badge, handle, shot_path=None, source=""):
        out.raw.append({"card": dict(card), "out": Path(out_path), "theme": theme, "badge": badge,
                        "handle": handle, "shot": shot_path, "source": source})
        if out.fail and out.fail.pop(0):
            return False
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"\x89PNG fake")
        return True
    monkeypatch.setattr(cards, "render", fake_render)
    return out


@pytest.fixture
def shots(monkeypatch):
    """Remplace le navigateur : note les URL capturées, écrit un faux JPEG."""
    calls: list[str] = []

    def fake_capture(url, tag="post", timeout_s=visuals.CAPTURE_TIMEOUT_S):
        calls.append(url)
        folder = utils.MEDIA_DIR / "shots"
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / f"{tag}_mobile.jpg"
        p.write_bytes(b"\xff\xd8 fake")
        return p, "The Verge — ChatGPT voice is now free. Long visible text of the page."
    monkeypatch.setattr(visuals, "capture_with_timeout", fake_capture)
    return calls


def item(url: str = PAGE) -> NewsItem:
    return NewsItem(id="n1", title="ChatGPT voice is now free", summary="", url=url, source="The Verge",
                    published=None)


def tip(**kw) -> Draft:
    return Draft(kind="tip", texts=["the post text"], card=dict(CARD), **kw)


def phone(url: str = PAGE, cited: bool = True, **kw) -> Draft:
    return Draft(kind="news", texts=["the post text"], card=dict(PHONE_CARD), visual=Visual("phone", url),
                 sources=[item(url)] if cited else [], **kw)


# ---------------------------------------------------------------------------
# Cartes dessinées
# ---------------------------------------------------------------------------
class TestDrawnCards:
    def test_card_is_rendered(self, settings, state, rendered, shots):
        path, kind = visuals.prepare(settings, tip(), StubWriter(state), "tip_2026-10-15")
        assert kind == "card" and path and Path(path).is_file()
        assert Path(path).parent == utils.MEDIA_DIR / "cards" and Path(path).name.startswith("tip_2026-10-15_")
        call = rendered.raw[0]
        assert call["card"] == CARD and call["handle"] == "sabislabi" and call["shot"] is None
        assert call["badge"] == "Everyday AI · No. 1" and call["theme"] in cards.THEMES
        assert shots == []                                   # aucune page chargée pour une carte dessinée

    def test_palettes_rotate_and_tips_are_numbered(self, settings, state, rendered):
        w = StubWriter(state)
        for _ in range(len(cards.THEME_ORDER) + 1):
            assert visuals.prepare(settings, tip(), w, "tip")[1] == "card"
        themes = [c["theme"] for c in rendered.raw]
        assert set(themes[:len(cards.THEME_ORDER)]) == set(cards.THEME_ORDER)     # toutes y passent
        assert all(a != b for a, b in zip(themes, themes[1:]))                    # jamais deux fois de suite
        assert themes[0] == themes[len(cards.THEME_ORDER)]                        # puis ça recommence
        assert [c["badge"] for c in rendered.raw][:3] == [
            "Everyday AI · No. 1", "Everyday AI · No. 2", "Everyday AI · No. 3"]
        assert state.data["cards"] == {"seq": 6, "tips": 6}

    @pytest.mark.parametrize("draft_kw, badge", [
        ({"kind": "news", "sources": [item()]}, "AI news, made simple"),
        ({"kind": "news"}, "Everyday AI · No. 1"),          # créneau actu, mais post du quotidien
        ({"kind": "tip", "sources": [item()]}, "AI news, made simple"),
        ({"kind": "reactive", "sources": [item()]}, "AI news, made simple"),
        ({"kind": "thread", "angle": "beginner"}, "Everyday AI"),
        ({"kind": "thread", "angle": "tutorial"}, "Step by step"),
        ({"kind": "thread", "angle": "timely"}, "AI news, made simple"),
        ({"kind": "thread", "angle": "weekly"}, "This week in AI"),
    ])
    def test_badge_follows_the_content(self, settings, state, rendered, draft_kw, badge):
        d = Draft(texts=["text"], card=dict(CARD), **draft_kw)
        assert visuals.prepare(settings, d, StubWriter(state), "t")[1] == "card"
        assert rendered.raw[0]["badge"] == badge

    def test_poll_or_nothing_to_show(self, settings, state, rendered, shots):
        w = StubWriter(state)
        assert visuals.prepare(settings, tip(poll={"options": ["a", "b"]}), w, "t") == (None, None)
        assert visuals.prepare(settings, Draft(kind="gm", texts=["gm"]), w, "t") == (None, None)
        assert visuals.prepare(settings, Draft(kind="tip", texts=["x"], card={"type": "poster"}), w, "t") == \
            (None, None)
        assert rendered.raw == [] and shots == []

    def test_render_failure_means_text_only(self, settings, state, rendered):
        rendered.fail.append(True)
        assert visuals.prepare(settings, tip(), StubWriter(state), "t") == (None, None)

    def test_works_without_a_state(self, settings, rendered):
        """Compteurs absents (aperçu, doublure) : une carte quand même."""
        assert visuals.prepare(settings, tip(), StubWriter(None), "t")[1] == "card"
        assert visuals.prepare(settings, tip(), None, "t")[1] == "card"
        assert rendered.raw[0]["badge"] == "Everyday AI"     # pas de numéro sans compteur

    def test_tag_is_sanitized(self, settings, state, rendered):
        path, _ = visuals.prepare(settings, tip(), StubWriter(state), "../../etc/pw d")
        assert Path(path).parent == utils.MEDIA_DIR / "cards"
        assert "/" not in Path(path).name and ".." not in Path(path).name

    def test_never_raises(self, settings, state, rendered):
        assert visuals.prepare(settings, object(), StubWriter(state), "t") == (None, None)
        d = tip()
        d.card = "not a dict"
        assert visuals.prepare(settings, d, StubWriter(state), "t") == (None, None)

    def test_old_cards_are_cleaned_up(self, settings, state, rendered, monkeypatch):
        monkeypatch.setattr(visuals, "CARDS_KEEP", 2)
        folder = utils.MEDIA_DIR / "cards"
        folder.mkdir(parents=True)
        import os
        for i in range(4):
            f = folder / f"old{i}.png"
            f.write_bytes(b"x")
            os.utime(f, (1000 + i, 1000 + i))
        path, _ = visuals.prepare(settings, tip(), StubWriter(state), "new")
        assert sorted(p.name for p in folder.iterdir()) == sorted(["old3.png", Path(path).name])


# ---------------------------------------------------------------------------
# La page de l'actu, vue dans un smartphone
# ---------------------------------------------------------------------------
class TestPhoneCards:
    def test_clean_capture_goes_into_the_phone(self, settings, state, rendered, shots):
        w = StubWriter(state, clean=True)
        path, kind = visuals.prepare(settings, phone(), w, "news_2026-10-15")
        assert kind == "phone" and Path(path).is_file()
        assert shots == [PAGE]
        call = rendered.raw[0]
        assert call["card"]["type"] == "phone" and call["source"] == "theverge.com"
        assert call["badge"] == "AI news, made simple"
        assert w.clean_calls == [str(call["shot"])]          # Claude a regardé CETTE capture
        assert not any((utils.MEDIA_DIR / "shots").iterdir())   # capture brute supprimée après usage

    @pytest.mark.parametrize("writer_kw", [{"clean": False}, {"raises": True}])
    def test_messy_capture_is_replaced_by_a_drawn_card(self, settings, state, rendered, shots, writer_kw):
        w = StubWriter(state, **writer_kw)
        path, kind = visuals.prepare(settings, phone(), w, "news")
        assert kind == "card" and Path(path).is_file()
        assert len(w.clean_calls) == 1 and len(rendered.raw) == 1
        card = rendered.raw[0]["card"]
        assert card["type"] == "list" and card["title"] == PHONE_CARD["title"]
        assert card["lines"] == PHONE_CARD["lines"] and rendered.raw[0]["shot"] is None
        assert not any((utils.MEDIA_DIR / "shots").iterdir())

    def test_no_checker_no_capture_used(self, settings, state, rendered, shots):
        """Sans relecteur (aucun Writer), une capture n'est jamais utilisée à l'aveugle."""
        path, kind = visuals.prepare(settings, phone(), None, "news")
        assert kind == "card" and rendered.raw[0]["card"]["type"] == "list"

    def test_capture_failure_falls_back(self, settings, state, rendered, monkeypatch):
        monkeypatch.setattr(visuals, "capture_with_timeout", lambda *a, **k: None)
        w = StubWriter(state)
        assert visuals.prepare(settings, phone(), w, "t")[1] == "card"
        assert w.clean_calls == [] and rendered.raw[0]["card"]["type"] == "list"

    def test_phone_render_failure_falls_back(self, settings, state, rendered, shots):
        rendered.fail.append(True)                           # le rendu « smartphone » échoue
        path, kind = visuals.prepare(settings, phone(), StubWriter(state), "t")
        assert kind == "card" and [c["card"]["type"] for c in rendered.raw] == ["phone", "list"]
        assert not any((utils.MEDIA_DIR / "shots").iterdir())

    def test_fallback_with_few_lines_is_a_fact_card(self, settings, state, rendered, monkeypatch):
        monkeypatch.setattr(visuals, "capture_with_timeout", lambda *a, **k: None)
        d = phone()
        d.card["lines"] = ["No paid plan needed"]
        assert visuals.prepare(settings, d, StubWriter(state), "t")[1] == "card"
        assert rendered.raw[0]["card"]["type"] == "fact"

    def test_same_palette_for_the_fallback(self, settings, state, rendered, shots):
        visuals.prepare(settings, phone(), StubWriter(state, clean=False), "t")
        assert state.data["cards"]["seq"] == 1               # une seule image comptée

    @pytest.mark.parametrize("url", [
        "https://x.com/OpenAI/status/1", "https://twitter.com/OpenAI/status/1",
        "https://mobile.twitter.com/a/status/1", "https://t.co/abc", "https://pbs.twimg.com/media/x.jpg",
        "https://openai.com@x.com/status/1",
    ])
    def test_never_captures_x(self, settings, state, rendered, shots, url):
        """Même cité, un post X n'est jamais capturé : carte dessinée à la place."""
        assert visuals.prepare(settings, phone(url), StubWriter(state), "news")[1] == "card"
        assert shots == [] and rendered.raw[0]["card"]["type"] == "list"

    @pytest.mark.parametrize("url", [
        "https://example.com/article", "https://evil-theverge.com/x", "https://theverge.com.evil.io/x",
        "javascript:alert(1)", "ftp://openai.com/file", "", "not a url",
    ])
    def test_uncited_page_outside_the_list_is_not_captured(self, settings, state, rendered, shots, url):
        assert visuals.prepare(settings, phone(url, cited=False), StubWriter(state), "news")[1] == "card"
        assert shots == []

    def test_cited_page_on_any_site_is_captured(self, settings, state, rendered, shots):
        url = "https://someblog.dev/agents-break"
        assert visuals.prepare(settings, phone(url), StubWriter(state), "news")[1] == "phone"
        assert shots == [url] and rendered.raw[0]["source"] == "someblog.dev"

    def test_disabled_screenshots(self, settings, state, rendered, shots):
        settings.screenshots_enabled = False
        assert visuals.prepare(settings, phone(), StubWriter(state), "news")[1] == "card"
        assert shots == []

    def test_phone_card_without_target(self, settings, state, rendered, shots):
        d = Draft(kind="news", texts=["x"], card=dict(PHONE_CARD))
        assert visuals.prepare(settings, d, StubWriter(state), "t")[1] == "card"
        assert shots == []


# ---------------------------------------------------------------------------
# Photo IA (désactivée par défaut, gardée pour qui la rallume)
# ---------------------------------------------------------------------------
def ai_draft(prompt: str) -> Draft:
    return Draft(kind="news", texts=["the post text"], visual=Visual("ai", prompt))


class TestAIImages:
    def test_disabled_by_default(self, settings, monkeypatch):
        called = []
        monkeypatch.setattr(visuals, "generate_ai_image", lambda *a: called.append(a))
        settings.image_ai_enabled = False
        settings.openai_api_key = "sk-x"
        assert visuals.prepare(settings, ai_draft("a calm desk"), StubWriter(), "t") == (None, None)
        assert called == []

    def test_needs_key(self, settings, monkeypatch):
        called = []
        monkeypatch.setattr(visuals, "generate_ai_image", lambda *a: called.append(a))
        settings.image_ai_enabled = True
        settings.openai_api_key = ""
        assert visuals.prepare(settings, ai_draft("a calm desk"), StubWriter(), "t") == (None, None)
        assert called == []

    def test_generates(self, settings, monkeypatch, tmp_path):
        out = tmp_path / "img.png"
        out.write_bytes(b"png")
        seen = []
        monkeypatch.setattr(visuals, "generate_ai_image", lambda s, prompt, tag: seen.append((prompt, tag)) or out)
        settings.image_ai_enabled = True
        settings.openai_api_key = "sk-x"
        assert visuals.prepare(settings, ai_draft("a calm desk at dawn"), StubWriter(), "tip") == (str(out), "ai")
        assert seen == [("a calm desk at dawn", "tip")]

    def test_refuses_links_and_off_limits(self, settings, monkeypatch):
        called = []
        monkeypatch.setattr(visuals, "generate_ai_image", lambda *a: called.append(a))
        settings.image_ai_enabled = True
        settings.openai_api_key = "sk-x"
        assert visuals.prepare(settings, ai_draft("logo of openai.com"), StubWriter(), "t") == (None, None)
        assert visuals.prepare(settings, ai_draft("a glass of beer on a bar"), StubWriter(), "t") == (None, None)
        assert called == []

    def test_generate_without_openai_package(self, settings, monkeypatch):
        monkeypatch.setitem(sys.modules, "openai", None)
        settings.openai_api_key = "sk-x"
        assert visuals.generate_ai_image(settings, "a desk", "t") is None

    def test_generate_writes_png(self, settings, monkeypatch):
        import base64
        import types

        class FakeImages:
            def __init__(self):
                self.kwargs = None

            def generate(self, **kw):
                self.kwargs = kw
                return types.SimpleNamespace(data=[types.SimpleNamespace(b64_json=base64.b64encode(b"PNGDATA"))])

        images = FakeImages()
        created = {}

        class FakeOpenAI:
            def __init__(self, **kw):
                created.update(kw)
                self.images = images

        monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))
        settings.openai_api_key = "sk-x"
        path = visuals.generate_ai_image(settings, "a desk", "tip")
        assert path is not None and path.read_bytes() == b"PNGDATA"
        assert path.parent == utils.MEDIA_DIR / "generated"
        assert created["timeout"] == 120.0 and created["max_retries"] == 1
        assert images.kwargs["prompt"].startswith("a desk") and "No text" in images.kwargs["prompt"]
        assert images.kwargs["quality"] == settings.image_quality


# ---------------------------------------------------------------------------
# Mécanique de capture (sans navigateur)
# ---------------------------------------------------------------------------
class TestCaptureMachinery:
    def test_timeout_returns_quickly_and_discards_late_result(self, monkeypatch):
        finished = threading.Event()
        late = utils.MEDIA_DIR / "late.jpg"

        def slow(url, tag, cancel):
            time.sleep(0.5)
            utils.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
            late.write_bytes(b"x")
            finished.set()
            return late, "text"
        monkeypatch.setattr(visuals, "_capture_in_browser", slow)
        t0 = time.monotonic()
        assert visuals.capture_with_timeout("https://openai.com/x", "t", timeout_s=0.1) is None
        assert time.monotonic() - t0 < 0.45
        assert finished.wait(3)
        time.sleep(0.05)
        assert not late.exists()     # le résultat arrivé trop tard est supprimé

    def test_result_returned_in_time(self, monkeypatch, tmp_path):
        p = tmp_path / "ok.jpg"
        monkeypatch.setattr(visuals, "_capture_in_browser", lambda url, tag, cancel: (p, "visible"))
        assert visuals.capture_with_timeout("https://openai.com/x", "t", timeout_s=2) == (p, "visible")

    def test_worker_exception_is_contained(self, monkeypatch):
        def boom(url, tag, cancel):
            raise RuntimeError("driver crashed")
        monkeypatch.setattr(visuals, "_capture_in_browser", boom)
        assert visuals.capture_with_timeout("https://openai.com/x", "t", timeout_s=2) is None

    def test_without_playwright(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "playwright", None)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
        assert visuals._capture_in_browser("https://openai.com/x", "t", threading.Event()) is None

    def test_page_is_loaded_like_a_phone(self, monkeypatch):
        monkeypatch.delenv("XBOT_BROWSER_PATH", raising=False)
        monkeypatch.delenv("XBOT_BROWSER_PROXY", raising=False)
        launch, context = visuals._browser_launch_options()
        assert context["viewport"] == {"width": 390, "height": visuals.MOBILE_VIEWPORT_HEIGHT}
        assert context["is_mobile"] and context["has_touch"] and "iPhone" in context["user_agent"]
        assert context["device_scale_factor"] >= 3            # net une fois posé dans la maquette
        assert launch["headless"] is True and "executable_path" not in launch and "proxy" not in launch
        assert "ignore_https_errors" not in context
        assert visuals.MOBILE_VIEWPORT_HEIGHT >= cards.PHONE_PAGE_VISIBLE_HEIGHT

    def test_x_requests_blocked_inside_pages(self):
        rx = visuals._X_REQUEST_RE
        for u in ("https://platform.twitter.com/widgets.js", "https://x.com/i/status/1",
                  "https://pbs.twimg.com/a.jpg", "https://t.co/x", "https://syndication.twitter.com:443/x"):
            assert rx.search(u), u
        for u in ("https://openai.com/x", "https://www.theverge.com/x?ref=x.com", "https://next.co/x"):
            assert not rx.search(u), u

    def test_page_looks_broken(self):
        long_body = "A real article about a new AI model. " * 10
        assert visuals.page_looks_broken("404 Not Found", long_body)
        assert visuals.page_looks_broken("Some title", "short")
        assert visuals.page_looks_broken("News", "Just a moment... checking your browser " + long_body)
        assert visuals.page_looks_broken("Introducing GPT-6", long_body) is None
        # « error » dans le texte (pas le titre) n'est pas une page cassée
        assert visuals.page_looks_broken("Debugging", long_body + " error handling 404 codes") is None

    def test_visible_text(self):
        assert visuals.visible_text("Title", "a\n\n b   c", 100) == "Title\na b c"

    def test_headline_must_be_whole_and_on_screen(self):
        ok = {"top": 40, "bottom": 190, "left": 16, "right": 374, "vw": 390, "pageWidth": 390}
        assert visuals.headline_fits(ok) is None
        assert visuals.headline_fits(dict(ok, top=-30)) == "titre coupé en haut"
        assert visuals.headline_fits(dict(ok, bottom=visuals.HEADLINE_MUST_END_BEFORE + 1)) == \
            "titre trop long pour l'écran"
        assert visuals.headline_fits(dict(ok, right=420)) == "titre plus large que l'écran"
        assert visuals.headline_fits(dict(ok, left=-12)) == "titre plus large que l'écran"
        for garbage in (None, {}, {"top": "x"}, dict(ok, bottom=10), dict(ok, vw=0)):
            assert visuals.headline_fits(garbage) == "titre non mesurable"
        assert visuals.HEADLINE_MUST_END_BEFORE < cards.PHONE_PAGE_VISIBLE_HEIGHT

    def test_site_of(self):
        assert visuals.site_of("https://www.theverge.com/ai/1") == "theverge.com"
        assert visuals.site_of("https://feeds.arstechnica.com/x") == "arstechnica.com"
        assert visuals.site_of("https://www.bbc.co.uk/news/1") == "bbc.co.uk"
        assert visuals.site_of("https://blog.google/products/gemini/") == "blog.google"
        assert visuals.site_of("not a url") == ""

    def test_final_page_must_be_the_one_we_asked_for(self):
        ok = visuals.final_url_ok
        assert ok("https://www.theverge.com/ai/1?utm=x", "https://www.theverge.com/ai/1")
        assert ok("https://someblog.dev/post/", "https://www.someblog.dev/post")          # même site
        assert ok("https://techcrunch.com/2026/a", "https://feeds.feedburner.com/tc/a")   # flux → site de confiance
        assert not ok("https://login.example.org/wall", "https://someblog.dev/post")      # autre site
        assert not ok("https://x.com/i/flow/login", "https://www.theverge.com/ai/1")
        assert not ok("https://t.co/abc", "https://t.co/abc")                             # jamais X
        assert not ok("", "https://www.theverge.com/ai/1")
        assert not ok("about:blank", "https://www.theverge.com/ai/1")

    def test_allowed_sites(self):
        assert visuals.is_allowed_url("https://www.theverge.com/a")
        assert visuals.is_allowed_url("https://www.zdnet.com/article/x")
        assert not visuals.is_allowed_url("https://evil-openai.com/a")
        assert not visuals.is_allowed_url("https://x.com/a")
        assert not set(visuals.ALLOWED_DOMAINS) & set(visuals.X_DOMAINS)
        assert len(set(visuals.ALLOWED_DOMAINS)) == len(visuals.ALLOWED_DOMAINS)

    def test_cleanup_keeps_latest(self, tmp_path):
        import os
        for i in range(5):
            f = tmp_path / f"{i}.png"
            f.write_bytes(b"x")
            os.utime(f, (1000 + i, 1000 + i))
        visuals._cleanup_old(tmp_path, keep=2)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["3.png", "4.png"]


# ---------------------------------------------------------------------------
# Claude regarde la capture
# ---------------------------------------------------------------------------
class TestScreenshotCheckByClaude:
    def test_writer_sends_the_image_to_claude(self, settings, state, tmp_path):
        from PIL import Image
        from bot.writer import Writer
        from tests.fakes import FakeLLM
        img = tmp_path / "shot.jpg"
        Image.new("RGB", (1560, 1720), (255, 255, 255)).save(img)
        llm = FakeLLM({"screenshot-check": {"ok": False, "issue": "titre coupé"}})
        ok, why = Writer(settings, llm, state).screenshot_is_clean(img)
        assert (ok, why) == (False, "titre coupé")
        assert llm.calls[0]["images"] == [img]

    def test_llm_builds_an_image_block(self):
        from PIL import Image
        from bot.llm import _image_block
        import tempfile
        p = Path(tempfile.mkdtemp()) / "x.png"
        Image.new("RGB", (3200, 1800), (10, 20, 30)).save(p)
        b = _image_block(p)
        assert b["type"] == "image" and b["source"]["media_type"] == "image/jpeg" and b["source"]["data"]
        assert _image_block(Path("/nonexistent.png")) is None
