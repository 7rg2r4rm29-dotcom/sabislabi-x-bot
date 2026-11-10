"""Cartes (bot/cards.py) : le HTML construit, puis le vrai rendu par Chromium.

Les tests de rendu réel ne tournent que sur demande (XBOT_RENDER_TESTS=1, ou
XBOT_BROWSER_PATH pour un navigateur préinstallé) : pendant une installation
sur le serveur, une différence de navigateur ne doit jamais bloquer la mise à
jour. Là-bas, c'est `python -m bot.main --check` qui dessine une vraie carte
et dit si les images fonctionnent."""
from __future__ import annotations

import os
import re
import sys

import pytest

from bot import cards

PROMPT = {"type": "prompt", "title": "Dinner from a *fridge photo*",
          "lines": ["Here is a photo of my fridge. Give me 3 dinners I can make tonight."],
          "note": "Works in ChatGPT, Claude or Gemini. Free versions too."}
STEPS = {"type": "steps", "title": "Understand any *letter*",
         "lines": ["Take a photo of it", "Ask: explain this like I'm 12", "Ask what you have to do, and by when"],
         "note": "Double-check anything important."}
LIST = {"type": "list", "title": "5 things to *try tonight*",
        "lines": ["Dinner from a fridge photo", "A confusing letter, explained", "A polite no to your boss"],
        "note": ""}
FACT = {"type": "fact", "title": "ChatGPT voice is now *free*",
        "lines": ["No paid plan needed", "Talk instead of typing"], "note": ""}
PHONE = {"type": "phone", "title": "ChatGPT can now *talk* to you", "lines": ["It's free"], "note": ""}


def html_of(card, **kw):
    opts = {"theme": "cream", "badge": "Everyday AI · No. 7", "handle": "sabislabi"}
    opts.update(kw)
    return cards.build_html(card, **opts)


