"""Visuels (bot/visuals.py) : jamais de capture de X, réglages respectés,
vérification par Claude, délai dur, aucune exception vers l'appelant."""
from __future__ import annotations

import sys
import threading
import time

import pytest

from bot import utils, visuals
from bot.models import Draft, Visual


class StubWriter:
    def __init__(self, ok: bool = True, raises: bool = False) -> None:
        self.ok = ok
        self.raises = raises
        self.calls: list[tuple] = []

    def visual_matches(self, post_text, url, visible_text):
        self.calls.append((post_text, url, visible_text))
        if self.raises:
            raise RuntimeError("claude down")
        return self.ok, "because"


@pytest.fixture
def shots(monkeypatch, tmp_path):
    """Remplace le navigateur : enregistre les URL, écrit un faux PNG."""
    calls: list[str] = []

    def fake_capture(url, tag="post", timeout_s=visuals.CAPTURE_TIMEOUT_S):
        calls.append(url)
        folder = utils.MEDIA_DIR / "shots"
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / f"{tag}.png"
        p.write_bytes(b"\x89PNG fake")
        return p, "OpenAI — Introducing a new model. Long visible text of the page."
    monkeypatch.setattr(visuals, "capture_with_timeout", fake_capture)
    return calls


def _draft(target: str, kind: str = "screenshot", **kw) -> Draft:
    return Draft(kind="news", texts=["the post text", "second"], visual=Visual(kind, target), **kw)


class TestScreenshots:
    @pytest.mark.parametrize("url", [
        "https://x.com/OpenAI/status/1",
        "https://twitter.com/OpenAI/status/1",
        "https://mobile.twitter.com/a/status/1",
        "https://t.co/abc",
        "https://pbs.twimg.com/media/x.jpg",
        "https://openai.com@x.com/status/1",
    ])
    def test_never_captures_x(self, settings, shots, url):
        assert visuals.prepare(settings, _draft(url), StubWriter(), "news") == (None, None)
        assert shots == []

    @pytest.mark.parametrize("url", [
        "https://example.com/article", "https://evil-theverge.com/x", "https://theverge.com.evil.io/x",
        "javascript:alert(1)", "ftp://openai.com/file", "", "not a url",
    ])
    def test_refuses_sites_outside_the_list(self, settings, shots, url):
        assert visuals.prepare(settings, _draft(url), StubWriter(), "news") == (None, None)
        assert shots == []

    def test_disabled_screenshots(self, settings, shots):
        settings.screenshots_enabled = False
        assert visuals.prepare(settings, _draft("https://openai.com/index/x"), StubWriter(), "news") == (None, None)
        assert shots == []

    def test_allowed_page_checked_by_claude(self, settings, shots):
        w = StubWriter(ok=True)
        path, kind = visuals.prepare(settings, _draft("https://www.theverge.com/ai/123"), w, "news_2026-10-15")
        assert kind == "screenshot"
        assert path and path.endswith("news_2026-10-15.png")
        assert shots == ["https://www.theverge.com/ai/123"]
        assert w.calls[0][0] == "the post text"            # 1er tweet seulement
        assert w.calls[0][1] == "https://www.theverge.com/ai/123"
        assert "Introducing" in w.calls[0][2]

    def test_mismatch_deletes_image(self, settings, shots):
        path_before = utils.MEDIA_DIR / "shots" / "news.png"
        assert visuals.prepare(settings, _draft("https://openai.com/index/x"), StubWriter(ok=False),
                               "news") == (None, None)
        assert not path_before.exists()

    def test_check_failure_deletes_image(self, settings, shots):
        assert visuals.prepare(settings, _draft("https://openai.com/index/x"), StubWriter(raises=True),
                               "news") == (None, None)
        assert not (utils.MEDIA_DIR / "shots" / "news.png").exists()

    def test_capture_failure(self, settings, monkeypatch):
        monkeypatch.setattr(visuals, "capture_with_timeout", lambda *a, **k: None)
        assert visuals.prepare(settings, _draft("https://openai.com/index/x"), StubWriter(), "t") == (None, None)

    def test_no_visual_or_poll(self, settings, shots):
        assert visuals.prepare(settings, Draft(kind="gm", texts=["gm"]), StubWriter(), "gm") == (None, None)
        d = _draft("https://openai.com/index/x", poll={"options": ["a", "b"]})
        assert visuals.prepare(settings, d, StubWriter(), "gm") == (None, None)
        assert shots == []

    def test_tag_is_sanitized(self, settings, shots):
        path, _ = visuals.prepare(settings, _draft("https://openai.com/index/x"), StubWriter(), "../../etc/pw d")
        assert path and "/" not in path.rsplit("/", 1)[1].replace(".png", "")
        assert (utils.MEDIA_DIR / "shots") in __import__("pathlib").Path(path).parents

    def test_never_raises(self, settings, shots):
        assert visuals.prepare(settings, _draft("https://openai.com/index/x"), None, "t") == (None, None)
        assert visuals.prepare(settings, object(), StubWriter(), "t") == (None, None)


