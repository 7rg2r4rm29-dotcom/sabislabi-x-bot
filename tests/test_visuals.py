"""Visuels (bot/visuals.py) : la capture d'écran, façon téléphone, de la page d'une
actu CITÉE. Jamais de capture de X ; la page doit être le bon article ; Claude
regarde la capture avant qu'elle serve (sinon : page suivante, puis texte seul) ;
délai dur ; aucune exception vers l'appelant.

Aucun vrai navigateur ici : un faux Playwright « joue » la page. Les tests optionnels
en bas du fichier lancent un vrai Chromium sur des pages LOCALES (jamais Internet) :
XBOT_REAL_BROWSER=1 [XBOT_BROWSER_PATH=…/chrome] python -m pytest tests/test_visuals.py"""
from __future__ import annotations

import base64
import logging
import os
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import utils, visuals
from bot.models import Draft, NewsItem, Visual

S = visuals.SHOT_SCALE

PAGE = "https://www.theverge.com/2026/10/15/openai-chatgpt-voice-mode-free"
TC = "https://techcrunch.com/2026/10/15/chatgpt-voice-mode-free-for-everyone/"
ARS = "https://arstechnica.com/ai/2026/10/openai-voice-mode-is-now-free/"
BLOG = "https://someblog.dev/2026/voice-mode-free-for-all"          # citée, hors de la liste de confiance
XPOST = "https://x.com/OpenAI/status/1846000000000000000"
TITLES = {
    PAGE: "OpenAI makes ChatGPT voice mode free for everyone",
    TC: "ChatGPT voice mode is now free for everyone",
    ARS: "OpenAI's voice mode is now free on every ChatGPT plan",
    BLOG: "Voice mode for free: what changes for ChatGPT users",
}
LONG_BODY = "A real article about a new AI model, written for everyone. " * 10


def item(url: str = PAGE, title: str | None = None) -> NewsItem:
    return NewsItem(id=url[-16:], title=title or TITLES.get(url, "A story"), summary="", url=url,
                    source="Site", published=None)


def shot_draft(target: str = PAGE, cited=(PAGE,), kind: str = "news", texts=None, **kw) -> Draft:
    return Draft(kind=kind, texts=list(texts or ["the post text"]), visual=Visual("shot", target),
                 sources=[item(u) for u in cited], **kw)


def page_text(url: str) -> str:
    """Texte visible d'une page d'article (titre de la page + début du texte)."""
    title = next((t for u, t in TITLES.items() if u.rstrip("/") == url.rstrip("/")), "")
    return f"{title} | Site\nMenu Tech AI {title}. By A. Writer. The update reaches every user this week."


class StubWriter:
    """Le Writer vu par visuals : seulement le contrôle visuel de la capture par Claude."""

    def __init__(self, *verdicts, raises: bool = False) -> None:
        self.verdicts = list(verdicts) or [(True, "capture propre")]
        self.raises = raises
        self.calls: list[dict] = []

    def screenshot_is_clean(self, image_path, headline: str = "", post: str = ""):
        self.calls.append({"path": str(image_path), "headline": headline, "post": post,
                           "exists": Path(image_path).is_file()})
        if self.raises:
            raise RuntimeError("claude down")
        return self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]


class FakeShots:
    """Remplace capture_with_timeout : un petit fichier par capture (dans media/shots) et le
    texte visible de la page ; texts[url] = None : la capture de cette page rate."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.files: list[Path] = []
        self.texts: dict[str, str | None] = {}

    def __call__(self, url, tag="post", timeout_s=visuals.CAPTURE_TIMEOUT_S):
        self.calls.append((url, tag))
        text = self.texts[url] if url in self.texts else page_text(url)
        if text is None:
            return None
        folder = utils.MEDIA_DIR / "shots"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{tag}_{len(self.calls)}.jpg"
        path.write_bytes(b"\xff\xd8 fake jpeg")
        self.files.append(path)
        return path, text

    def urls(self) -> list[str]:
        return [u for u, _ in self.calls]


@pytest.fixture
def shots(monkeypatch):
    fake = FakeShots()
    monkeypatch.setattr(visuals, "capture_with_timeout", fake)
    return fake


def capture_file(path: Path, height: int, content: int, width: int = 60,
                 bg=(255, 255, 255), ink=(40, 40, 40)) -> Path:
    """JPEG « capture » : du contenu (bloc uni `ink`) sur les `content` premières lignes, le fond dessous."""
    from PIL import Image
    im = Image.new("RGB", (width, height), bg)
    if content:
        im.paste(ink, (0, 0, width, content))
    im.save(path, "JPEG", quality=visuals.JPEG_QUALITY)
    return path


def size_of(path) -> tuple[int, int]:
    from PIL import Image
    with Image.open(path) as im:
        return im.size


# ---------------------------------------------------------------------------
# Faux Playwright : la page répond ce qu'on lui dit, et on note ce que visuals lui fait
# ---------------------------------------------------------------------------
GOOD_MEASURE = {"top": 37, "bottom": 140.5, "left": 16, "right": 374, "vw": 390, "pageWidth": 390, "lead": False}
LEAD_MEASURE = dict(GOOD_MEASURE, top=256, bottom=340, lead=True)
ARTICLE_BODY = ("Menu Tech AI Science\nVOICE\nOpenAI makes ChatGPT voice mode free for everyone\nBy A. Writer\n"
                + "The update reaches every ChatGPT user this week, on phones and on the web. " * 8)


def _next(seq: list):
    """Réponses successives d'une étape ; la dernière se répète."""
    return seq.pop(0) if len(seq) > 1 else seq[0]