def body_of(page: str) -> str:
    """La page sans ses styles (les polices embarquées font plusieurs centaines de Ko)."""
    return page.split("</style>", 1)[1]


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
class TestHtml:
    def test_title_emphasis_and_escaping(self):
        assert cards.title_html("Dinner from a *fridge photo*") == "Dinner from a <em>fridge photo</em>"
        assert cards.title_html("*One* and *two*") == "<em>One</em> and <em>two</em>"
        assert cards.title_html("5 * 3 = 15") == "5  3 = 15"              # astérisque isolé : retiré
        assert cards.title_html("<b>bold</b> & *<i>*") == "&lt;b&gt;bold&lt;/b&gt; &amp; <em>&lt;i&gt;</em>"
        assert cards.title_html("") == "" and cards.title_html(None) == ""
        assert cards.plain_title("  Dinner from a *fridge   photo* ") == "Dinner from a fridge photo"

    def test_every_type_builds(self):
        for card in (PROMPT, STEPS, LIST, FACT):
            page = html_of(card)
            body = body_of(page)
            assert page.startswith("<!doctype html>") and "@font-face" in page
            assert "Everyday AI · No. 7" in body and "@sabislabi" in body
            for line in card["lines"]:
                assert cards._esc(line) in body
            if card["note"]:
                assert card["note"] in body
        assert "Copy this" in body_of(html_of(PROMPT)) and "&ldquo;Here is a photo" in body_of(html_of(PROMPT))
        steps = body_of(html_of(STEPS))
        assert "<b>1</b>" in steps and "<b>3</b>" in steps and "<b>4</b>" not in steps
        assert "<b>1</b>" not in body_of(html_of(LIST)) and "<svg" in body_of(html_of(LIST))

    def test_fact_alone_is_centered_with_a_bar(self):
        solo = body_of(html_of({"type": "fact", "title": "It can *talk*", "lines": [], "note": ""}))
        assert 'class="wrap fact solo"' in solo and 'class="bar"' in solo and 'data-max="210"' in solo
        full = body_of(html_of(FACT))
        assert 'class="wrap fact"' in full and 'class="bar"' not in full and 'data-max="176"' in full

    def test_text_is_escaped_everywhere(self):
        evil = {"type": "list", "title": "<script>alert(1)</script>",
                "lines": ['<img src=x onerror="x()">', "a & b"], "note": "<b>note</b>"}
        body = body_of(html_of(evil, badge="<i>badge</i>", handle="<u>me</u>"))
        assert "<script>" not in body and "<img src=x" not in body and "<b>note</b>" not in body
        assert "&lt;script&gt;" in body and "a &amp; b" in body and "&lt;i&gt;badge" in body

    def test_handle_shown_once_with_at(self):
        assert "@sabislabi<" in body_of(html_of(LIST, handle="@sabislabi"))
        assert "@@" not in body_of(html_of(LIST, handle="@sabislabi"))

    def test_phone_layout(self):
        body = body_of(html_of(PHONE, image_data_uri="data:image/jpeg;base64,AAAA", source="theverge.com",
                               chrome=("#101010", "#FFFFFF", "#262626")))
        assert "9:41" in body and "theverge.com" in body and 'src="data:image/jpeg;base64,AAAA"' in body
        assert "--chrome:#101010" in body and "--chrome-ink:#FFFFFF" in body and "--pill:#262626" in body
        assert 'class="wrap phonecard"' in body

    def test_unknown_type(self, tmp_path):
        with pytest.raises(ValueError):
            html_of({"type": "poster", "title": "x"})
        assert cards.render({"type": "poster", "title": "x"}, tmp_path / "x.png", theme="cream", badge="b",
                            handle="h") is False
        assert cards.render(PHONE, tmp_path / "p.png", theme="cream", badge="b", handle="h") is False   # sans capture

    def test_themes(self):
        keys = set(cards.THEMES["cream"])
        assert set(cards.THEME_ORDER) == set(cards.THEMES) and len(cards.THEME_ORDER) >= 5
        for name, theme in cards.THEMES.items():
            assert set(theme) == keys, name
        assert [cards.theme_for(i) for i in range(len(cards.THEME_ORDER) + 1)] == \
            [*cards.THEME_ORDER, cards.THEME_ORDER[0]]
        assert "--bg:#0D0E12" in body_of(html_of(LIST, theme="midnight"))
        assert "--bg:#F3EDE2" in body_of(html_of(LIST, theme="unknown-theme"))     # repli sur la première

    def test_fonts_are_bundled(self):
        for name in ("inter.woff2", "bricolage.woff2", "instrument-italic.woff2"):
            f = cards.FONTS_DIR / name
            assert f.is_file() and f.read_bytes()[:4] == b"wOF2", name
        licences = sorted(p.name for p in cards.FONTS_DIR.glob("LICENSE-*.txt"))
        assert len(licences) == 3
        css = cards._fonts_css()
        assert css.count("@font-face") == 3 and "http" not in css       # rien n'est chargé depuis Internet

    def test_no_network_reference_in_the_page(self):
        for card in (PROMPT, STEPS, LIST, FACT):
            assert not re.search(r"""(?:src|href)=["']https?://""", html_of(card))
            assert "url(http" not in html_of(card)

    def test_launch_options(self, monkeypatch):
        monkeypatch.delenv("XBOT_BROWSER_PATH", raising=False)
        monkeypatch.delenv("XBOT_BROWSER_PROXY", raising=False)
        assert cards.launch_options() == {"headless": True, "args": [
            "--disable-dev-shm-usage", "--no-first-run", "--disable-extensions"]}
        monkeypatch.setenv("XBOT_BROWSER_PATH", "/opt/chrome")
        monkeypatch.setenv("XBOT_BROWSER_PROXY", "http://proxy:8080")
        opts = cards.launch_options()
        assert opts["executable_path"] == "/opt/chrome" and opts["proxy"] == {"server": "http://proxy:8080"}

    def test_chrome_colors(self, tmp_path):
        from PIL import Image
        light, dark = tmp_path / "light.png", tmp_path / "dark.png"
        Image.new("RGB", (40, 40), (255, 255, 255)).save(light)
        Image.new("RGB", (40, 40), (16, 16, 16)).save(dark)
        assert cards.chrome_colors(light) == ("#FFFFFF", "#000000", "#E5E5E5")
        bg, ink, pill = cards.chrome_colors(dark)
        assert (bg, ink) == ("#101010", "#FFFFFF") and pill != bg
        assert cards.chrome_colors(tmp_path / "missing.png") == ("#FFFFFF", "#000000", "#EEEEF1")

    def test_render_without_playwright(self, monkeypatch, tmp_path):
        monkeypatch.setitem(sys.modules, "playwright", None)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
        assert cards.render(LIST, tmp_path / "x.png", theme="cream", badge="b", handle="h") is False
        assert cards.self_test() == (False, "Playwright n'est pas installé")

    def test_render_timeout(self, monkeypatch, tmp_path):
        import time
        monkeypatch.setattr(cards, "_render_in_browser", lambda html, out: time.sleep(0.6) or True)
        t0 = time.monotonic()
        assert cards.render_html("<html></html>", tmp_path / "x.png", timeout_s=0.1) is False
        assert time.monotonic() - t0 < 0.5
        assert "trop long" in cards.LAST_ERROR

    def test_render_crash_is_contained(self, monkeypatch, tmp_path):
        def boom(html, out):
            raise RuntimeError("Executable doesn't exist at /x/chrome\nmore details")
        monkeypatch.setattr(cards, "_render_in_browser", boom)
        assert cards.render(LIST, tmp_path / "x.png", theme="cream", badge="b", handle="h") is False
        assert cards.LAST_ERROR == "Executable doesn't exist at /x/chrome"
        assert cards.self_test() == (False, "Executable doesn't exist at /x/chrome")

    def test_big_png_is_shrunk(self, tmp_path, monkeypatch):
        from PIL import Image
        import os
        p = tmp_path / "big.png"
        Image.frombytes("RGB", (cards.W * 2, cards.H * 2), os.urandom(cards.W * 2 * cards.H * 2 * 3)).save(p)
        monkeypatch.setattr(cards, "MAX_PNG_BYTES", 6_000_000)
        assert p.stat().st_size > 6_000_000
        cards._shrink_if_too_big(p)
        assert Image.open(p).size == (cards.W, cards.H)       # bruit pur : il faut descendre à 1×
        small = tmp_path / "small.png"
        Image.new("RGB", (cards.W * 2, cards.H * 2), (240, 240, 240)).save(small)
        cards._shrink_if_too_big(small)
        assert Image.open(small).size == (cards.W * 2, cards.H * 2)   # léger : on n'y touche pas


