"""Visuels (bot/visuals.py) : la photo de l'actu CITÉE, jamais une capture d'écran.

La page de l'actu est lue en HTTP (jamais X), on y prend la photo d'ouverture qu'elle
déclare (og:image) et son crédit ; un crédit d'agence ou du média lui-même est refusé
d'office ; la photo est téléchargée (jamais depuis X ni une adresse privée), remise en
JPEG propre, puis Claude la regarde avant qu'elle serve (sinon : actu suivante, puis
texte seul). Aucune exception vers l'appelant. Aucun accès réseau : pages et images
sont simulées (tests/conftest.py bloque la lecture des pages et la résolution des noms)."""
from __future__ import annotations

import base64
import io
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import utils, visuals
from bot.models import Draft, NewsItem, Visual

TC = "https://techcrunch.com/2026/11/13/chatgpts-new-wallet-lets-ai-agents-shop/"
VERGE = "https://www.theverge.com/2026/11/13/openai-chatgpt-wallet"
BLOG = "https://openai.com/index/introducing-chatgpt-wallet/"
XPOST = "https://x.com/OpenAI/status/1846000000000000000"
TC_PHOTO = "https://techcrunch.com/wp-content/uploads/2026/11/chatgpt-wallet.jpg?w=1200"
VERGE_PHOTO = "https://platform.theverge.com/wp-content/uploads/2026/11/wallet.jpg"
BLOG_PHOTO = "https://images.openai.com/wallet-hero.png"
TITLES = {TC: "ChatGPT's new Wallet lets AI agents shop and check out for you",
          VERGE: "ChatGPT can now pay for things for you",
          BLOG: "Introducing ChatGPT Wallet"}


def page(title: str, photo: str = "", credit: str = "", site: str = "", extra_head: str = "",
         body_before: str = "") -> str:
    """Une page d'article : l'en-tête (photo déclarée pour le partage), puis le titre, la
    photo d'ouverture et sa légende de crédit, puis le texte."""
    head = f"<title>{title}</title>"
    if photo:
        head += f'<meta property="og:image" content="{photo}">'
    if site:
        head += f'<meta property="og:site_name" content="{site}">'
    head += extra_head
    fig = (f'<figure><img src="{photo}"><figcaption><strong>Image Credits:</strong>{credit}</figcaption>'
           "</figure>") if credit else ""
    return (f"<!doctype html><html><head>{head}</head><body><nav>Home · AI · Apps</nav>{body_before}"
            f"<article><h1>{title}</h1>{fig}<p>A real article about something new, written for everyone. "
            "It explains what changed and who can use it today.</p></article></body></html>")


def jpeg(width: int = 1200, height: int = 675, color=(40, 90, 160), fmt: str = "JPEG", mode: str = "RGB",
         **save) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new(mode, (width, height), color).save(buf, fmt, **save)
    return buf.getvalue()


def item(url: str = TC, title: str | None = None, official: bool = False) -> NewsItem:
    return NewsItem(id=url[-16:], title=title or TITLES.get(url, "A story"), summary="", url=url,
                    source="Site", published=None, official=official)


def photo_draft(target: str = TC, cited=(TC,), texts=None, official=(), **kw) -> Draft:
    return Draft(kind="news", texts=list(texts or ["ChatGPT can now shop for you."]),
                 visual=Visual("photo", target),
                 sources=[item(u, official=u in official) for u in cited], **kw)


class Web:
    """Remplace sources.default_http_get : des pages connues, 404 pour le reste."""

    def __init__(self, pages: dict[str, str] | None = None) -> None:
        self.pages = dict(pages or {})
        self.urls: list[str] = []

    def __call__(self, url: str, accept: str = "") -> tuple[int, str]:
        self.urls.append(url)
        return (200, self.pages[url]) if url in self.pages else (404, "")


class Images:
    """Remplace le téléchargement des photos : des octets par adresse."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = dict(files or {})
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes | None:
        self.urls.append(url)
        return self.files.get(url)


class Checker:
    """Le regard de Claude sur la photo (writer.photo_is_usable)."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers) or [(True, "fine")]
        self.calls: list[dict] = []

    def photo_is_usable(self, path, headline: str = "", post: str = "", credit: str = "",
                        official: bool = False):
        self.calls.append({"path": Path(path), "exists": Path(path).is_file(), "headline": headline,
                           "post": post, "credit": credit, "official": official})
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def web(monkeypatch):
    from bot import sources
    w = Web({TC: page(TITLES[TC], TC_PHOTO, "OpenAI", "TechCrunch"),
             VERGE: page(TITLES[VERGE], VERGE_PHOTO, "Jane Doe / The Verge", "The Verge"),
             BLOG: page(TITLES[BLOG], BLOG_PHOTO, "", "OpenAI")})
    monkeypatch.setattr(sources, "default_http_get", w)
    return w


@pytest.fixture
def images(monkeypatch):
    imgs = Images({TC_PHOTO: jpeg(), VERGE_PHOTO: jpeg(), BLOG_PHOTO: jpeg(fmt="PNG")})
    monkeypatch.setattr(visuals, "fetch_image_bytes", imgs)
    return imgs