def _write_capture(path: Path, width: int, height: int, content_rows: int | None) -> None:
    """Fausse capture : colonnes de « texte » sur les `content_rows` premières lignes, blanc dessous."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (width, height), (255, 255, 255))
    rows = height if content_rows is None else content_rows
    if rows > 0:
        draw = ImageDraw.Draw(im)
        for x in range(0, width, 16):
            draw.rectangle([x, 0, x + 7, rows - 1], fill=(30, 30, 30))
    im.save(path, "JPEG", quality=visuals.JPEG_QUALITY)


class FakeWorld:
    def __init__(self) -> None:
        self.status: int | None = 200          # None : goto ne renvoie pas de réponse
        self.redirect_to: str | None = None    # adresse finale après chargement
        self.url_after_click: str | None = None
        self.title = "OpenAI makes ChatGPT voice mode free for everyone | The Verge"
        self.bodies = [ARTICLE_BODY]           # innerText successifs
        self.prepared = True                   # _PREPARE_JS a trouvé le titre
        self.aligns = [dict(GOOD_MEASURE)]     # mesures successives de _ALIGN_JS
        self.frame: dict | None = {"cut": 460, "floor": 300}
        self.visible: set[str] = set()         # sélecteurs de bandeaux visibles
        self.fail: dict[str, BaseException] = {}   # étape -> exception
        self.hooks: dict[str, callable] = {}       # étape -> fonction (annulation, attente…)
        self.content_rows: int | None = None   # lignes de contenu dans l'image (None : toute l'image)
        self.screenshot_bytes: bytes | None = None
        self.current_url = ""
        # journal
        self.steps: list[str] = []
        self.args: dict[str, list] = {}
        self.launch_opts: dict | None = None
        self.context_opts: dict | None = None
        self.routes: list[tuple] = []
        self.clicked: list[str] = []
        self.shots: list[dict] = []
        self.contents: list[str] = []
        self.closed = 0

    def step(self, name: str, arg=None) -> None:
        self.steps.append(name)
        self.args.setdefault(name, []).append(arg)
        hook = self.hooks.get(name)
        if hook:
            hook()
        if name in self.fail:
            raise self.fail[name]

    def count(self, name: str) -> int:
        return self.steps.count(name)

    def install(self, monkeypatch) -> "FakeWorld":
        world = self

        class _Session:
            def __init__(self) -> None:
                self.chromium = SimpleNamespace(launch=world._launch)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        mod = types.ModuleType("playwright.sync_api")
        mod.sync_playwright = _Session
        pkg = types.ModuleType("playwright")
        pkg.__path__ = []
        pkg.sync_api = mod
        monkeypatch.setitem(sys.modules, "playwright", pkg)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)
        return self

    def _launch(self, **opts):
        self.launch_opts = opts
        self.step("launch")
        return _FakeBrowser(self)


class _FakeBrowser:
    def __init__(self, world: FakeWorld) -> None:
        self.w = world

    def new_context(self, **opts):
        self.w.context_opts = opts
        self.w.step("new_context")
        return _FakeContext(self.w)

    def close(self) -> None:
        self.w.closed += 1


class _FakeContext:
    def __init__(self, world: FakeWorld) -> None:
        self.w = world

    def route(self, pattern, handler) -> None:
        self.w.routes.append((pattern, handler))

    def new_page(self):
        return _FakePage(self.w)


class _FakeLocator:
    def __init__(self, world: FakeWorld, selector: str) -> None:
        self.w, self.selector = world, selector

    @property
    def first(self):
        return self

    def is_visible(self) -> bool:
        if "locator" in self.w.fail:
            raise self.w.fail["locator"]
        return self.selector in self.w.visible

    def click(self, timeout=None) -> None:
        self.w.clicked.append(self.selector)
        self.w.visible.discard(self.selector)
        if self.w.url_after_click:
            self.w.current_url = self.w.url_after_click


class _FakePage:
    _SCRIPTS = {visuals._PREPARE_JS: "prepare", visuals._SETTLE_JS: "settle",
                visuals._ALIGN_JS: "align", visuals._FRAME_JS: "frame"}

    def __init__(self, world: FakeWorld) -> None:
        self.w = world

    @property
    def url(self) -> str:
        return self.w.current_url

    def set_default_timeout(self, ms) -> None:
        self.w.default_timeout = ms

    def goto(self, url, wait_until=None, timeout=None):
        self.w.step("goto", url)
        self.w.current_url = self.w.redirect_to or url
        return None if self.w.status is None else SimpleNamespace(status=self.w.status)

    def wait_for_load_state(self, state="load", timeout=None) -> None:
        self.w.step(state)

    def title(self) -> str:
        self.w.step("title")
        return self.w.title

    def evaluate(self, script, arg=None):
        name = self._SCRIPTS.get(script)
        if name is None:
            assert "innerText" in script, f"script inattendu : {script[:60]}"
            name = "body"
        self.w.step(name, arg)
        if name == "prepare":
            return self.w.prepared
        if name == "settle":
            return 0
        if name == "align":
            return _next(self.w.aligns)
        if name == "frame":
            return self.w.frame
        return _next(self.w.bodies)

    def locator(self, selector):
        return _FakeLocator(self.w, selector)

    def screenshot(self, path, type="png", quality=None, clip=None) -> None:
        self.w.step("screenshot", clip)
        self.w.shots.append({"path": path, "type": type, "quality": quality, "clip": dict(clip or {})})
        if self.w.screenshot_bytes is not None:
            Path(path).write_bytes(self.w.screenshot_bytes)
            return
        _write_capture(Path(path), int(clip["width"] * S), int(clip["height"] * S), self.w.content_rows)

    def set_content(self, html, wait_until=None, timeout=None) -> None:
        self.w.contents.append(html)
        self.w.step("set_content")


@pytest.fixture
def waits(monkeypatch):
    """Les pauses de la capture (attente du chargement) deviennent instantanées ; on les note."""
    noted: list[float] = []
    monkeypatch.setattr(visuals, "time", SimpleNamespace(sleep=noted.append, strftime=time.strftime,
                                                         time=time.time))
    return noted


@pytest.fixture
def fake_browser(monkeypatch, waits):
    return FakeWorld().install(monkeypatch)


def shot_files() -> list[Path]:
    folder = utils.MEDIA_DIR / "shots"
    return sorted(folder.iterdir()) if folder.is_dir() else []


# ---------------------------------------------------------------------------
# Le titre doit être entier et bien placé
# ---------------------------------------------------------------------------
class TestHeadlineFits:
    def test_well_placed_headline(self):
        assert visuals.headline_fits(GOOD_MEASURE) is None
        assert visuals.headline_fits(LEAD_MEASURE) is None

    @pytest.mark.parametrize("lead, limit", [(False, visuals.HEADLINE_LATEST_END),
                                             (True, visuals.HEADLINE_MUST_END_BEFORE)])
    def test_headline_must_end_in_time(self, lead, limit):
        """Image d'ouverture au-dessus : le titre finit avant HEADLINE_MUST_END_BEFORE ; sans
        elle, un long titre peut descendre jusqu'à HEADLINE_LATEST_END. 2 px de tolérance."""
        m = dict(GOOD_MEASURE, lead=lead)
        assert visuals.headline_fits(dict(m, bottom=limit + 2)) is None
        assert visuals.headline_fits(dict(m, bottom=limit + 2.5)) == "titre trop long pour l'écran"

    def test_long_headline_fits_only_without_a_lead_image(self):
        m = dict(GOOD_MEASURE, bottom=visuals.HEADLINE_MUST_END_BEFORE + 40)
        assert visuals.headline_fits(m) is None
        assert visuals.headline_fits(dict(m, lead=True)) == "titre trop long pour l'écran"

    def test_limits_are_consistent(self):
        assert visuals.SHOT_MIN_HEIGHT < visuals.HEADLINE_MUST_END_BEFORE < visuals.HEADLINE_LATEST_END
        assert visuals.HEADLINE_LATEST_END < visuals.SHOT_MAX_HEIGHT <= visuals.SHOT_VIEWPORT_HEIGHT

    def test_cut_at_the_top(self):
        assert visuals.headline_fits(dict(GOOD_MEASURE, top=-2)) is None
        assert visuals.headline_fits(dict(GOOD_MEASURE, top=-2.5)) == "titre coupé en haut"
        assert visuals.headline_fits(dict(GOOD_MEASURE, top=-30, bottom=600)) == "titre coupé en haut"

    def test_wider_than_the_screen(self):
        assert visuals.headline_fits(dict(GOOD_MEASURE, left=-2, right=392)) is None
        assert visuals.headline_fits(dict(GOOD_MEASURE, left=-12)) == "titre plus large que l'écran"
        assert visuals.headline_fits(dict(GOOD_MEASURE, right=420)) == "titre plus large que l'écran"

    @pytest.mark.parametrize("garbage", [
        None, {}, [], "x", {"top": "x"}, dict(GOOD_MEASURE, bottom=10), dict(GOOD_MEASURE, bottom=37),
        dict(GOOD_MEASURE, vw=0), {k: v for k, v in GOOD_MEASURE.items() if k != "vw"},
    ])
    def test_not_measurable(self, garbage):
        assert visuals.headline_fits(garbage) == "titre non mesurable"


# ---------------------------------------------------------------------------
# Hauteur de la capture, bande vide en bas
# ---------------------------------------------------------------------------
class TestFrameHeight:
    @pytest.mark.parametrize("frame, height", [
        ({"cut": 460, "floor": 300}, 460),
        ({"cut": visuals.SHOT_MIN_HEIGHT}, visuals.SHOT_MIN_HEIGHT),
        ({"cut": visuals.SHOT_MAX_HEIGHT}, visuals.SHOT_MAX_HEIGHT),
        ({"cut": 486.6}, 487),
        ({"cut": "400"}, 400),
        ({"cut": visuals.SHOT_MIN_HEIGHT - 1}, None),
        ({"cut": visuals.SHOT_MAX_HEIGHT + 1}, None),
        ({"cut": None, "floor": 470}, None),        # aucun endroit propre où couper
        ({"cut": "abc"}, None),
        ({"cut": float("nan")}, None),
        ({}, None),
        (None, None),
    ])
    def test_frame_height(self, frame, height):
        assert visuals.frame_height(frame) == height

    def test_portrait_4_5_at_most_like_a_phone_screenshot(self):
        width = visuals.SHOT_WIDTH * S
        assert width == 1170
        assert visuals.SHOT_MAX_HEIGHT * S * 4 <= width * 5          # X affiche le 4:5 en entier
        assert visuals.SHOT_MIN_HEIGHT * S < width


class TestTrimFlatBottom:
    def test_big_empty_band_is_removed(self, tmp_path):
        p = capture_file(tmp_path / "a.jpg", 1456, 1200)
        assert visuals.trim_flat_bottom(p) is True
        assert size_of(p) == (60, 1200 + visuals.FLAT_BAND_KEEP * S)    # un peu d'air gardé sous le contenu
        from PIL import Image
        with Image.open(p) as im:
            assert im.format == "JPEG"

    def test_band_just_over_the_limit(self, tmp_path):
        content = 1456 - visuals.FLAT_BAND_MAX * S - 8
        p = capture_file(tmp_path / "a.jpg", 1456, content)
        assert visuals.trim_flat_bottom(p) is True
        assert size_of(p) == (60, content + visuals.FLAT_BAND_KEEP * S)

    def test_short_band_is_kept(self, tmp_path):
        p = capture_file(tmp_path / "a.jpg", 1456, 1456 - visuals.FLAT_BAND_MAX * S)
        before = p.read_bytes()
        assert visuals.trim_flat_bottom(p) is False
        assert p.read_bytes() == before

    def test_never_below_the_minimum_height(self, tmp_path):
        p = capture_file(tmp_path / "a.jpg", 1461, 320)
        assert visuals.trim_flat_bottom(p) is True
        assert size_of(p) == (60, visuals.SHOT_MIN_HEIGHT * S)

    def test_minimum_height_capture_is_left_alone(self, tmp_path):
        p = capture_file(tmp_path / "a.jpg", visuals.SHOT_MIN_HEIGHT * S, 0)    # entièrement unie
        assert visuals.trim_flat_bottom(p) is False
        assert size_of(p) == (60, visuals.SHOT_MIN_HEIGHT * S)

    def test_dark_band_is_removed_too(self, tmp_path):
        p = capture_file(tmp_path / "a.jpg", 1456, 1008, bg=(12, 12, 12), ink=(235, 235, 235))
        assert visuals.trim_flat_bottom(p) is True
        assert size_of(p) == (60, 1008 + visuals.FLAT_BAND_KEEP * S)

    @pytest.mark.parametrize("content", [b"not an image", b"", None])
    def test_unreadable_file_is_not_an_error(self, tmp_path, content):
        p = tmp_path / "a.jpg"
        if content is not None:
            p.write_bytes(content)
        assert visuals.trim_flat_bottom(p) is False