# ---------------------------------------------------------------------------
# Vrai rendu (Chromium) — sauté si le navigateur n'est pas disponible
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    if not (os.getenv("XBOT_RENDER_TESTS") or os.getenv("XBOT_BROWSER_PATH")):
        pytest.skip("rendu réel non demandé (XBOT_RENDER_TESTS=1)")
    pytest.importorskip("playwright", reason="Playwright n'est pas installé")
    probe = tmp_path_factory.mktemp("probe") / "probe.png"
    if not cards.render(LIST, probe, theme="cream", badge="Check", handle="check"):
        pytest.skip(f"Chromium indisponible ici ({cards.LAST_ERROR or 'rendu impossible'})")
    return probe


class TestRealRender:
    def test_png_size_and_weight(self, browser):
        from PIL import Image
        img = Image.open(browser)
        assert img.size == (cards.W * cards.RENDER_SCALE, cards.H * cards.RENDER_SCALE)
        assert img.size[0] / img.size[1] == pytest.approx(4 / 5)      # portrait 4:5, montré en entier par X
        assert 20_000 < browser.stat().st_size <= cards.MAX_PNG_BYTES

    def test_every_type(self, browser, tmp_path):
        for i, card in enumerate((PROMPT, STEPS, LIST, FACT)):
            out = tmp_path / f"{card['type']}.png"
            assert cards.render(card, out, theme=cards.theme_for(i + 1), badge="Everyday AI · No. 12",
                                handle="sabislabi"), card["type"]
            assert out.stat().st_size <= cards.MAX_PNG_BYTES

    def test_palettes_really_differ(self, browser, tmp_path):
        from PIL import Image
        corners = set()
        for theme in cards.THEME_ORDER:
            out = tmp_path / f"{theme}.png"
            assert cards.render(LIST, out, theme=theme, badge="b", handle="h")
            corners.add(Image.open(out).convert("RGB").getpixel((20, 2600)))
        assert len(corners) == len(cards.THEME_ORDER)

    def test_longest_texts_the_writer_accepts_still_fit(self, browser, tmp_path):
        """Les longueurs maximales tolérées par bot/writer.py doivent toujours tenir sur la carte."""
        from bot.writer import CARD_LINES, CARD_NOTE_MAX, CARD_TITLE_MAX
        title = ("How to read a confusing *letter* from your bank in one go, today")[:CARD_TITLE_MAX + 2]
        assert len(title.replace("*", "")) == CARD_TITLE_MAX
        note = ("Works in ChatGPT, Claude or Gemini. Free versions too. Double-check anything important, "
                "always.")[:CARD_NOTE_MAX]
        filler = "Ask it to explain every difficult word, then to give you the three key points again. "
        for ctype in ("prompt", "steps", "list", "fact"):
            _lo, hi, chars = CARD_LINES[ctype]
            card = {"type": ctype, "title": title, "lines": [(filler * 4)[:chars]] * hi, "note": note}
            assert cards.render(card, tmp_path / f"{ctype}.png", theme="cream", badge="Everyday AI · No. 128",
                                handle="sabislabi"), ctype

    def test_shortest_cards(self, browser, tmp_path):
        for card in ({"type": "fact", "title": "Free", "lines": [], "note": ""},
                     {"type": "prompt", "title": "Try", "lines": ["Quiz me."], "note": ""},
                     {"type": "list", "title": "Ask", "lines": ["Why?", "How?"], "note": ""}):
            assert cards.render(card, tmp_path / f"{card['type']}.png", theme="blush", badge="b", handle="h")

    def test_text_that_cannot_fit_is_refused(self, browser, tmp_path):
        card = {"type": "list", "title": "A title", "lines": ["A very long line of text " * 12] * 9, "note": ""}
        out = tmp_path / "too_much.png"
        assert cards.render(card, out, theme="cream", badge="b", handle="h") is False
        assert not out.exists() and "ne tient pas" in cards.LAST_ERROR

    def test_phone_card_with_a_capture(self, browser, tmp_path):
        from PIL import Image, ImageDraw
        shot = tmp_path / "shot.jpg"
        img = Image.new("RGB", (cards.PHONE_PAGE_WIDTH * 4, cards.PHONE_PAGE_VISIBLE_HEIGHT * 4), (255, 255, 255))
        ImageDraw.Draw(img).rectangle((60, 120, 1500, 520), fill=(20, 20, 20))
        img.save(shot, quality=90)
        out = tmp_path / "phone.png"
        assert cards.render(PHONE, out, theme="midnight", badge="AI news, made simple", handle="sabislabi",
                            shot_path=shot, source="theverge.com")
        result = Image.open(out).convert("RGB")
        assert result.size == (cards.W * cards.RENDER_SCALE, cards.H * cards.RENDER_SCALE)
        # La capture (blanche) est bien visible dans le bas de l'image, à l'intérieur du téléphone.
        assert result.getpixel((result.width // 2, result.height - 60)) == (255, 255, 255)

    def test_self_test(self, browser):
        assert cards.self_test() == (True, "")