# ---------------------------------------------------------------------------
# Adresses
# ---------------------------------------------------------------------------
class TestUrls:
    @pytest.mark.parametrize("url, host", [
        ("https://www.TheVerge.com/a", "www.theverge.com"),
        ("https://x.com\\@site.com/a", "x.com"),
        ("https://ｘ.com/a", "x.com"),
        ("ftp://site.com/a", ""),
        ("not a url", ""),
    ])
    def test_host_of(self, url, host):
        assert visuals.host_of(url) == host

    @pytest.mark.parametrize("url", ["https://x.com/a", "https://mobile.twitter.com/a", "https://t.co/xyz",
                                     "https://pbs.twimg.com/media/a.jpg", "http://X.COM/a"])
    def test_x_is_recognized(self, url):
        assert visuals.is_x_url(url)

    @pytest.mark.parametrize("url", ["https://xcom.example/a", "https://notx.com/a", "https://techcrunch.com/a"])
    def test_not_x(self, url):
        assert not visuals.is_x_url(url)

    @pytest.mark.parametrize("url, site", [("https://www.theverge.com/a", "theverge.com"),
                                           ("https://www.bbc.co.uk/news/a", "bbc.co.uk"),
                                           ("https://spectrum.ieee.org/a", "ieee.org")])
    def test_site_of(self, url, site):
        assert visuals.site_of(url) == site

    def test_public_host(self, monkeypatch):
        table = {"cdn.example.com": ["93.184.216.34"], "meta.example": ["169.254.169.254"],
                 "lan.example": ["10.0.0.7"], "mixed.example": ["93.184.216.34", "127.0.0.1"],
                 "v6.example": ["2606:2800:220:1:248:1893:25c8:1946"]}

        def resolve(host):
            if host not in table:
                raise OSError("unknown host")
            return table[host]
        monkeypatch.setattr(visuals, "_resolve", resolve)
        assert visuals.public_host("cdn.example.com") and visuals.public_host("v6.example")
        for host in ("meta.example", "lan.example", "mixed.example", "nowhere.example", "localhost",
                     "printer.local", "db.internal", ""):
            assert not visuals.public_host(host), host


class TestFiles:
    @pytest.mark.parametrize("tag, safe", [("tip_2026-10-15", "tip_2026-10-15"), ("../../etc/pw d", "etc-pw-d"),
                                           ("", "post"), ("***", "post"), ("x" * 60, "x" * 40)])
    def test_safe_tag(self, tag, safe):
        assert visuals._safe_tag(tag) == safe

    def test_cleanup_keeps_the_latest_files(self, tmp_path):
        import os
        for i in range(5):
            f = tmp_path / f"f{i}.jpg"
            f.write_bytes(b"x")
            os.utime(f, (1000 + i, 1000 + i))
        visuals._cleanup_old(tmp_path, 2)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["f3.jpg", "f4.jpg"]

    def test_cleanup_of_a_missing_folder(self, tmp_path):
        visuals._cleanup_old(tmp_path / "missing", 2)


# ---------------------------------------------------------------------------
# La page : sa photo d'ouverture et son crédit
# ---------------------------------------------------------------------------
class TestLeadImage:
    def test_open_graph_photo(self):
        meta = visuals.page_meta(page(TITLES[TC], TC_PHOTO, "OpenAI", "TechCrunch"))
        assert visuals.lead_image_url(meta, TC) == TC_PHOTO
        assert meta["og:site_name"] == "TechCrunch"

    def test_relative_and_protocol_relative_addresses(self):
        rel = visuals.page_meta('<head><meta property="og:image" content="/img/robot.jpg"></head>')
        assert visuals.lead_image_url(rel, TC) == "https://techcrunch.com/img/robot.jpg"
        proto = visuals.page_meta('<head><meta property="og:image" content="//cdn.site.com/robot.jpg"></head>')
        assert visuals.lead_image_url(proto, TC) == "https://cdn.site.com/robot.jpg"

    def test_entities_are_decoded(self):
        meta = visuals.page_meta('<head><meta property="og:image" '
                                 'content="https://cdn.site.com/r.jpg?w=1200&amp;h=630"></head>')
        assert visuals.lead_image_url(meta, TC) == "https://cdn.site.com/r.jpg?w=1200&h=630"

    def test_secure_url_first_then_twitter_image(self):
        both = visuals.page_meta('<head><meta property="og:image" content="http://a.com/1.jpg">'
                                 '<meta property="og:image:secure_url" content="https://a.com/1.jpg"></head>')
        assert visuals.lead_image_url(both, TC) == "https://a.com/1.jpg"
        tw = visuals.page_meta('<head><meta name="twitter:image" content="https://a.com/2.jpg"></head>')
        assert visuals.lead_image_url(tw, TC) == "https://a.com/2.jpg"

    def test_never_an_image_from_x(self):
        meta = visuals.page_meta('<head><meta property="og:image" content="https://pbs.twimg.com/media/a.jpg">'
                                 '<meta name="twitter:image" content="https://cdn.site.com/b.jpg"></head>')
        assert visuals.lead_image_url(meta, TC) == "https://cdn.site.com/b.jpg"

    @pytest.mark.parametrize("photo", ["https://site.com/logo.png", "https://site.com/static/default-share.jpg",
                                       "https://site.com/img/og-default.png", "https://site.com/placeholder.jpg"])
    def test_the_sites_default_image_is_not_a_photo_of_the_story(self, photo):
        meta = visuals.page_meta(f'<head><meta property="og:image" content="{photo}"></head>')
        assert visuals.lead_image_url(meta, TC) == ""

    def test_a_companys_logo_named_in_the_file_is_left_to_claude(self):
        meta = visuals.page_meta('<head><meta property="og:image" content="https://site.com/u/openai-logo.jpg">'
                                 "</head>")
        assert visuals.lead_image_url(meta, TC) == "https://site.com/u/openai-logo.jpg"

    def test_no_photo_or_only_in_the_body(self):
        assert visuals.lead_image_url(visuals.page_meta("<head><title>t</title></head>"), TC) == ""
        late = '<head></head><body><meta property="og:image" content="https://a.com/1.jpg"></body>'
        assert visuals.lead_image_url(visuals.page_meta(late), TC) == ""
        assert visuals.page_meta("") == {} and visuals.page_meta("<<<>>>") == {}