# ---------------------------------------------------------------------------
# Navigateur « iPhone »
# ---------------------------------------------------------------------------
class TestBrowserOptions:
    def test_page_is_loaded_like_an_iphone(self, monkeypatch):
        monkeypatch.delenv("XBOT_BROWSER_PATH", raising=False)
        monkeypatch.delenv("XBOT_BROWSER_PROXY", raising=False)
        launch, context = visuals._browser_launch_options()
        assert launch == visuals.launch_options()
        assert launch["headless"] is True and "executable_path" not in launch and "proxy" not in launch
        assert context["viewport"] == {"width": 390, "height": visuals.SHOT_VIEWPORT_HEIGHT}
        assert context["device_scale_factor"] == 3 and context["is_mobile"] and context["has_touch"]
        assert "iPhone" in context["user_agent"] and context["locale"] == "en-US"
        assert context["accept_downloads"] is False and "ignore_https_errors" not in context

    def test_preinstalled_browser(self, monkeypatch):
        monkeypatch.setenv("XBOT_BROWSER_PATH", "/opt/chrome/chrome")
        monkeypatch.delenv("XBOT_BROWSER_PROXY", raising=False)
        launch, context = visuals._browser_launch_options()
        assert launch["executable_path"] == "/opt/chrome/chrome" and "proxy" not in launch
        assert "ignore_https_errors" not in context

    def test_test_proxy(self, monkeypatch):
        monkeypatch.delenv("XBOT_BROWSER_PATH", raising=False)
        monkeypatch.setenv("XBOT_BROWSER_PROXY", "http://127.0.0.1:3128")
        launch, context = visuals._browser_launch_options()
        assert launch["proxy"] == {"server": "http://127.0.0.1:3128"}
        assert context["ignore_https_errors"] is True             # proxy de test seulement

    def test_empty_variables_are_ignored(self, monkeypatch):
        monkeypatch.setenv("XBOT_BROWSER_PATH", "")
        monkeypatch.setenv("XBOT_BROWSER_PROXY", "")
        launch, context = visuals._browser_launch_options()
        assert "executable_path" not in launch and "proxy" not in launch
        assert "ignore_https_errors" not in context


# ---------------------------------------------------------------------------
# Adresses : jamais X, le bon site, le bon article
# ---------------------------------------------------------------------------
class TestUrls:
    @pytest.mark.parametrize("url, host", [
        ("https://www.TheVerge.com/ai/1", "www.theverge.com"),
        ("HTTPS://WWW.THEVERGE.COM/x", "www.theverge.com"),
        ("https://user:pw@techcrunch.com:443/a", "techcrunch.com"),
        ("https://openai.com@x.com/status/1", "x.com"),
        ("https://x.com./OpenAI", "x.com"),
        ("  https://openai.com/index  ", "openai.com"),
        ("ftp://openai.com/file", ""), ("javascript:alert(1)", ""), ("not a url", ""),
        ("https://", ""), ("", ""), (None, ""),
    ])
    def test_host_of(self, url, host):
        assert visuals.host_of(url) == host

    @pytest.mark.parametrize("url", [
        XPOST, "https://twitter.com/a/status/1", "https://mobile.twitter.com/a/status/1", "https://t.co/abc",
        "https://pbs.twimg.com/media/x.jpg", "https://openai.com@x.com/status/1", "http://X.COM/a",
    ])
    def test_x_is_recognized(self, url):
        assert visuals.is_x_url(url) and not visuals.is_allowed_url(url)

    @pytest.mark.parametrize("url", ["https://www.theverge.com/a", "https://next.co/x", "https://notx.com/a",
                                     "https://x.com.evil.io/a", "https://openai.com/index/x", ""])
    def test_not_x(self, url):
        assert not visuals.is_x_url(url)

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
        "BUG: host_of lit l'hôte avec urllib, Chromium autrement : pour ces adresses le navigateur va "
        "sur x.com (\\ avant @, lettres pleine chasse, point encodé) mais is_x_url dit non (et "
        "is_allowed_url dit oui pour la première). Seul le blocage des requêtes X dans la page rattrape."))
    @pytest.mark.parametrize("url", ["https://x.com\\@theverge.com/OpenAI/status/1",
                                     "https://ｘ.com/OpenAI/status/1",
                                     "https://x%2Ecom/OpenAI/status/1"])
    def test_urls_a_browser_opens_on_x(self, url):
        assert visuals.is_x_url(url) and not visuals.is_allowed_url(url)

    def test_allowed_sites(self):
        assert visuals.is_allowed_url("https://www.theverge.com/a")
        assert visuals.is_allowed_url("https://www.zdnet.com/article/x")
        assert visuals.is_allowed_url("https://openai.com/index/voice")
        for bad in ("https://evil-openai.com/a", "https://theverge.com.evil.io/x", "https://x.com/a",
                    "ftp://openai.com/file", "https://example.com/a", ""):
            assert not visuals.is_allowed_url(bad), bad
        assert not set(visuals.ALLOWED_DOMAINS) & set(visuals.X_DOMAINS)
        assert len(set(visuals.ALLOWED_DOMAINS)) == len(visuals.ALLOWED_DOMAINS)

    @pytest.mark.parametrize("url, site", [
        ("https://www.theverge.com/ai/1", "theverge.com"),
        ("https://feeds.arstechnica.com/x", "arstechnica.com"),
        ("https://www.bbc.co.uk/news/1", "bbc.co.uk"),
        ("https://news.bbc.co.uk/x", "bbc.co.uk"),
        ("https://www.abc.com.au/news", "abc.com.au"),
        ("https://blog.google/products/gemini/", "blog.google"),
        ("not a url", ""),
    ])
    def test_site_of(self, url, site):
        assert visuals.site_of(url) == site

    def test_final_page_must_be_on_the_site_we_asked_for(self):
        ok = visuals.final_url_ok
        assert ok("https://www.theverge.com/ai/1?utm=x", "https://www.theverge.com/ai/1")
        assert ok("https://someblog.dev/post/", "https://www.someblog.dev/post")          # même site
        assert ok("https://techcrunch.com/2026/a", "https://feeds.feedburner.com/tc/a")   # flux → site de confiance
        assert not ok("https://login.example.org/wall", "https://someblog.dev/post")      # autre site
        assert not ok("https://x.com/i/flow/login", "https://www.theverge.com/ai/1")
        assert not ok("https://t.co/abc", "https://t.co/abc")                             # jamais X
        assert not ok("", "https://www.theverge.com/ai/1")
        assert not ok("about:blank", "https://www.theverge.com/ai/1")

    @pytest.mark.parametrize("final, requested", [
        (PAGE, PAGE),
        (PAGE + "?utm_source=rss", PAGE),
        (PAGE + "/", PAGE),
        ("https://www.bbc.co.uk/news/technology-68012345", "https://bbc.in/3xYzAb"),   # adresse canonique
        (TC, "https://feeds.feedburner.com/techcrunch/abc"),                         # lien de flux
        ("https://www.theverge.com/anything", "https://www.theverge.com/"),          # accueil demandé : rien à juger
    ])
    def test_same_article(self, final, requested):
        assert visuals.same_article(final, requested) is True

    @pytest.mark.parametrize("final, requested", [
        ("https://www.theverge.com/", PAGE),                       # renvoyé vers l'accueil
        ("https://www.theverge.com", PAGE),
        ("https://www.theverge.com/2026/10", PAGE),                # page de rubrique (chemin raccourci)
        ("https://www.wired.com/tag/ai/", "https://www.wired.com/tag/ai/story/openai-voice-mode-free"),
        ("https://site.com/login", "https://site.com/news/123456-voice-mode"),   # mur de connexion
        ("https://site.com/subscribe", "https://site.com/news/123456-voice-mode"),
    ])
    def test_not_the_article_anymore(self, final, requested):
        assert visuals.same_article(final, requested) is False

    def test_x_requests_blocked_inside_pages(self):
        rx = visuals._X_REQUEST_RE
        for u in ("https://platform.twitter.com/widgets.js", "https://x.com/i/status/1",
                  "https://pbs.twimg.com/a.jpg", "https://t.co/x", "https://syndication.twitter.com:443/x"):
            assert rx.search(u), u
        for u in ("https://openai.com/x", "https://www.theverge.com/x?ref=x.com", "https://next.co/x"):
            assert not rx.search(u), u