class TestAIImages:
    def test_disabled_by_default(self, settings, monkeypatch):
        called = []
        monkeypatch.setattr(visuals, "generate_ai_image", lambda *a: called.append(a))
        settings.image_ai_enabled = False
        settings.openai_api_key = "sk-x"
        assert visuals.prepare(settings, _draft("a calm desk", "ai"), StubWriter(), "t") == (None, None)
        assert called == []

    def test_needs_key(self, settings, monkeypatch):
        called = []
        monkeypatch.setattr(visuals, "generate_ai_image", lambda *a: called.append(a))
        settings.image_ai_enabled = True
        settings.openai_api_key = ""
        assert visuals.prepare(settings, _draft("a calm desk", "ai"), StubWriter(), "t") == (None, None)
        assert called == []

    def test_generates(self, settings, monkeypatch, tmp_path):
        out = tmp_path / "img.png"
        out.write_bytes(b"png")
        seen = []
        monkeypatch.setattr(visuals, "generate_ai_image", lambda s, prompt, tag: seen.append((prompt, tag)) or out)
        settings.image_ai_enabled = True
        settings.openai_api_key = "sk-x"
        assert visuals.prepare(settings, _draft("a calm desk at dawn", "ai"), StubWriter(), "tip") == (str(out), "ai")
        assert seen == [("a calm desk at dawn", "tip")]

    def test_refuses_links_and_off_limits(self, settings, monkeypatch):
        called = []
        monkeypatch.setattr(visuals, "generate_ai_image", lambda *a: called.append(a))
        settings.image_ai_enabled = True
        settings.openai_api_key = "sk-x"
        assert visuals.prepare(settings, _draft("logo of openai.com", "ai"), StubWriter(), "t") == (None, None)
        assert visuals.prepare(settings, _draft("a glass of beer on a bar", "ai"), StubWriter(), "t") == (None, None)
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


class TestCaptureMachinery:
    def test_timeout_returns_quickly_and_discards_late_result(self, monkeypatch):
        finished = threading.Event()
        late = utils.MEDIA_DIR / "late.png"

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
        p = tmp_path / "ok.png"
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

    def test_frame_image(self, tmp_path):
        from PIL import Image
        raw = tmp_path / "raw.png"
        Image.new("RGB", (2000, 2000), (255, 255, 255)).save(raw)
        out = tmp_path / "out.png"
        assert visuals.frame_image(raw, out, "openai.com") is True
        img = Image.open(out)
        assert img.size[0] == 1600 + 2 * 28
        assert img.size[1] == int(1600 * 0.62) + 2 * 28 + 44

    def test_cleanup_keeps_latest(self, tmp_path):
        import os
        for i in range(5):
            f = tmp_path / f"{i}.png"
            f.write_bytes(b"x")
            os.utime(f, (1000 + i, 1000 + i))
        visuals._cleanup_old(tmp_path, keep=2)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["3.png", "4.png"]