class TestCredit:
    @pytest.mark.parametrize("credit_html, credit", [
        ("<figcaption><strong>Image Credits:</strong>OpenAI</figcaption>", "OpenAI"),
        ("<figcaption>Photograph: Jane Doe/Reuters</figcaption>", "Jane Doe/Reuters"),
        ("<div class='caption'>Photo by Jane Doe / The Verge</div>", "Jane Doe / The Verge"),
        ("<small>Courtesy of Google</small>", "Google"),
        ("<span>(Image credit: Future)</span>", "Future"),
        ("<p class='credit'>Credit: Samsung</p>", "Samsung"),
        ("<figcaption>The new glasses. Image: Meta</figcaption>", "Meta"),
        ("<figcaption>Image Credits: <a href='#'>Meta/Alexandr Wang<span>(opens in a new window)</span></a>"
         "</figcaption>", "Meta/Alexandr Wang"),
    ])
    def test_credit_line_near_the_headline(self, credit_html, credit):
        html = (f"<html><body><nav>Menu</nav><article><h1>{TITLES[TC]}</h1><figure><img src='a.jpg'>"
                f"{credit_html}</figure><p>Body text of the article.</p></article></body></html>")
        assert visuals.photo_credit(html, TITLES[TC]) == credit

    def test_no_credit(self):
        assert visuals.photo_credit(page(TITLES[TC], TC_PHOTO), TITLES[TC]) == ""
        assert visuals.photo_credit("", "x") == ""

    def test_the_first_credit_is_the_lead_photos(self):
        html = (f"<article><h1>{TITLES[TC]}</h1><figure><figcaption>Image Credits: OpenAI</figcaption></figure>"
                "<p>Text.</p><figure><figcaption>Image Credits: Getty Images</figcaption></figure></article>")
        assert visuals.photo_credit(html, TITLES[TC]) == "OpenAI"

    def test_a_credit_far_below_the_headline_is_not_the_lead_photos(self):
        filler = "<p>" + "A long paragraph of the article that goes on. " * 200 + "</p>"
        html = (f"<article><h1>{TITLES[TC]}</h1>{filler}<aside>Related: Photo: Getty Images</aside></article>")
        assert visuals.photo_credit(html, TITLES[TC]) == ""

    def test_scripts_and_styles_are_not_read(self):
        html = (f"<h1>{TITLES[TC]}</h1><script>var credit = 'Image Credits: Getty';</script>"
                "<figcaption>Image Credits: OpenAI</figcaption>")
        assert visuals.photo_credit(html, TITLES[TC]) == "OpenAI"


class TestCreditProblem:
    @pytest.mark.parametrize("credit", ["Getty Images", "Jane Doe/Reuters", "AP Photo/John Smith", "AP",
                                        "Jane Doe/AFP via Getty Images", "Shutterstock", "Bloomberg via Getty",
                                        "Alamy Stock Photo", "picture alliance/dpa", "Unsplash"])
    def test_agencies_and_photo_libraries(self, credit):
        assert "agence" in visuals.credit_problem(credit, "TechCrunch", TC)
        assert "agence" in visuals.credit_problem(credit, "OpenAI", BLOG)      # même chez l'entreprise

    @pytest.mark.parametrize("credit", ["Future", "Staff", "File photo"])
    def test_the_outlets_staff(self, credit):
        assert "média" in visuals.credit_problem(credit, "Tom's Guide", "https://www.tomsguide.com/a")

    def test_the_outlets_own_photographer(self):
        assert "média lui-même" in visuals.credit_problem("Jane Doe / The Verge", "The Verge", VERGE)
        assert "média lui-même" in visuals.credit_problem("TechCrunch", "TechCrunch", TC)
        assert "média lui-même" in visuals.credit_problem("Jane Doe/BBC", "", "https://www.bbc.co.uk/news/a")

    @pytest.mark.parametrize("credit", ["OpenAI", "Google", "Samsung", "Apple", "Courtesy of Meta", "", "  "])
    def test_the_company_of_the_story(self, credit):
        assert visuals.credit_problem(credit, "TechCrunch", TC) is None

    def test_the_site_name_only_counts_on_a_news_site(self):
        assert visuals.credit_problem("Samsung", "Samsung Newsroom", "https://news.samsung.com/a") is None

    def test_the_site_name_is_matched_word_for_word(self):
        ign = "https://www.ign.com/articles/a"
        assert visuals.credit_problem("Courtesy of Signal Foundation", "IGN", ign) is None
        assert "média lui-même" in visuals.credit_problem("Jane Doe/IGN", "IGN", ign)
        # Sans nom de site déclaré : le domaine suffit, collé ou non.
        assert "média lui-même" in visuals.credit_problem("Photo by Jane Doe / The Verge", "theverge.com", VERGE)
        assert "média lui-même" in visuals.credit_problem("TechCrunch staff photo", "", TC)