# ---------------------------------------------------------------------------
# La page elle-même : cassée ? la bonne histoire ?
# ---------------------------------------------------------------------------
class TestPageChecks:
    def test_page_looks_broken(self):
        assert visuals.page_looks_broken("404 Not Found", LONG_BODY).startswith("titre d'erreur")
        assert visuals.page_looks_broken("Some title", "short") == "page quasi vide (5 caractères)"
        assert visuals.page_looks_broken("News", "Just a moment... checking your browser " + LONG_BODY)
        assert visuals.page_looks_broken("News", "Please log in to continue. " + LONG_BODY).startswith(
            "signature d'erreur en haut de page")
        assert visuals.page_looks_broken("Access denied", LONG_BODY)
        assert visuals.page_looks_broken("Introducing GPT-6", LONG_BODY) is None

    def test_short_error_words_only_count_in_the_title(self):
        assert visuals.page_looks_broken("Debugging", LONG_BODY + " error handling 404 codes") is None
        assert visuals.page_looks_broken("Debugging", "error " + LONG_BODY) is None

    def test_error_phrase_far_down_the_page_is_ignored(self):
        body = LONG_BODY * 2 + " The page not found error used to haunt us."
        assert len(LONG_BODY * 2) > 600
        assert visuals.page_looks_broken("A new way to browse", body) is None

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
        "BUG: page_looks_broken cherche les mots courts (« error », « oops »…) comme sous-chaînes du "
        "titre : « terrorism », « hoops » font prendre un vrai article pour une page d'erreur."))
    @pytest.mark.parametrize("title", ["Counter-terrorism agencies turn to AI to read the signals",
                                       "NBA hoops: the AI scout that sees every play"])
    def test_error_words_inside_other_words(self, title):
        assert visuals.page_looks_broken(title, LONG_BODY) is None

    def test_visible_text(self):
        assert visuals.visible_text("Title", "a\n\n b   c", 100) == "Title\na b c"
        assert visuals.visible_text("", "body text") == "body text"
        assert visuals.visible_text("Title", "") == "Title"
        assert visuals.visible_text(None, None) == ""
        text = visuals.visible_text("T", "x" * 5000)
        assert text == "T\n" + "x" * visuals.VISIBLE_TEXT_CHARS

    def test_title_on_page(self):
        title = TITLES[PAGE]
        assert visuals.title_on_page(title, page_text(PAGE))
        assert visuals.title_on_page(title, "OPENAI: ChatGPT's VOICE mode, now FREE!")       # casse, ponctuation
        assert not visuals.title_on_page(title, "Live: the latest on the election and the markets.")

    def test_title_on_page_needs_half_of_the_words(self):
        title = "Alpha bravo charlie delta echoes foxtrot"            # 6 mots qui comptent
        assert visuals.title_on_page(title, "alpha bravo charlie and more")
        assert not visuals.title_on_page(title, "alpha bravo and more")

    def test_title_on_page_concludes_nothing_without_material(self):
        assert visuals.title_on_page("GPT-5 is here", "nothing in common")           # moins de 3 mots qui comptent
        assert visuals.title_on_page(TITLES[PAGE], "")
        assert visuals.title_on_page("", "anything")

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
        "BUG: title_on_page compte les mots-outils (your, this, will, have, next…) comme des mots « qui "
        "comptent » : une page sans rapport passe le garde-fou dès que le titre en contient beaucoup."))
    def test_title_on_page_is_not_fooled_by_common_words(self):
        title = "Why your next phone will have this new Qualcomm chip inside"
        unrelated = "Most read: what your kids will have for lunch next week, and this summer's best beaches."
        assert not visuals.title_on_page(title, unrelated)          # phone, qualcomm, chip, inside : aucun


class TestFiles:
    @pytest.mark.parametrize("tag, safe", [
        ("tip_2026-10-15", "tip_2026-10-15"), ("../../etc/pw d", "etc-pw-d"), ("", "post"), (None, "post"),
        ("///", "post"), ("a" * 50, "a" * 40), ("news 18:00 é", "news-18-00"),
    ])
    def test_safe_tag(self, tag, safe):
        assert visuals._safe_tag(tag) == safe

    def test_cleanup_keeps_the_latest_files(self, tmp_path):
        for i in range(5):
            f = tmp_path / f"{i}.jpg"
            f.write_bytes(b"x")
            os.utime(f, (1000 + i, 1000 + i))
        (tmp_path / "subdir").mkdir()
        visuals._cleanup_old(tmp_path, keep=2)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["3.jpg", "4.jpg", "subdir"]

    def test_cleanup_of_a_missing_folder(self, tmp_path):
        visuals._cleanup_old(tmp_path / "nope", keep=2)                  # aucune exception


# ---------------------------------------------------------------------------
# Quelles pages essayer
# ---------------------------------------------------------------------------
class TestShotCandidates:
    def test_designated_page_first_then_the_other_cited_pages(self):
        assert visuals.shot_candidates(shot_draft(TC, cited=(PAGE, TC, ARS))) == [TC, PAGE, ARS]

    def test_at_most_max_pages_tried(self):
        out = visuals.shot_candidates(shot_draft(PAGE, cited=(PAGE, TC, ARS, BLOG)))
        assert out == [PAGE, TC, ARS, BLOG][:visuals.MAX_PAGES_TRIED]
        assert len(out) == visuals.MAX_PAGES_TRIED

    def test_never_x(self):
        d = shot_draft(XPOST, cited=(XPOST, "https://t.co/abc", "https://openai.com@x.com/status/1", PAGE))
        assert visuals.shot_candidates(d) == [PAGE]

    def test_never_twice_the_same_page(self):
        d = shot_draft(PAGE + "/", cited=(PAGE, PAGE.upper(), TC))
        assert visuals.shot_candidates(d) == [PAGE + "/", TC]

    def test_cited_page_on_any_site_but_uncited_page_only_on_a_trusted_site(self):
        assert visuals.shot_candidates(shot_draft("https://example.com/some-article", cited=(BLOG,))) == [BLOG]
        trusted = "https://openai.com/index/chatgpt-voice-for-everyone/"
        assert visuals.shot_candidates(shot_draft(trusted, cited=(BLOG,))) == [trusted, BLOG]

    @pytest.mark.parametrize("bad", ["javascript:alert(1)", "ftp://openai.com/file", "", "not a url",
                                     "https://evil-theverge.com/x", "https://theverge.com.evil.io/x"])
    def test_unusable_designated_page_is_skipped(self, bad):
        assert visuals.shot_candidates(shot_draft(bad, cited=(PAGE,))) == [PAGE]

    def test_without_a_designated_page(self):
        assert visuals.shot_candidates(Draft(kind="news", texts=["x"], sources=[item(TC), item(PAGE)])) == [TC, PAGE]
        assert visuals.shot_candidates(Draft(kind="gm", texts=["gm"])) == []
        assert visuals.shot_candidates(object()) == []


# ---------------------------------------------------------------------------
# La capture, regardée par Claude avant de servir
# ---------------------------------------------------------------------------
class TestShotVisual:
    def test_clean_capture_is_used(self, settings, shots):
        w = StubWriter()
        path, kind = visuals.prepare(settings, shot_draft(), w, "news_2026-10-15")
        assert kind == "shot" and isinstance(path, str) and Path(path).is_file()
        assert Path(path).parent == utils.MEDIA_DIR / "shots"
        assert shots.calls == [(PAGE, "news_2026-10-15")]
        # Claude a regardé CETTE capture, en sachant de quelle actu et de quel post il s'agit
        assert w.calls == [{"path": path, "headline": TITLES[PAGE], "post": "the post text", "exists": True}]

    def test_screenshots_disabled(self, settings, shots):
        settings.screenshots_enabled = False
        w = StubWriter()
        assert visuals.prepare(settings, shot_draft(), w, "news") == (None, None)
        assert shots.calls == [] and w.calls == []

    def test_page_of_another_story_is_skipped(self, settings, shots):
        shots.texts[PAGE] = "Live: the latest on the election and the markets. " * 5
        w = StubWriter()
        path, kind = visuals.prepare(settings, shot_draft(PAGE, cited=(PAGE, TC)), w, "news")
        assert kind == "shot" and shots.urls() == [PAGE, TC]
        assert not shots.files[0].exists() and Path(path) == shots.files[1]
        assert [c["headline"] for c in w.calls] == [TITLES[TC]]         # jamais montrée à Claude

    def test_refused_capture_is_deleted_and_the_next_page_tried(self, settings, shots):
        w = StubWriter((False, "bandeau cookies au milieu"), (True, "propre"))
        path, kind = visuals.prepare(settings, shot_draft(PAGE, cited=(PAGE, TC)), w, "news")
        assert kind == "shot" and shots.calls == [(PAGE, "news"), (TC, "news-2")]
        assert not shots.files[0].exists() and Path(path) == shots.files[1] and shots.files[1].is_file()
        assert [c["headline"] for c in w.calls] == [TITLES[PAGE], TITLES[TC]]

    def test_text_only_when_no_capture_is_clean(self, settings, shots):
        w = StubWriter((False, "pub en plein milieu"))
        d = shot_draft(PAGE, cited=(PAGE, TC, ARS, BLOG))
        assert visuals.prepare(settings, d, w, "news") == (None, None)
        assert len(shots.calls) == len(w.calls) == visuals.MAX_PAGES_TRIED
        assert shots.files and not any(f.exists() for f in shots.files)

    def test_failed_capture_moves_on_to_the_next_page(self, settings, shots):
        shots.texts[PAGE] = None
        w = StubWriter()
        path, kind = visuals.prepare(settings, shot_draft(PAGE, cited=(PAGE, TC)), w, "news")
        assert kind == "shot" and shots.urls() == [PAGE, TC] and len(w.calls) == 1

    @pytest.mark.parametrize("writer", [None, object()])
    def test_never_used_without_a_checker(self, settings, shots, writer):
        assert visuals.prepare(settings, shot_draft(), writer, "news") == (None, None)
        assert shots.files and not any(f.exists() for f in shots.files)

    def test_checker_failure_counts_as_a_no(self, settings, shots):
        w = StubWriter(raises=True)
        assert visuals.prepare(settings, shot_draft(PAGE, cited=(PAGE, TC)), w, "news") == (None, None)
        assert len(w.calls) == 2 and not any(f.exists() for f in shots.files)

    def test_unexpected_answer_from_the_checker_counts_as_a_no(self, settings, shots):
        assert visuals.prepare(settings, shot_draft(), StubWriter("yes"), "news") == (None, None)
        assert not any(f.exists() for f in shots.files)

    def test_the_check_knows_the_story_and_the_first_tweet(self, settings, shots):
        w = StubWriter()
        d = shot_draft(TC.rstrip("/"), cited=(PAGE, TC), kind="thread",
                       texts=["First tweet of the thread", "Second tweet"])
        path, kind = visuals.prepare(settings, d, w, "thread")
        assert kind == "shot" and shots.urls() == [TC.rstrip("/")]
        assert w.calls == [{"path": path, "headline": TITLES[TC], "post": "First tweet of the thread",
                            "exists": True}]

    def test_designated_page_that_is_not_cited(self, settings, shots):
        url = "https://openai.com/index/chatgpt-voice-for-everyone/"
        w = StubWriter()
        path, kind = visuals.prepare(settings, shot_draft(url, cited=()), w, "news")
        assert kind == "shot" and shots.urls() == [url] and w.calls[0]["headline"] == ""

    def test_nothing_to_capture(self, settings, shots):
        w = StubWriter()
        assert visuals.prepare(settings, shot_draft(XPOST, cited=(XPOST,)), w, "news") == (None, None)
        assert shots.calls == [] and w.calls == []