class TestFindStoryPhoto:
    def test_photo_credit_and_site(self):
        w = Web({TC: page(TITLES[TC], TC_PHOTO, "OpenAI", "TechCrunch")})
        found = visuals.find_story_photo(TC, TITLES[TC], http_get=w)
        assert found == visuals.PagePhoto(image_url=TC_PHOTO, credit="OpenAI", site="TechCrunch")
        assert w.urls == [TC]

    def test_site_falls_back_to_the_domain_and_title_to_the_page(self):
        w = Web({TC: page("", TC_PHOTO, extra_head=f'<meta property="og:title" content="{TITLES[TC]}">')})
        found = visuals.find_story_photo(TC, http_get=w)
        assert found.site == "techcrunch.com" and found.credit == ""

    @pytest.mark.parametrize("pages", [{}, {TC: ""}, {TC: page(TITLES[TC])}])
    def test_no_page_or_no_photo(self, pages):
        assert visuals.find_story_photo(TC, TITLES[TC], http_get=Web(pages)) is None

    def test_never_x_and_never_raises(self):
        w = Web({XPOST: page("x", TC_PHOTO)})
        assert visuals.find_story_photo(XPOST, http_get=w) is None and w.urls == []
        assert visuals.find_story_photo("not a url", http_get=w) is None

        def boom(url):
            raise TimeoutError("slow")
        assert visuals.find_story_photo(TC, http_get=boom) is None

    def test_reads_pages_with_the_article_reader_by_default(self, web):
        assert visuals.find_story_photo(TC, TITLES[TC]).image_url == TC_PHOTO and web.urls == [TC]


# ---------------------------------------------------------------------------
# Téléchargement (requests simulé) et mise au propre (Pillow)
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"", location=None):
        self.status_code = status
        self.headers = dict(headers or {})
        if location:
            self.headers["Location"] = location
        self.is_redirect = bool(location) and status in (301, 302, 303, 307, 308)
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self.body), 1000):
            yield self.body[i:i + 1000]


@pytest.fixture
def net(monkeypatch):
    """Un faux Internet pour fetch_image_bytes : noms → adresses, adresses → réponses."""
    import requests
    hosts = {"cdn.example.com": ["93.184.216.34"], "other.example.com": ["93.184.216.35"],
             "internal.example.com": ["10.0.0.5"]}
    routes: dict[str, FakeResponse] = {}
    calls: list[str] = []

    def resolve(host):
        if host not in hosts:
            raise OSError("unknown")
        return hosts[host]

    def get(url, timeout=None, headers=None, stream=False, allow_redirects=True):
        assert stream and not allow_redirects and timeout == visuals.PHOTO_TIMEOUT_S
        assert "image/" in headers["Accept"]
        calls.append(url)
        return routes.get(url) or FakeResponse(404)
    monkeypatch.setattr(visuals, "_resolve", resolve)
    monkeypatch.setattr(requests, "get", get)
    return SimpleNamespace(routes=routes, calls=calls, hosts=hosts)


class TestFetchImage:
    URL = "https://cdn.example.com/a.jpg"

    def test_an_image(self, net):
        net.routes[self.URL] = FakeResponse(200, {"Content-Type": "image/jpeg"}, b"JPEGDATA" * 10)
        assert visuals.fetch_image_bytes(self.URL) == b"JPEGDATA" * 10

    def test_redirects_are_followed_one_by_one(self, net):
        net.routes[self.URL] = FakeResponse(302, location="/b.jpg")
        net.routes["https://cdn.example.com/b.jpg"] = FakeResponse(301, location="https://other.example.com/c.jpg")
        net.routes["https://other.example.com/c.jpg"] = FakeResponse(200, {"Content-Type": "image/png"}, b"PNG")
        assert visuals.fetch_image_bytes(self.URL) == b"PNG"
        assert net.calls == [self.URL, "https://cdn.example.com/b.jpg", "https://other.example.com/c.jpg"]

    @pytest.mark.parametrize("target", ["https://pbs.twimg.com/media/a.jpg", "https://internal.example.com/a.jpg",
                                        "http://localhost/a.jpg", "https://unknown.example.com/a.jpg"])
    def test_never_x_nor_a_private_address(self, net, target):
        net.routes[self.URL] = FakeResponse(302, location=target)
        assert visuals.fetch_image_bytes(self.URL) is None
        assert net.calls == [self.URL]                         # la destination n'est jamais demandée
        assert visuals.fetch_image_bytes(target) is None and net.calls == [self.URL]

    @pytest.mark.parametrize("response", [
        FakeResponse(404),
        FakeResponse(200, {"Content-Type": "text/html"}, b"<html>"),
        FakeResponse(200, {"Content-Type": "image/jpeg", "Content-Length": str(11 * 2**20)}, b"x"),
        FakeResponse(302),                                     # redirection sans destination
    ])
    def test_refused(self, net, response):
        net.routes[self.URL] = response
        assert visuals.fetch_image_bytes(self.URL) is None

    def test_too_heavy_while_streaming(self, net, monkeypatch):
        monkeypatch.setattr(visuals, "PHOTO_MAX_BYTES", 5000)
        net.routes[self.URL] = FakeResponse(200, {"Content-Type": "image/jpeg"}, b"x" * 6000)
        assert visuals.fetch_image_bytes(self.URL) is None

    def test_too_slow(self, net, monkeypatch):
        monkeypatch.setattr(visuals, "PHOTO_MAX_SECONDS", -1)
        net.routes[self.URL] = FakeResponse(200, {"Content-Type": "image/jpeg"}, b"x" * 3000)
        assert visuals.fetch_image_bytes(self.URL) is None

    def test_too_many_redirects(self, net):
        net.routes[self.URL] = FakeResponse(302, location=self.URL)
        assert visuals.fetch_image_bytes(self.URL) is None
        assert len(net.calls) == visuals.MAX_REDIRECTS + 1

    def test_octet_stream_is_accepted_then_checked_by_pillow(self, net):
        net.routes[self.URL] = FakeResponse(200, {"Content-Type": "application/octet-stream"}, b"DATA")
        assert visuals.fetch_image_bytes(self.URL) == b"DATA"


class TestNormalizePhoto:
    def test_clean_jpeg_without_metadata(self, tmp_path):
        from PIL import Image
        exif = Image.Exif()
        exif[0x010F] = "PhoneMaker"                            # marque de l'appareil
        exif[0x8825] = {2: (48, 51, 24)}                       # position GPS
        out = tmp_path / "p.jpg"
        assert visuals.normalize_photo(jpeg(exif=exif.tobytes()), out) is None
        with Image.open(out) as im:
            assert im.format == "JPEG" and im.mode == "RGB" and im.size == (1200, 675)
            assert not im.getexif() and "exif" not in im.info

    def test_transparency_goes_on_white(self, tmp_path):
        from PIL import Image
        out = tmp_path / "p.jpg"
        assert visuals.normalize_photo(jpeg(color=(255, 0, 0, 0), fmt="PNG", mode="RGBA"), out) is None
        with Image.open(out) as im:
            r, g, b = im.getpixel((10, 10))
            assert min(r, g, b) > 240

    def test_big_photos_are_reduced(self, tmp_path):
        from PIL import Image
        out = tmp_path / "p.jpg"
        assert visuals.normalize_photo(jpeg(4000, 2250), out) is None
        with Image.open(out) as im:
            assert im.size == (2048, 1152)

    def test_webp_and_first_frame_of_a_gif(self, tmp_path):
        from PIL import Image
        assert visuals.normalize_photo(jpeg(fmt="WEBP"), tmp_path / "w.jpg") is None
        frames = [Image.new("RGB", (800, 450), c) for c in ((255, 0, 0), (0, 255, 0))]
        buf = io.BytesIO()
        frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:])
        assert visuals.normalize_photo(buf.getvalue(), tmp_path / "g.jpg") is None
        with Image.open(tmp_path / "g.jpg") as im:
            r, g, b = im.getpixel((5, 5))
            assert r > 200 and g < 60                           # la 1re image, rouge

    @pytest.mark.parametrize("raw, why", [
        (b"not an image at all", "image illisible"),
        (b"", "image illisible"),
    ])
    def test_unreadable(self, tmp_path, raw, why):
        out = tmp_path / "p.jpg"
        assert why in visuals.normalize_photo(raw, out) and not out.exists()

    @pytest.mark.parametrize("size, why", [((300, 200), "trop petite"), ((1200, 300), "trop petite"),
                                           ((3000, 1000), "format inhabituel"), ((700, 1500), "format inhabituel")])
    def test_thumbnails_and_strange_formats(self, tmp_path, size, why):
        assert why in visuals.normalize_photo(jpeg(*size), tmp_path / "p.jpg")

    def test_portrait_4_5_is_fine(self, tmp_path):
        assert visuals.normalize_photo(jpeg(1080, 1350), tmp_path / "p.jpg") is None

    def test_oversized_images_are_refused_before_decoding(self, tmp_path, monkeypatch):
        monkeypatch.setattr(visuals, "PHOTO_MAX_PIXELS", 1000)
        assert "démesurée" in visuals.normalize_photo(jpeg(), tmp_path / "p.jpg")

    def test_without_pillow(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "PIL", None)
        assert visuals.normalize_photo(b"x", tmp_path / "p.jpg") == "Pillow n'est pas installé"