# ---------------------------------------------------------------------------
# Délai dur : la boucle principale ne reste jamais figée
# ---------------------------------------------------------------------------
class TestCaptureWithTimeout:
    def test_result_returned_in_time(self, monkeypatch, tmp_path):
        p = tmp_path / "ok.jpg"
        seen = []

        def fake(url, tag, cancel):
            seen.append((url, tag, cancel.is_set()))
            return p, "visible"
        monkeypatch.setattr(visuals, "_capture_in_browser", fake)
        assert visuals.capture_with_timeout(PAGE, "news", timeout_s=5) == (p, "visible")
        assert seen == [(PAGE, "news", False)]

    def test_timeout_returns_quickly_and_the_late_capture_is_deleted(self, monkeypatch):
        release, done = threading.Event(), threading.Event()
        late = utils.MEDIA_DIR / "shots" / "late.jpg"
        cancelled = []

        def slow(url, tag, cancel):
            release.wait(5)
            late.parent.mkdir(parents=True, exist_ok=True)
            late.write_bytes(b"x")
            cancelled.append(cancel.is_set())
            done.set()
            return late, "text"
        monkeypatch.setattr(visuals, "_capture_in_browser", slow)
        t0 = time.monotonic()
        assert visuals.capture_with_timeout(PAGE, "t", timeout_s=0.1) is None
        assert time.monotonic() - t0 < 2
        release.set()
        assert done.wait(5)
        deadline = time.monotonic() + 5
        while late.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not late.exists()              # arrivée trop tard : supprimée
        assert cancelled == [True]            # le travail en retard sait qu'il est annulé

    @pytest.mark.parametrize("exc", [RuntimeError("driver crashed"), ValueError(), OSError("disk full")])
    def test_errors_never_reach_the_caller(self, monkeypatch, exc):
        def boom(url, tag, cancel):
            raise exc
        monkeypatch.setattr(visuals, "_capture_in_browser", boom)
        assert visuals.capture_with_timeout(PAGE, "t", timeout_s=5) is None

    def test_worker_is_a_daemon_with_a_hard_deadline(self, monkeypatch):
        made, events = [], []

        class HungThread:
            def __init__(self, target=None, daemon=None, name=None):
                self.daemon, self.joined = daemon, None
                made.append(self)

            def start(self):
                pass

            def join(self, timeout=None):
                self.joined = timeout

            def is_alive(self):
                return True

        class Event(threading.Event):
            def __init__(self):
                super().__init__()
                events.append(self)
        monkeypatch.setattr(visuals, "threading", SimpleNamespace(Thread=HungThread, Event=Event))
        assert visuals.capture_with_timeout(PAGE, "t", timeout_s=7) is None
        assert made[0].daemon is True and made[0].joined == 7
        assert events[0].is_set()

    def test_hung_page_is_abandoned_then_its_browser_closed(self, fake_browser):
        release = threading.Event()
        fake_browser.hooks["networkidle"] = lambda: release.wait(5)
        t0 = time.monotonic()
        assert visuals.capture_with_timeout(PAGE, "news", timeout_s=0.2) is None
        assert time.monotonic() - t0 < 2
        release.set()                          # la page « répond » enfin…
        deadline = time.monotonic() + 5
        while not fake_browser.closed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fake_browser.closed == 1        # … et le thread ferme son navigateur sans photographier
        assert fake_browser.shots == [] and "prepare" not in fake_browser.steps
        assert shot_files() == []