class TestDownloadPhoto:
    def test_downloaded_and_cleaned(self, tmp_path):
        out = tmp_path / "photos" / "p.jpg"
        assert visuals.download_photo(TC_PHOTO, out, fetch=Images({TC_PHOTO: jpeg()})) == out and out.is_file()

    def test_failures_mean_no_photo(self, tmp_path):
        out = tmp_path / "p.jpg"
        assert visuals.download_photo(TC_PHOTO, out, fetch=Images({})) is None

        def boom(url):
            raise ConnectionError("reset")
        assert visuals.download_photo(TC_PHOTO, out, fetch=boom) is None
        assert visuals.download_photo(TC_PHOTO, out, fetch=Images({TC_PHOTO: b"junk"})) is None
        assert not out.exists()


# ---------------------------------------------------------------------------
# Quelle photo pour CE post
# ---------------------------------------------------------------------------
class TestPhotoCandidates:
    def urls(self, draft):
        return [it.url for it in visuals.photo_candidates(draft)]

    def test_designated_story_first_then_the_other_cited_stories(self):
        assert self.urls(photo_draft(VERGE, cited=(TC, VERGE, BLOG))) == [VERGE, TC, BLOG]

    def test_at_most_max_pages_tried(self):
        cited = tuple(f"https://site{i}.com/story-{i}" for i in range(6))
        assert len(self.urls(photo_draft(cited[0], cited=cited))) == visuals.MAX_PAGES_TRIED

    def test_never_x_never_twice_only_cited_stories(self):
        d = photo_draft(XPOST, cited=(XPOST, TC, TC + "/", VERGE))
        assert self.urls(d) == [TC, VERGE]
        assert self.urls(photo_draft("https://elsewhere.com/a", cited=(TC,))) == [TC]
        assert self.urls(Draft(kind="news", texts=["x"], visual=Visual("photo", TC))) == []


class TestPhotoVisual:
    def run(self, settings, draft, writer, web=None, images=None, tag="news_2026-11-14"):
        return visuals._photo_visual(settings, draft, writer, tag,
                                     http_get=web or Web({TC: page(TITLES[TC], TC_PHOTO, "OpenAI", "TechCrunch")}),
                                     fetch=images or Images({TC_PHOTO: jpeg()}))

    def test_the_photo_of_the_story_is_used(self, settings):
        checker = Checker((True, "fine"))
        path, kind = self.run(settings, photo_draft(), checker)
        assert kind == "photo" and Path(path).is_file()
        assert Path(path).parent == utils.MEDIA_DIR / "photos" and Path(path).name.startswith("news_2026-11-14_")
        call = checker.calls[0]
        assert call["exists"] and call["headline"] == TITLES[TC] and call["credit"] == "OpenAI"
        assert call["post"] == "ChatGPT can now shop for you." and call["official"] is False

    def test_photos_disabled(self, settings):
        settings.photos_enabled = False
        checker, imgs = Checker(), Images({TC_PHOTO: jpeg()})
        assert self.run(settings, photo_draft(), checker, images=imgs) == (None, None)
        assert checker.calls == [] and imgs.urls == []

    def test_never_used_without_claudes_look(self, settings):
        imgs = Images({TC_PHOTO: jpeg()})
        assert self.run(settings, photo_draft(), object(), images=imgs) == (None, None)
        assert imgs.urls == []

    def test_agency_photo_skipped_without_download_then_next_story(self, settings):
        w = Web({TC: page(TITLES[TC], TC_PHOTO, "Getty Images", "TechCrunch"),
                 VERGE: page(TITLES[VERGE], VERGE_PHOTO, "Google", "The Verge")})
        imgs = Images({TC_PHOTO: jpeg(), VERGE_PHOTO: jpeg()})
        checker = Checker()
        path, kind = self.run(settings, photo_draft(TC, cited=(TC, VERGE)), checker, w, imgs)
        assert kind == "photo" and imgs.urls == [VERGE_PHOTO]
        assert [c["credit"] for c in checker.calls] == ["Google"]

    def test_outlets_own_photo_is_skipped(self, settings):
        w = Web({VERGE: page(TITLES[VERGE], VERGE_PHOTO, "Jane Doe / The Verge", "The Verge")})
        imgs = Images({VERGE_PHOTO: jpeg()})
        assert self.run(settings, photo_draft(VERGE, cited=(VERGE,)), Checker(), w, imgs) == (None, None)
        assert imgs.urls == []

    def test_official_page_is_said_to_be_official(self, settings):
        w = Web({BLOG: page(TITLES[BLOG], BLOG_PHOTO, "", "OpenAI")})
        checker = Checker()
        path, kind = self.run(settings, photo_draft(BLOG, cited=(BLOG,), official=(BLOG,)), checker, w,
                              Images({BLOG_PHOTO: jpeg(fmt="PNG")}))
        assert kind == "photo" and checker.calls[0]["official"] is True

    def test_refused_photo_is_deleted_and_the_next_story_tried(self, settings):
        w = Web({TC: page(TITLES[TC], TC_PHOTO, "OpenAI", "TechCrunch"),
                 BLOG: page(TITLES[BLOG], BLOG_PHOTO, "", "OpenAI")})
        checker = Checker((False, "a news outlet's logo"), (True, "ok"))
        path, kind = self.run(settings, photo_draft(TC, cited=(TC, BLOG)), checker, w,
                              Images({TC_PHOTO: jpeg(), BLOG_PHOTO: jpeg()}))
        assert kind == "photo" and len(checker.calls) == 2
        assert not checker.calls[0]["path"].exists() and Path(path) == checker.calls[1]["path"]

    @pytest.mark.parametrize("answer", [(False, "blurry"), ("yes", ""), (1, ""), RuntimeError("API down")])
    def test_text_only_unless_claude_clearly_says_yes(self, settings, answer):
        checker = Checker(answer)
        assert self.run(settings, photo_draft(), checker) == (None, None)
        assert not list((utils.MEDIA_DIR / "photos").glob("*.jpg"))

    def test_page_or_download_failure_moves_on(self, settings):
        w = Web({VERGE: page(TITLES[VERGE], VERGE_PHOTO, "Google", "The Verge"),
                 BLOG: page(TITLES[BLOG], BLOG_PHOTO, "", "OpenAI")})   # TC : 404
        imgs = Images({BLOG_PHOTO: jpeg()})                              # VERGE : photo introuvable
        path, kind = self.run(settings, photo_draft(TC, cited=(TC, VERGE, BLOG)), Checker(), w, imgs)
        assert kind == "photo" and w.urls == [TC, VERGE, BLOG] and imgs.urls == [VERGE_PHOTO, BLOG_PHOTO]

    def test_search_is_bounded_in_time(self, settings, monkeypatch):
        monkeypatch.setattr(visuals, "PHOTO_SEARCH_MAX_S", -1)
        w = Web({TC: page(TITLES[TC], TC_PHOTO, "OpenAI", "TechCrunch")})
        assert self.run(settings, photo_draft(), Checker(), w) == (None, None) and w.urls == []

    def test_old_photos_are_cleaned_up(self, settings, monkeypatch):
        monkeypatch.setattr(visuals, "PHOTOS_KEEP", 2)
        folder = utils.MEDIA_DIR / "photos"
        folder.mkdir(parents=True)
        for i in range(4):
            (folder / f"old{i}.jpg").write_bytes(b"x")
        self.run(settings, photo_draft(), Checker())
        assert len(list(folder.iterdir())) == 2


# ---------------------------------------------------------------------------
# Point d'entrée : prepare()
# ---------------------------------------------------------------------------
def ai_draft(prompt: str) -> Draft:
    return Draft(kind="news", texts=["the post text"], visual=Visual("ai", prompt))


class TestPrepare:
    def test_photo_of_the_story(self, settings, web, images):
        checker = Checker()
        path, kind = visuals.prepare(settings, photo_draft(), checker, "news_2026-11-14")
        assert kind == "photo" and images.urls == [TC_PHOTO] and web.urls == [TC]

    def test_old_screenshot_drafts_now_get_a_photo(self, settings, web, images):
        d = photo_draft()
        d.visual = Visual(" Shot ", TC)
        assert visuals.prepare(settings, d, Checker(), "t")[1] == "photo"

    def test_poll_has_no_image(self, settings, web, images):
        assert visuals.prepare(settings, photo_draft(poll={"options": ["yes", "no"]}), Checker(), "t") == (None, None)
        assert web.urls == []

    @pytest.mark.parametrize("draft", [
        Draft(kind="gm", texts=["gm"]),
        Draft(kind="news", texts=["x"], visual=Visual("phone", TC), sources=[item()]),   # ancien type
        Draft(kind="news", texts=["x"], visual=Visual("card", ""), sources=[item()]),
        Draft(kind="news", texts=["x"], visual=Visual("ai", "")),
    ])
    def test_nothing_to_show(self, settings, web, images, draft):
        assert visuals.prepare(settings, draft, Checker(), "t") == (None, None)
        assert web.urls == []

    def test_tag_is_sanitized(self, settings, web, images):
        path, _ = visuals.prepare(settings, photo_draft(), Checker(), "../../etc/pw d")
        assert Path(path).name.startswith("etc-pw-d_")

    def test_never_raises(self, settings, monkeypatch):
        class Exploding:
            @property
            def poll(self):
                raise RuntimeError("boom")
        assert visuals.prepare(settings, Exploding(), Checker(), "t") == (None, None)
        assert visuals.prepare(settings, object(), Checker(), "t") == (None, None)
        d = photo_draft()
        d.visual = "not a visual"
        assert visuals.prepare(settings, d, Checker(), "t") == (None, None)

        def boom(*a, **k):
            raise RuntimeError("unexpected")
        monkeypatch.setattr(visuals, "_photo_visual", boom)
        assert visuals.prepare(settings, photo_draft(), Checker(), "t") == (None, None)

    def test_tests_never_reach_the_internet(self, settings):
        """Sans les faux de ce fichier, la page n'est pas lue et la photo pas téléchargée."""
        assert visuals.prepare(settings, photo_draft(), Checker(), "t") == (None, None)
        with pytest.raises(AssertionError, match="accès réseau interdit"):
            visuals.fetch_image_bytes(TC_PHOTO)