# ---------------------------------------------------------------------------
# Dans le navigateur (faux Playwright)
# ---------------------------------------------------------------------------
class TestCaptureInBrowser:
    def capture(self, url: str = PAGE, tag: str = "news", cancel: threading.Event | None = None):
        return visuals._capture_in_browser(url, tag, cancel or threading.Event())

    def test_a_good_page_is_photographed_like_a_phone(self, fake_browser):
        res = self.capture()
        assert res is not None
        path, text = res
        assert path.parent == utils.MEDIA_DIR / "shots" and path.name.startswith("news_") and path.suffix == ".jpg"
        from PIL import Image
        with Image.open(path) as im:
            assert im.format == "JPEG" and im.size == (visuals.SHOT_WIDTH * S, 460 * S)
        assert text.startswith(fake_browser.title + "\n") and "voice mode free" in text
        shot = fake_browser.shots[0]
        assert shot["type"] == "jpeg" and shot["quality"] == visuals.JPEG_QUALITY
        assert shot["clip"] == {"x": 0, "y": 0, "width": visuals.SHOT_WIDTH, "height": 460}
        assert fake_browser.launch_opts == visuals.launch_options()
        assert fake_browser.context_opts == visuals._browser_launch_options()[1]
        assert fake_browser.closed == 1
        steps = fake_browser.steps
        assert steps.index("goto") < steps.index("prepare") < steps.index("align") < steps.index("frame") \
            < steps.index("screenshot")
        assert fake_browser.args["goto"] == [PAGE]
        assert fake_browser.args["align"][0] == [visuals.SHOT_TOP_MARGIN, visuals.SHOT_MAX_HEIGHT,
                                                 visuals.HEADLINE_MUST_END_BEFORE]
        assert fake_browser.args["frame"] == [[visuals.SHOT_MIN_HEIGHT, visuals.SHOT_MAX_HEIGHT]]

    def test_requests_to_x_are_blocked_inside_the_page(self, fake_browser):
        self.capture()
        (pattern, handler), = fake_browser.routes
        assert pattern is visuals._X_REQUEST_RE
        route = SimpleNamespace(aborted=0)
        route.abort = lambda: setattr(route, "aborted", route.aborted + 1)
        handler(route)
        assert route.aborted == 1

    def test_flat_band_at_the_bottom_is_trimmed(self, fake_browser):
        fake_browser.content_rows = 600
        path, _ = self.capture()
        assert size_of(path) == (visuals.SHOT_WIDTH * S, visuals.SHOT_MIN_HEIGHT * S)

    def test_old_captures_are_cleaned_up(self, fake_browser, monkeypatch):
        monkeypatch.setattr(visuals, "SHOTS_KEEP", 2)
        folder = utils.MEDIA_DIR / "shots"
        folder.mkdir(parents=True)
        for i in range(3):
            f = folder / f"old{i}.jpg"
            f.write_bytes(b"x")
            os.utime(f, (1000 + i, 1000 + i))
        path, _ = self.capture()
        assert sorted(p.name for p in folder.iterdir()) == sorted(["old2.jpg", path.name])

    def test_no_http_response_object_is_fine(self, fake_browser):
        fake_browser.status = None
        assert self.capture() is not None

    def test_feed_link_redirecting_to_a_trusted_site(self, fake_browser):
        fake_browser.redirect_to = TC
        assert self.capture("https://feeds.feedburner.com/techcrunch/abc123") is not None

    def test_consent_banners_are_clicked_away(self, fake_browser):
        fake_browser.visible = {'button:has-text("Accept all")', "#onetrust-accept-btn-handler"}
        assert self.capture() is not None
        assert fake_browser.clicked == ['button:has-text("Accept all")', "#onetrust-accept-btn-handler"]
        assert fake_browser.steps.index("prepare") > fake_browser.steps.index("goto")

    def test_flaky_banner_lookups_are_ignored(self, fake_browser):
        fake_browser.fail["locator"] = RuntimeError("element is not attached to the DOM")
        assert self.capture() is not None and fake_browser.clicked == []

    @pytest.mark.parametrize("step", ["networkidle", "settle"])
    def test_slow_network_and_settling_errors_are_tolerated(self, fake_browser, step):
        fake_browser.fail[step] = TimeoutError("Timeout 12000ms exceeded")
        assert self.capture() is not None

    def test_waits_for_a_page_that_is_still_empty(self, fake_browser, waits):
        fake_browser.bodies = ["", "Loading…", ARTICLE_BODY]
        assert self.capture() is not None
        assert fake_browser.count("body") == 4                 # 3 lectures avant les bandeaux, 1 après
        assert waits.count(2.0) >= 3

    def test_unreadable_title_is_tolerated(self, fake_browser):
        fake_browser.fail["title"] = RuntimeError("Execution context was destroyed")
        path, text = self.capture()
        assert text == visuals.visible_text("", ARTICLE_BODY)

    def test_unreadable_text_counts_as_an_empty_page(self, fake_browser):
        fake_browser.fail["body"] = RuntimeError("Execution context was destroyed")
        assert self.capture() is None and fake_browser.shots == []

    def test_page_that_stays_empty_is_refused(self, fake_browser):
        fake_browser.bodies = [""]
        assert self.capture() is None
        assert fake_browser.count("body") == 5                 # 1 lecture + 4 nouvelles tentatives
        assert "prepare" not in fake_browser.steps and fake_browser.closed == 1

    def test_reframes_when_the_lead_image_shows_up(self, fake_browser):
        """L'image d'ouverture arrive pendant l'attente : le cadrage change, on recadre."""
        fake_browser.aligns = [dict(GOOD_MEASURE), dict(LEAD_MEASURE), dict(LEAD_MEASURE)]
        assert self.capture() is not None
        assert fake_browser.count("align") == 4

    def test_at_most_four_framing_passes(self, fake_browser):
        fake_browser.aligns = [dict(GOOD_MEASURE) if i % 2 == 0 else dict(LEAD_MEASURE) for i in range(9)]
        assert self.capture() is not None
        assert fake_browser.count("align") == 8

    def test_framing_with_a_lead_image_uses_the_stricter_limit(self, fake_browser):
        fake_browser.aligns = [dict(LEAD_MEASURE, bottom=visuals.HEADLINE_MUST_END_BEFORE + 10)]
        assert self.capture() is None and fake_browser.shots == []
        fake_browser.aligns = [dict(GOOD_MEASURE, bottom=visuals.HEADLINE_MUST_END_BEFORE + 10)]
        fake_browser.closed = 0
        assert self.capture() is not None

    # cas -> (mise en place, étape qui ne doit jamais être atteinte : le refus tombe au bon moment)
    REFUSALS = {
        "http_error": (lambda w: setattr(w, "status", 404), "networkidle"),
        "server_error": (lambda w: setattr(w, "status", 503), "networkidle"),
        "other_site": (lambda w: setattr(w, "redirect_to", "https://login.example.org/wall"), "networkidle"),
        "x_login": (lambda w: setattr(w, "redirect_to", "https://x.com/i/flow/login"), "networkidle"),
        "homepage": (lambda w: setattr(w, "redirect_to", "https://www.theverge.com/"), "networkidle"),
        "section_page": (lambda w: setattr(w, "redirect_to", "https://www.theverge.com/2026/10"), "networkidle"),
        "goto_fails": (lambda w: w.fail.update(goto=RuntimeError("net::ERR_NAME_NOT_RESOLVED")), "networkidle"),
        "context_fails": (lambda w: w.fail.update(new_context=RuntimeError("browser crashed")), "goto"),
        "error_page": (lambda w: setattr(w, "title", "404 Not Found | The Verge"), "prepare"),
        "wall_after_banners": (lambda w: setattr(w, "bodies", [ARTICLE_BODY, "Subscribe to continue reading. " * 5]),
                               "prepare"),
        "moved_after_banners": (lambda w: (w.visible.add('button:has-text("Accept all")'),
                                           setattr(w, "url_after_click", "https://www.theverge.com/")), "prepare"),
        "no_headline": (lambda w: setattr(w, "prepared", False), "settle"),
        "headline_lost": (lambda w: setattr(w, "aligns", [None]), "frame"),
        "headline_cut": (lambda w: setattr(w, "aligns", [dict(GOOD_MEASURE, top=-30)]), "frame"),
        "headline_too_long": (lambda w: setattr(w, "aligns", [dict(GOOD_MEASURE, bottom=500)]), "frame"),
        "no_clean_cut": (lambda w: setattr(w, "frame", {"cut": None, "floor": 470}), "screenshot"),
        "frame_lost": (lambda w: setattr(w, "frame", None), "screenshot"),
        "screenshot_fails": (lambda w: w.fail.update(screenshot=RuntimeError("Target closed")), None),
    }

    @pytest.mark.parametrize("case", sorted(REFUSALS))
    def test_refused_pages_give_no_capture(self, fake_browser, case):
        setup, never = self.REFUSALS[case]
        setup(fake_browser)
        assert self.capture() is None
        assert shot_files() == [] and fake_browser.shots == []
        assert never is None or never not in fake_browser.steps
        assert fake_browser.closed == 1                         # le navigateur est toujours refermé

    def test_browser_that_does_not_start(self, fake_browser):
        fake_browser.fail["launch"] = RuntimeError("Executable doesn't exist")
        assert self.capture() is None and fake_browser.closed == 0 and shot_files() == []

    def test_cancelled_before_loading(self, fake_browser):
        cancel = threading.Event()
        cancel.set()
        assert self.capture(cancel=cancel) is None
        assert "goto" not in fake_browser.steps and fake_browser.closed == 1

    def test_cancelled_while_framing(self, fake_browser):
        cancel = threading.Event()
        fake_browser.hooks["frame"] = cancel.set
        assert self.capture(cancel=cancel) is None
        assert fake_browser.shots == [] and fake_browser.closed == 1 and shot_files() == []

    def test_without_playwright(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "playwright", None)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
        assert self.capture() is None


# ---------------------------------------------------------------------------
# --check : le navigateur des captures fonctionne-t-il ?
# ---------------------------------------------------------------------------
class TestSelfTest:
    def test_success(self, fake_browser):
        assert visuals.self_test() == (True, "")
        assert "goto" not in fake_browser.steps                     # page locale : aucun réseau
        assert "<h1>Screenshot check</h1>" in fake_browser.contents[0]
        assert fake_browser.context_opts["is_mobile"] is True
        assert fake_browser.shots[0]["clip"]["height"] == visuals.SHOT_MIN_HEIGHT
        assert fake_browser.closed == 1

    def test_failure_message_is_the_first_line_of_the_error(self, fake_browser):
        fake_browser.fail["launch"] = RuntimeError("Executable doesn't exist at /ms/chrome\n╔══ install ══╗")
        assert visuals.self_test() == (False, "Executable doesn't exist at /ms/chrome")

    def test_long_messages_are_cut(self, fake_browser):
        fake_browser.fail["launch"] = RuntimeError("x" * 300)
        assert visuals.self_test() == (False, "x" * 160)

    def test_error_without_message(self, fake_browser):
        fake_browser.fail["set_content"] = TimeoutError()
        assert visuals.self_test() == (False, "TimeoutError")
        assert fake_browser.closed == 1

    def test_empty_screenshot(self, fake_browser):
        fake_browser.screenshot_bytes = b"\xff\xd8 tiny"
        assert visuals.self_test() == (False, "le navigateur n'a pas pu faire la capture d'essai")

    def test_without_playwright(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "playwright", None)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
        assert visuals.self_test() == (False, "Playwright n'est pas installé")

    def test_browser_that_hangs(self, fake_browser, monkeypatch):
        joined = []

        class HungThread:
            def __init__(self, target=None, daemon=None, name=None):
                assert daemon is True

            def start(self):
                pass

            def join(self, timeout=None):
                joined.append(timeout)

            def is_alive(self):
                return True
        monkeypatch.setattr(visuals, "threading", SimpleNamespace(Thread=HungThread, Event=threading.Event))
        assert visuals.self_test() == (False, "le navigateur ne répond pas (plus de 60 s)")
        assert joined == [60]


# ---------------------------------------------------------------------------
# Point d'entrée : prepare()
# ---------------------------------------------------------------------------
def ai_draft(prompt: str) -> Draft:
    return Draft(kind="news", texts=["the post text"], visual=Visual("ai", prompt))