class TestSelfTest:
    def test_success(self):
        assert visuals.self_test() == (True, "")

    def test_failure_gives_the_reason(self, monkeypatch):
        monkeypatch.setattr(visuals, "normalize_photo", lambda raw, out: "image illisible (OSError)")
        assert visuals.self_test() == (False, "image illisible (OSError)")

    def test_errors_are_reported_on_one_line(self, monkeypatch):
        def boom(raw, out):
            raise RuntimeError("first line\nsecond line")
        monkeypatch.setattr(visuals, "normalize_photo", boom)
        assert visuals.self_test() == (False, "first line")

    def test_without_pillow(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "PIL", None)
        assert visuals.self_test() == (False, "Pillow n'est pas installé")


class TestAIImages:
    def test_disabled_by_default(self, settings, monkeypatch):
        called = []
        monkeypatch.setattr(visuals, "generate_ai_image", lambda *a: called.append(a))
        settings.image_ai_enabled = False
        settings.openai_api_key = "sk-x"
        assert visuals.prepare(settings, ai_draft("a calm desk"), Checker(), "t") == (None, None)
        assert called == []

    def test_needs_key(self, settings, monkeypatch):
        called = []
        monkeypatch.setattr(visuals, "generate_ai_image", lambda *a: called.append(a))
        settings.image_ai_enabled = True
        settings.openai_api_key = ""
        assert visuals.prepare(settings, ai_draft("a calm desk"), Checker(), "t") == (None, None)
        assert called == []

    def test_generates(self, settings, monkeypatch, tmp_path):
        out = tmp_path / "img.png"
        out.write_bytes(b"png")
        seen = []
        monkeypatch.setattr(visuals, "generate_ai_image", lambda s, prompt, tag: seen.append((prompt, tag)) or out)
        settings.image_ai_enabled = True
        settings.openai_api_key = "sk-x"
        assert visuals.prepare(settings, ai_draft("a calm desk at dawn"), Checker(), "tip/1") == (str(out), "ai")
        assert seen == [("a calm desk at dawn", "tip-1")]

    def test_generation_failure_means_text_only(self, settings, monkeypatch):
        monkeypatch.setattr(visuals, "generate_ai_image", lambda *a: None)
        settings.image_ai_enabled = True
        settings.openai_api_key = "sk-x"
        assert visuals.prepare(settings, ai_draft("a calm desk"), Checker(), "t") == (None, None)

    def test_refuses_links_and_off_limits(self, settings, monkeypatch):
        called = []
        monkeypatch.setattr(visuals, "generate_ai_image", lambda *a: called.append(a))
        settings.image_ai_enabled = True
        settings.openai_api_key = "sk-x"
        assert visuals.prepare(settings, ai_draft("logo of openai.com"), Checker(), "t") == (None, None)
        assert visuals.prepare(settings, ai_draft("a glass of beer on a bar"), Checker(), "t") == (None, None)
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
# Claude regarde la photo (le contrat avec writer.photo_is_usable)
# ---------------------------------------------------------------------------
class TestPhotoCheckByClaude:
    def test_real_writer_sees_the_photo_the_story_the_post_and_the_credit(self, settings, state, web, images):
        from bot.writer import Writer
        from tests.fakes import FakeLLM
        llm = FakeLLM({"photo-check": {"ok": False, "issue": "Getty Images watermark"}})
        assert visuals.prepare(settings, photo_draft(), Writer(settings, llm, state), "t") == (None, None)
        call = llm.calls[0]
        assert len(call["images"]) == 1 and Path(call["images"][0]).suffix == ".jpg"
        assert TITLES[TC] in call["user"] and "ChatGPT can now shop for you." in call["user"]
        assert 'credit line on the page: "OpenAI"' in call["user"]

    def test_no_answer_means_no_photo(self, settings, state, web, images):
        from bot.writer import Writer
        from tests.fakes import FakeLLM
        assert visuals.prepare(settings, photo_draft(), Writer(settings, FakeLLM({}), state), "t") == (None, None)

    def test_llm_builds_an_image_block(self, tmp_path):
        from PIL import Image
        from bot.llm import _image_block
        p = tmp_path / "x.png"
        Image.new("RGB", (3200, 1800), (10, 20, 30)).save(p)
        b = _image_block(p)
        assert b["type"] == "image" and b["source"]["media_type"] == "image/jpeg" and b["source"]["data"]
        assert _image_block(tmp_path / "nonexistent.png") is None