class TestPrepare:
    def test_poll_has_no_image(self, settings, shots):
        d = shot_draft(poll={"options": ["yes", "no"]})
        assert visuals.prepare(settings, d, StubWriter(), "t") == (None, None)
        assert shots.calls == []

    @pytest.mark.parametrize("draft", [
        Draft(kind="gm", texts=["gm"]),
        Draft(kind="news", texts=["x"], visual=Visual("phone", PAGE), sources=[item()]),   # ancien type
        Draft(kind="news", texts=["x"], visual=Visual("card", ""), sources=[item()]),
        Draft(kind="news", texts=["x"], visual=Visual("ai", "")),
    ])
    def test_nothing_to_show(self, settings, shots, draft):
        assert visuals.prepare(settings, draft, StubWriter(), "t") == (None, None)
        assert shots.calls == []

    def test_kind_is_read_loosely(self, settings, shots):
        d = shot_draft()
        d.visual = Visual(" Shot ", PAGE)
        assert visuals.prepare(settings, d, StubWriter(), "t")[1] == "shot"

    def test_shot_without_a_target_uses_the_cited_pages(self, settings, shots):
        assert visuals.prepare(settings, shot_draft("", cited=(TC,)), StubWriter(), "t")[1] == "shot"
        assert shots.urls() == [TC]

    def test_tag_is_sanitized(self, settings, shots):
        visuals.prepare(settings, shot_draft(), StubWriter(), "../../etc/pw d")
        assert shots.calls[0][1] == "etc-pw-d"

    def test_never_raises(self, settings, shots, monkeypatch):
        class Exploding:
            @property
            def poll(self):
                raise RuntimeError("boom")
        assert visuals.prepare(settings, Exploding(), StubWriter(), "t") == (None, None)
        assert visuals.prepare(settings, object(), StubWriter(), "t") == (None, None)
        d = shot_draft()
        d.visual = "not a visual"
        assert visuals.prepare(settings, d, StubWriter(), "t") == (None, None)

        def boom(*a, **k):
            raise RuntimeError("unexpected")
        monkeypatch.setattr(visuals, "_shot_visual", boom)
        assert visuals.prepare(settings, shot_draft(), StubWriter(), "t") == (None, None)


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
        assert visuals.prepare(settings, ai_draft("a calm desk at dawn"), StubWriter(), "tip/1") == (str(out), "ai")
        assert seen == [("a calm desk at dawn", "tip-1")]

    def test_generation_failure_means_text_only(self, settings, monkeypatch):
        monkeypatch.setattr(visuals, "generate_ai_image", lambda *a: None)
        settings.image_ai_enabled = True
        settings.openai_api_key = "sk-x"
        assert visuals.prepare(settings, ai_draft("a calm desk"), StubWriter(), "t") == (None, None)

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

    def test_generate_without_key_or_prompt(self, settings):
        settings.openai_api_key = ""
        assert visuals.generate_ai_image(settings, "a desk", "t") is None
        settings.openai_api_key = "sk-x"
        assert visuals.generate_ai_image(settings, "   ", "t") is None

    @staticmethod
    def fake_openai(monkeypatch, generate):
        created = {}

        class FakeOpenAI:
            def __init__(self, **kw):
                created.update(kw)
                self.images = SimpleNamespace(generate=generate)

        monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))
        return created

    def test_generate_writes_png(self, settings, monkeypatch):
        kwargs = {}

        def generate(**kw):
            kwargs.update(kw)
            return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(b"PNGDATA"))])
        created = self.fake_openai(monkeypatch, generate)
        settings.openai_api_key = "sk-x"
        path = visuals.generate_ai_image(settings, "a desk", "tip")
        assert path is not None and path.read_bytes() == b"PNGDATA"
        assert path.parent == utils.MEDIA_DIR / "generated"
        assert created["timeout"] == 120.0 and created["max_retries"] == 1
        assert kwargs["prompt"].startswith("a desk") and "No text" in kwargs["prompt"]
        assert kwargs["quality"] == settings.image_quality

    def test_generate_downloads_an_image_given_by_url(self, settings, monkeypatch):
        import requests
        got = []
        monkeypatch.setattr(requests, "get", lambda url, timeout=None: got.append((url, timeout)) or
                            SimpleNamespace(content=b"PNGBYTES", raise_for_status=lambda: None))
        self.fake_openai(monkeypatch, lambda **kw: SimpleNamespace(
            data=[SimpleNamespace(b64_json=None, url="https://img.example/x.png")]))
        settings.openai_api_key = "sk-x"
        path = visuals.generate_ai_image(settings, "a desk", "tip")
        assert path is not None and path.read_bytes() == b"PNGBYTES"
        assert got == [("https://img.example/x.png", 60)]

    def test_api_error_never_logs_the_key(self, settings, monkeypatch, caplog):
        class APIError(Exception):
            status_code = 401

        def generate(**kw):
            raise APIError("Incorrect API key provided: sk-secret-123")
        self.fake_openai(monkeypatch, generate)
        settings.openai_api_key = "sk-secret-123"
        with caplog.at_level(logging.INFO, logger="xbot.visuals"):
            assert visuals.generate_ai_image(settings, "a desk", "t") is None
        assert "sk-secret" not in caplog.text and "HTTP 401" in caplog.text


# ---------------------------------------------------------------------------
# Claude regarde la capture (le contrat avec writer.screenshot_is_clean)
# ---------------------------------------------------------------------------
class TestScreenshotCheckByClaude:
    def test_writer_sends_the_image_the_story_and_the_post(self, settings, state, tmp_path):
        from PIL import Image
        from bot.writer import Writer
        from tests.fakes import FakeLLM
        img = tmp_path / "shot.jpg"
        Image.new("RGB", (1170, 1461), (255, 255, 255)).save(img)
        llm = FakeLLM({"screenshot-check": {"ok": False, "issue": "titre coupé"}})
        ok, why = Writer(settings, llm, state).screenshot_is_clean(img, headline=TITLES[PAGE],
                                                                   post="Voice mode is free now.")
        assert (ok, why) == (False, "titre coupé")
        call = llm.calls[0]
        assert call["images"] == [img]
        assert TITLES[PAGE] in call["user"] and "Voice mode is free now." in call["user"]

    def test_no_answer_means_not_clean(self, settings, state, tmp_path):
        from bot.writer import Writer
        from tests.fakes import FakeLLM
        ok, _ = Writer(settings, FakeLLM({}), state).screenshot_is_clean(tmp_path / "shot.jpg")
        assert ok is False

    def test_llm_builds_an_image_block(self, tmp_path):
        from PIL import Image
        from bot.llm import _image_block
        p = tmp_path / "x.png"
        Image.new("RGB", (3200, 1800), (10, 20, 30)).save(p)
        b = _image_block(p)
        assert b["type"] == "image" and b["source"]["media_type"] == "image/jpeg" and b["source"]["data"]
        assert _image_block(tmp_path / "nonexistent.png") is None


# ---------------------------------------------------------------------------
# Optionnel : un vrai Chromium sur des pages locales (XBOT_REAL_BROWSER=1)
# ---------------------------------------------------------------------------
def _real_browser_wanted() -> bool:
    if os.environ.get("XBOT_REAL_BROWSER") != "1":
        return False
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


real_browser = pytest.mark.skipif(not _real_browser_wanted(),
                                  reason="vrai navigateur : XBOT_REAL_BROWSER=1 et Playwright installé")

LEAD_SVG = ("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='780' height='520'>"
            "<rect width='780' height='520' fill='%23e07a2f'/></svg>")
HEADLINE = "Robots learn to fold laundry at home, and they are getting fast"
PARAS = "".join(f"<p>Paragraph {i}: the robots in the lab now fold towels, sort socks and stack plates "
                "without help from anyone.</p>" for i in range(12))
SLICED_LINES_JS = """(cut) => {
  const out = [], range = document.createRange();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const el = n.parentElement;
    if (!n.textContent.trim() || !el || !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue;
    range.selectNodeContents(n);
    for (const r of range.getClientRects())
      if (r.height >= 6 && r.width >= 2 && r.top < cut && r.bottom > cut) out.push(n.textContent.trim().slice(0, 40));
  }
  return out;
}"""


def article_html(*, lead: str = "", kicker: str = "Robotics", headline: str = HEADLINE, title: str = HEADLINE,
                 top: str = "", after: str = "", style: str = "") -> str:
    kick = f"<div class='kicker'>{kicker}</div>" if kicker else ""
    return ("<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title} | Example News</title><style>"
            "body{margin:0;font:17px/1.5 Georgia,serif;background:#fff;color:#111}"
            "p,.kicker,.byline,h1{padding:0 16px} h1{font:700 30px/1.15 Arial,sans-serif;margin:0 0 12px}"
            ".kicker{font:700 13px/16px Arial;color:#333;margin:0 0 4px;text-transform:uppercase}"
            f"{style}</style></head><body>{top}<main><article>{lead}{kick}<h1>{headline}</h1>"
            f"<div class='byline'>By A. Writer · Oct 15, 2026</div>{PARAS}{after}</article></main></body></html>")


def frame_page(page, html: str) -> tuple[dict, dict]:
    """Les mêmes étapes que _capture_in_browser, sur une page déjà chargée : (mesure du titre, coupe)."""
    page.set_content(html, wait_until="load")
    assert page.evaluate(visuals._PREPARE_JS) is True
    page.evaluate(visuals._SETTLE_JS, visuals.SHOT_VIEWPORT_HEIGHT + 200)
    args = [visuals.SHOT_TOP_MARGIN, visuals.SHOT_MAX_HEIGHT, visuals.HEADLINE_MUST_END_BEFORE]
    measure = None
    for _ in range(4):
        before = page.evaluate(visuals._ALIGN_JS, args)
        page.evaluate(visuals._SETTLE_JS, visuals.SHOT_MAX_HEIGHT + 60)
        measure = page.evaluate(visuals._ALIGN_JS, args)
        if abs(float((measure or {}).get("top", 0)) - float((before or {}).get("top", 0))) < 2:
            break
    return measure, page.evaluate(visuals._FRAME_JS, [visuals.SHOT_MIN_HEIGHT, visuals.SHOT_MAX_HEIGHT])


def colour_pixels(path, test) -> int:
    from PIL import Image
    with Image.open(path) as im:
        small = im.convert("RGB").resize((im.width // 6, im.height // 6))
        data = small.tobytes()
        return sum(1 for i in range(0, len(data), 3) if test(data[i], data[i + 1], data[i + 2]))


@pytest.fixture
def real_page():
    from playwright.sync_api import sync_playwright
    launch, context = visuals._browser_launch_options()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch)
        try:
            yield browser.new_context(**context).new_page()
        finally:
            browser.close()


@pytest.fixture
def local_site():
    """Petit serveur HTTP local (127.0.0.1) : pages[chemin] = HTML ; le reste répond 404."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    pages: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = pages.get(self.path.split("?")[0])
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write((body if body is not None else "<title>404 Not Found</title>").encode("utf-8"))

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield SimpleNamespace(base=f"http://127.0.0.1:{server.server_address[1]}", pages=pages)
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def quick_waits(monkeypatch):
    """Pages locales et statiques : les pauses de la capture sont raccourcies."""
    monkeypatch.setattr(visuals, "time", SimpleNamespace(sleep=lambda s: time.sleep(min(s, 0.25)),
                                                         strftime=time.strftime, time=time.time))


@real_browser
class TestRealBrowser:
    def test_capture_of_a_local_article_end_to_end(self, settings, local_site, quick_waits):
        top = ("<nav class='site-nav'>Home Tech Science AI</nav>"
               "<div class='smartbanner'>Open in the app for a better experience</div>")
        style = (".site-nav{position:sticky;top:0;height:56px;background:#d00;color:#fff}"
                 ".smartbanner{position:fixed;top:0;left:0;right:0;height:120px;background:#00f;color:#fff}"
                 ".ad-slot{height:250px;background:#0b0}")
        ad = "<div><div>Advertisement</div><div class='ad-slot'></div></div>"
        path = "/2026/10/15/robots-learn-to-fold-laundry"
        local_site.pages[path] = article_html(top=top, style=style).replace(
            "<div class='byline'>", ad + "<div class='byline'>")
        url = local_site.base + path
        d = Draft(kind="news", texts=["Robots now fold laundry."], visual=Visual("shot", url),
                  sources=[NewsItem(id="r1", title=HEADLINE, summary="", url=url, source="Example",
                                    published=None)])
        w = StubWriter()
        img, kind = visuals.prepare(settings, d, w, "news")
        assert kind == "shot"
        from PIL import Image
        with Image.open(img) as im:
            assert im.format == "JPEG" and im.width == visuals.SHOT_WIDTH * S
            assert visuals.SHOT_MIN_HEIGHT * S <= im.height <= visuals.SHOT_MAX_HEIGHT * S
        # menu collant (rouge), bandeau fixe (bleu) et pub (verte) ont disparu de la capture
        assert colour_pixels(img, lambda r, g, b: r > 180 and g < 90 and b < 90) == 0
        assert colour_pixels(img, lambda r, g, b: b > 180 and r < 90 and g < 90) == 0
        assert colour_pixels(img, lambda r, g, b: g > 140 and r < 90 and b < 90) == 0
        assert w.calls[0]["headline"] == HEADLINE and w.calls[0]["post"] == "Robots now fold laundry."

    def test_frame_starts_above_the_kicker_and_cuts_between_lines(self, real_page):
        measure, frame = frame_page(real_page, article_html())
        assert visuals.headline_fits(measure) is None and measure["lead"] is False
        cut = visuals.frame_height(frame)
        assert cut is not None
        kicker_top = real_page.evaluate("() => document.querySelector('.kicker').getBoundingClientRect().top")
        assert 10 <= kicker_top < measure["top"] < 80
        assert real_page.evaluate(SLICED_LINES_JS, cut) == []

    def test_lead_image_above_the_headline_is_kept(self, real_page):
        lead = (f"<figure style='margin:0 0 16px'><img src=\"{LEAD_SVG}\" width='390' height='260' "
                "style='width:100%;display:block'><figcaption style='font:12px Arial;padding:4px 16px'>"
                "Photo: a robot folding a towel in a lab.</figcaption></figure>")
        measure, frame = frame_page(real_page, article_html(lead=lead))
        assert measure["lead"] is True and visuals.headline_fits(measure) is None
        img_top, img_bottom = real_page.evaluate(
            "() => { const r = document.querySelector('img').getBoundingClientRect(); return [r.top, r.bottom]; }")
        assert img_bottom >= 110 and img_bottom < measure["top"]           # une bonne part de l'image est dans le cadre
        assert visuals.frame_height(frame) is not None

    def test_self_test(self):
        assert visuals.self_test() == (True, "")

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
        "BUG: _ALIGN_JS cherche l'image d'ouverture d'après le haut du CADRE (déjà 22 px au-dessus du "
        "titre, 14 px au-dessus de la rubrique) : une image collée au titre (écart < 14 px) n'est pas "
        "gardée ; son bas est masqué et laisse une bande blanche."))
    def test_lead_image_right_above_the_headline_is_kept(self, real_page):
        lead = (f"<figure style='margin:0'><img src=\"{LEAD_SVG}\" width='390' height='260' "
                "style='width:100%;display:block'></figure>")
        measure, _ = frame_page(real_page, article_html(lead=lead, kicker=""))
        assert measure["lead"] is True

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
        "BUG: _DISMISS_SELECTORS : Playwright :has-text() est une recherche de sous-chaîne sans casse ; "
        "'button:has-text(\"OK\")' clique « Cookie settings » (ou « Facebook », « Bookmark »…) avant le vrai "
        "bouton OK : ici la page part sur les réglages cookies et la capture est perdue."))
    def test_cookie_settings_button_is_not_taken_for_ok(self, local_site, quick_waits):
        banner = ("<div id='banner' style='position:fixed;bottom:0;left:0;right:0;height:150px;background:#eee'>"
                  "We use cookies. <button onclick=\"location.href='/privacy/cookie-settings'\">Cookie settings"
                  "</button> <button onclick=\"document.getElementById('banner').remove()\">OK</button></div>")
        local_site.pages["/2026/10/15/robots-fold-laundry"] = article_html(top=banner)
        local_site.pages["/privacy/cookie-settings"] = ("<!doctype html><title>Cookie settings</title><p>"
                                                       + "Manage your cookie preferences here. " * 10)
        res = visuals.capture_with_timeout(local_site.base + "/2026/10/15/robots-fold-laundry", "t", timeout_s=60)
        assert res is not None

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
        "BUG: _PREPARE_JS : « gpt » fait partie des mots de publicité ; une figure de l'article sur GPT "
        "(aria-label, id ou classe « gpt-… ») est masquée comme une pub."))
    def test_article_figure_about_gpt_is_not_taken_for_an_ad(self, real_page):
        figure = (f"<figure aria-label='GPT-5 benchmark chart' style='margin:0'><img src=\"{LEAD_SVG}\" "
                  "width='390' height='260' style='width:100%'></figure>")
        real_page.set_content(article_html(headline="OpenAI's GPT-5 beats every benchmark we threw at it",
                                           after=figure), wait_until="load")
        assert real_page.evaluate(visuals._PREPARE_JS) is True
        assert real_page.evaluate("() => getComputedStyle(document.querySelector('figure')).display") != "none"

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
        "BUG: _PREPARE_JS masque les étiquettes « Advertising »/« Sponsored »/« Ad » sans exclure ce qui est "
        "DANS le titre (h.contains(el) manque) : un mot du titre peut disparaître de la capture."))
    def test_words_of_the_headline_are_never_hidden(self, real_page):
        real_page.set_content(article_html(headline="Google rethinks <span>advertising</span> for the age of AI",
                                           title="Google rethinks advertising for the age of AI"),
                              wait_until="load")
        assert real_page.evaluate(visuals._PREPARE_JS) is True
        assert real_page.evaluate("() => document.querySelector('h1').innerText") == \
            "Google rethinks advertising for the age of AI"

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
        "BUG: _SETTLE_JS masque la <figure> d'une image cassée ou trop lente sans vérifier qu'elle ne "
        "contient pas le titre : titre en surimpression sur la photo → titre masqué, capture refusée."))
    def test_broken_hero_image_does_not_hide_the_headline(self, real_page):
        html = article_html(kicker="", headline="").replace("<h1></h1>", "").replace(
            "<article>", "<article><figure class='hero' style='margin:0'><img src='data:image/png;base64,AAAA' "
            "width='390' height='260' style='width:100%;display:block'><figcaption><h1>" + HEADLINE
            + "</h1></figcaption></figure>")
        measure, _ = frame_page(real_page, html)
        assert visuals.headline_fits(measure) is None
