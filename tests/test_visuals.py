"""Visuels (bot/visuals.py) : la vidéo ou la photo de l'actu, prise À LA SOURCE.

Jamais l'image d'un média : la page officielle de l'histoire (flux officiel, ou lien de
l'article désigné par Claude) donne sa vidéo (un fichier, jamais YouTube : extrait de
30 s par ffmpeg) ou sa photo ; crédit d'agence refusé d'office ; Claude regarde avant
que ça serve (sinon : page suivante, puis texte seul). Jamais X, jamais une adresse
privée. Aucune exception vers l'appelant. Aucun accès réseau : pages, images et vidéos
sont simulées (tests/conftest.py bloque la lecture des pages et la résolution des noms) ;
les tests du découpage lancent le vrai ffmpeg sur une vidéo fabriquée sur place."""
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

# Deux articles de presse (jamais une source d'image) et les pages officielles vers lesquelles ils renvoient.
TC = "https://techcrunch.com/2026/11/13/chatgpts-new-wallet-lets-ai-agents-shop/"
VERGE = "https://www.theverge.com/2026/11/13/openai-chatgpt-wallet"
BLOG = "https://openai.example/index/introducing-chatgpt-wallet/"
META = "https://about.fb.com/news/2026/11/introducing-spaces-customize-muse-app/"
LAB = "https://www.university.example/news/robot-frog"
XPOST = "https://x.com/OpenAI/status/1846000000000000000"
TC_PHOTO = "https://techcrunch.com/wp-content/uploads/2026/11/chatgpt-wallet.jpg?w=1200"
VERGE_PHOTO = "https://platform.theverge.com/wp-content/uploads/2026/11/wallet.jpg"
BLOG_PHOTO = "https://images.openai.example/wallet-hero.png"
META_PHOTO = "https://about.fb.com/wp-content/uploads/2026/11/Spaces_Social-Share.png"
META_VIDEO = "https://about.fb.com/wp-content/uploads/2026/11/Spaces_Header.mp4?_=1"
LAB_PHOTO = "https://www.university.example/media/frog.jpg"
TITLES = {TC: "ChatGPT's new Wallet lets AI agents shop and check out for you",
          VERGE: "ChatGPT can now pay for things for you",
          BLOG: "Introducing ChatGPT Wallet",
          META: "Introducing Spaces: customize the Muse app",
          LAB: "A robot frog swims with no motor"}


def page(title: str, photo: str = "", credit: str = "", site: str = "", extra_head: str = "",
         body_before: str = "", links=(), video: str = "") -> str:
    """Une page : l'en-tête (photo déclarée pour le partage), puis le titre, la photo
    d'ouverture et sa légende de crédit, la vidéo, puis le texte et ses liens."""
    head = f"<title>{title}</title>"
    if photo:
        head += f'<meta property="og:image" content="{photo}">'
    if site:
        head += f'<meta property="og:site_name" content="{site}">'
    head += extra_head
    fig = (f'<figure><img src="{photo}"><figcaption><strong>Image Credits:</strong>{credit}</figcaption>'
           "</figure>") if credit else ""
    vid = f'<video autoplay muted><source src="{video}" type="video/mp4"></video>' if video else ""
    para = "".join(f'<p>Read <a href="{u}">{t}</a> for more.</p>' for u, t in links)
    return (f"<!doctype html><html><head>{head}</head><body><nav>Home · AI · Apps</nav>{body_before}"
            f"<article><h1>{title}</h1>{fig}{vid}<p>A real article about something new, written for everyone. "
            f"It explains what changed and who can use it today.</p>{para}</article>"
            "<footer><p><a href='https://www.ziffdavis.com/privacy'>Terms</a></p></footer></body></html>")


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


class Videos:
    """Remplace le téléchargement des vidéos : le contenu d'un fichier par adresse."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = dict(files or {})
        self.urls: list[str] = []

    def __call__(self, url: str, dest: Path) -> bool:
        self.urls.append(url)
        if url not in self.files:
            return False
        Path(dest).write_bytes(self.files[url])
        return True


class Checker:
    """Ce que Claude décide : le lien de la source officielle, puis le regard sur la photo
    et sur l'extrait vidéo (writer.pick_official_link / photo_is_usable / video_is_usable)."""

    def __init__(self, *answers, video=(True, "fine"), official=(BLOG, META, LAB)) -> None:
        self.answers = list(answers) or [(True, "fine")]
        self.video_answer = video
        self.official = set(official)
        self.calls: list[dict] = []
        self.video_calls: list[dict] = []
        self.picks: list[list[str]] = []

    def pick_official_link(self, headline, post, links):
        self.picks.append([u for u, _ in links])
        return next((i for i, (u, _) in enumerate(links, start=1) if u in self.official), 0)

    def photo_is_usable(self, path, headline: str = "", post: str = "", credit: str = "",
                        official: bool = True):
        self.calls.append({"path": Path(path), "exists": Path(path).is_file(), "headline": headline,
                           "post": post, "credit": credit, "official": official})
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def video_is_usable(self, frames, headline: str = "", post: str = "", credit: str = "",
                        official: bool = True):
        self.video_calls.append({"frames": [Path(f) for f in frames], "exist": all(Path(f).is_file() for f in frames),
                                 "headline": headline, "post": post, "credit": credit})
        if isinstance(self.video_answer, Exception):
            raise self.video_answer
        return self.video_answer


def pages_world() -> dict[str, str]:
    """Le web simulé : TC et The Verge renvoient vers la page officielle d'OpenAI."""
    return {TC: page(TITLES[TC], TC_PHOTO, "OpenAI", "TechCrunch",
                     links=[("https://x.com/OpenAI/status/1", "said on X"),
                            ("https://www.businessinsider.com/ai-agents-2026-11", "blocking AI agents"),
                            (BLOG, "launched a new wallet feature")]),
            VERGE: page(TITLES[VERGE], VERGE_PHOTO, "Jane Doe / The Verge", "The Verge",
                        links=[(BLOG, "announced today")]),
            BLOG: page(TITLES[BLOG], BLOG_PHOTO, "", "OpenAI"),
            META: page(TITLES[META], META_PHOTO, "", "Meta Newsroom", video=META_VIDEO),
            LAB: page(TITLES[LAB], LAB_PHOTO, "Cecilie Ryberg / Aarhus University", "Aarhus University")}


@pytest.fixture
def web(monkeypatch):
    from bot import sources
    w = Web(pages_world())
    monkeypatch.setattr(sources, "default_http_get", w)
    return w


@pytest.fixture
def images(monkeypatch):
    imgs = Images({TC_PHOTO: jpeg(), VERGE_PHOTO: jpeg(), BLOG_PHOTO: jpeg(fmt="PNG"), META_PHOTO: jpeg(),
                   LAB_PHOTO: jpeg()})
    monkeypatch.setattr(visuals, "fetch_image_bytes", imgs)
    return imgs


FFMPEG = visuals.ffmpeg_exe()
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg absent (paquet imageio-ffmpeg)")


def make_video(path: Path, seconds: float = 3, size: str = "640x360", audio: bool = True) -> Path:
    """Une vraie petite vidéo, fabriquée sur place par ffmpeg (aucun réseau)."""
    import subprocess
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
    cmd += ["-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac"] if audio else []
    subprocess.run(cmd + [str(path)], check=True, capture_output=True, timeout=120)
    return path


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


class TestSourceUrls:
    @pytest.mark.parametrize("url", [BLOG, META, LAB, "https://www.eurekalert.org/news-releases/1151530",
                                     "https://blog.google/products/gemini/a", "https://store.google.com/p"])
    def test_official_pages_can_be_sources(self, url):
        assert visuals.is_source_url(url)

    @pytest.mark.parametrize("url", [TC, VERGE, "https://www.businessinsider.com/a", "https://news.sky.com/a",
                                     "https://x.com/a/status/1", "https://t.co/xyz", "https://www.youtube.com/watch?v=1",
                                     "https://youtu.be/abc", "https://doi.org/10.1002/x", "https://www.nature.com/a",
                                     "https://www.amazon.com/dp/1", "https://en.wikipedia.org/wiki/A",
                                     "https://www.instagram.com/p/1", "https://bit.ly/abc", "ftp://site.com/a", ""])
    def test_never_a_news_site_social_network_journal_or_shop(self, url):
        assert not visuals.is_source_url(url)


class TestArticleLinks:
    def test_only_links_written_in_the_article_that_could_be_the_source(self):
        html = pages_world()[TC]
        assert visuals.article_links(html, TC) == [(BLOG, "launched a new wallet feature")]

    def test_relative_same_site_duplicate_and_fragment_links(self):
        html = ("<article><p><a href='/2026/11/other-story/'>our story</a> "
                "<a href='https://openai.example/index/a#top'>blog post</a> "
                "<a href='https://openai.example/index/a'>again</a> "
                "<a href='mailto:tips@techcrunch.com'>tips</a> <a>no address</a></p>"
                "<nav><p><a href='https://partner.example/'>menu</a></p></nav></article>")
        assert visuals.article_links(html, TC) == [("https://openai.example/index/a", "blog post")]

    def test_links_outside_paragraphs_are_ignored(self):
        html = "<ul><li><a href='https://openai.example/a'>menu</a></li></ul><div><a href='https://b.example/'>x</a></div>"
        assert visuals.article_links(html, TC) == []

    def test_at_most_max_links(self):
        html = "<p>" + "".join(f"<a href='https://site{i}.example/a'>s{i}</a>" for i in range(50)) + "</p>"
        assert len(visuals.article_links(html, TC)) == visuals.MAX_LINKS

    def test_broken_html(self):
        assert visuals.article_links("<p><a href='https://a.example/x'>unclosed", TC) == []
        assert visuals.article_links("", TC) == []


class TestReadPage:
    def test_page_or_nothing_never_x(self):
        w = Web({TC: "<html>ok</html>", XPOST: "<html>x</html>"})
        assert visuals.read_page(TC, http_get=w) == "<html>ok</html>"
        assert visuals.read_page(XPOST, http_get=w) == "" and visuals.read_page("nope", http_get=w) == ""
        assert visuals.read_page(VERGE, http_get=w) == ""                     # 404
        assert w.urls == [TC, VERGE]

        def boom(url):
            raise TimeoutError("slow")
        assert visuals.read_page(TC, http_get=boom) == ""

    def test_reads_pages_with_the_article_reader_by_default(self, web):
        assert visuals.read_page(BLOG).startswith("<!doctype html>") and web.urls == [BLOG]


class TestSourcePages:
    def test_press_article_leads_to_the_official_page_never_its_own(self):
        w, checker = Web(pages_world()), Checker()
        pages = visuals.source_pages(photo_draft(), checker, http_get=w, post="the post")
        assert [p.url for p in pages] == [BLOG]
        assert checker.picks == [[BLOG]]                    # X et Business Insider ne sont pas proposés
        assert w.urls == [TC, BLOG]
        assert pages[0].title == TITLES[TC] and pages[0].site == "OpenAI" and pages[0].meta["og:image"] == BLOG_PHOTO

    def test_official_feed_item_is_its_own_source(self):
        w, checker = Web(pages_world()), Checker()
        pages = visuals.source_pages(photo_draft(META, cited=(META,), official=(META,)), checker, http_get=w)
        assert [p.url for p in pages] == [META] and checker.picks == [] and w.urls == [META]

    def test_the_same_official_page_once(self):
        w = Web(pages_world())
        pages = visuals.source_pages(photo_draft(TC, cited=(TC, VERGE)), Checker(), http_get=w)
        assert [p.url for p in pages] == [BLOG] and w.urls == [TC, BLOG, VERGE]

    def test_no_official_link_or_no_answer(self):
        w = Web(pages_world())
        assert visuals.source_pages(photo_draft(), Checker(official=()), http_get=w) == []
        assert visuals.source_pages(photo_draft(), object(), http_get=w) == []           # pas de Claude
        assert w.urls == [TC, TC]

    def test_a_news_site_is_never_taken_as_the_source_even_if_designated(self):
        world = pages_world()
        world[TC] = page(TITLES[TC], TC_PHOTO, links=[(BLOG, "blog")])
        w = Web(world)

        class Liar(Checker):
            def pick_official_link(self, headline, post, links):
                return 1
        assert [p.url for p in visuals.source_pages(photo_draft(), Liar(), http_get=w)] == [BLOG]
        # …et un flux « officiel » mal réglé qui pointe vers un média n'est pas une source non plus.
        assert visuals.source_pages(photo_draft(VERGE, cited=(VERGE,), official=(VERGE,)), Checker(official=()),
                                    http_get=Web(pages_world())) == []

    def test_picker_failures(self):
        w = Web(pages_world())

        class Broken(Checker):
            def pick_official_link(self, headline, post, links):
                raise RuntimeError("down")
        assert visuals.source_pages(photo_draft(), Broken(), http_get=w) == []

        class OutOfRange(Checker):
            def pick_official_link(self, headline, post, links):
                return 7
        assert visuals.source_pages(photo_draft(), OutOfRange(), http_get=w) == []

    def test_unreadable_pages(self):
        world = pages_world()
        del world[BLOG]
        assert visuals.source_pages(photo_draft(), Checker(), http_get=Web(world)) == []
        assert visuals.source_pages(photo_draft(), Checker(), http_get=Web({})) == []

    def test_bounded_in_time(self):
        w = Web(pages_world())
        assert visuals.source_pages(photo_draft(), Checker(), http_get=w, deadline=0) == [] and w.urls == []


class TestLeadVideo:
    @pytest.mark.parametrize("head, body, found", [
        ('<meta property="og:video" content="https://cdn.example/v.mp4">', "", "https://cdn.example/v.mp4"),
        ('<meta property="og:video:secure_url" content="https://cdn.example/s.mp4">'
         '<meta property="og:video" content="http://cdn.example/s.mp4">', "", "https://cdn.example/s.mp4"),
        ('<meta property="og:video" content="https://cdn.example/play?id=1">'
         '<meta property="og:video:type" content="video/mp4">', "", "https://cdn.example/play?id=1"),
        ('<meta name="twitter:player:stream" content="https://cdn.example/t.mp4">', "", "https://cdn.example/t.mp4"),
        ("", '<video src="/media/hero.webm" autoplay></video>', "https://www.example.org/media/hero.webm"),
        ("", '<video><source src="https://cdn.example/a.mov?x=1" type="video/quicktime"></video>',
         "https://cdn.example/a.mov?x=1"),
        ("", '<script type="application/ld+json">{"contentUrl": "https:\\/\\/cdn.example\\/ld.mp4"}</script>',
         "https://cdn.example/ld.mp4"),
    ])
    def test_video_files_published_by_the_page(self, head, body, found):
        html = f"<html><head>{head}</head><body>{body}</body></html>"
        assert visuals.lead_video_url(visuals.page_meta(html), html, "https://www.example.org/news/a") == found

    @pytest.mark.parametrize("head, body", [
        ('<meta property="og:video" content="https://www.youtube.com/embed/r8sv2JxnUKw">', ""),
        ('<meta property="og:video:url" content="https://player.vimeo.com/video/1">', ""),
        ("", '<video src="https://cdn.example/live/index.m3u8"></video>'),
        ("", '<video><source src="https://cdn.example/manifest.mpd" type="application/dash+xml"></video>'),
        ("", '<video src="https://cdn.ex.co/transformations/ad.mp4"></video>'),
        ("", '<video src="https://video.twimg.com/ext_tw_video/1/a.mp4"></video>'),
        ("", '<iframe src="https://www.youtube.com/embed/r8sv2JxnUKw"></iframe>'),
        ("", "<p>no video here</p>"),
    ])
    def test_never_youtube_streams_ads_or_x(self, head, body):
        html = f"<html><head>{head}</head><body>{body}</body></html>"
        assert visuals.lead_video_url(visuals.page_meta(html), html, "https://www.example.org/news/a") == ""


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
        assert stream and not allow_redirects and timeout in (visuals.PHOTO_TIMEOUT_S, visuals.VIDEO_TIMEOUT_S)
        assert ("image/" in headers["Accept"]) == (timeout == visuals.PHOTO_TIMEOUT_S)
        assert ("video/" in headers["Accept"]) == (timeout == visuals.VIDEO_TIMEOUT_S)
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
class TestStoryCandidates:
    def urls(self, draft):
        return [it.url for it in visuals.story_candidates(draft)]

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


class TestStoryMedia:
    def run(self, settings, draft, checker, web=None, images=None, videos=None, tag="news_2026-11-14"):
        return visuals._story_media(settings, draft, checker, tag, http_get=web or Web(pages_world()),
                                    fetch=images or Images({BLOG_PHOTO: jpeg(fmt="PNG"), META_PHOTO: jpeg(),
                                                            LAB_PHOTO: jpeg(), TC_PHOTO: jpeg()}),
                                    fetch_video=videos or Videos())

    def test_the_photo_comes_from_the_official_page_never_from_the_article(self, settings):
        imgs, checker = Images({TC_PHOTO: jpeg(), BLOG_PHOTO: jpeg(fmt="PNG")}), Checker()
        path, kind = self.run(settings, photo_draft(), checker, images=imgs)
        assert kind == "photo" and Path(path).is_file() and imgs.urls == [BLOG_PHOTO]
        assert Path(path).parent == utils.MEDIA_DIR / "photos" and Path(path).name.startswith("news_2026-11-14_")
        call = checker.calls[0]
        assert call["exists"] and call["headline"] == TITLES[TC] and call["credit"] == "" and call["official"]
        assert call["post"] == "ChatGPT can now shop for you."

    def test_text_only_when_the_article_leads_to_no_official_page(self, settings):
        imgs = Images({TC_PHOTO: jpeg()})
        assert self.run(settings, photo_draft(), Checker(official=()), images=imgs) == (None, None)
        assert imgs.urls == []                              # la photo de l'article n'est jamais prise

    def test_university_photo_with_its_own_credit(self, settings):
        checker = Checker()
        path, kind = self.run(settings, photo_draft(LAB, cited=(LAB,), official=(LAB,)), checker)
        assert kind == "photo" and checker.calls[0]["credit"] == "Cecilie Ryberg / Aarhus University"

    def test_agency_photo_on_the_official_page_is_refused_without_download(self, settings):
        world = pages_world()
        world[BLOG] = page(TITLES[BLOG], BLOG_PHOTO, "Jane Doe/Getty Images", "OpenAI")
        imgs, checker = Images({BLOG_PHOTO: jpeg()}), Checker()
        assert self.run(settings, photo_draft(), checker, web=Web(world), images=imgs) == (None, None)
        assert imgs.urls == [] and checker.calls == []

    def test_photos_disabled(self, settings):
        settings.photos_enabled = settings.videos_enabled = False
        w = Web(pages_world())
        assert self.run(settings, photo_draft(), Checker(), web=w) == (None, None) and w.urls == []

    def test_never_used_without_claudes_look(self, settings):
        imgs = Images({BLOG_PHOTO: jpeg()})
        assert self.run(settings, photo_draft(), object(), images=imgs) == (None, None)
        assert imgs.urls == []

    @pytest.mark.parametrize("answer", [(False, "blurry"), ("yes", ""), (1, ""), RuntimeError("API down")])
    def test_text_only_unless_claude_clearly_says_yes(self, settings, answer):
        assert self.run(settings, photo_draft(), Checker(answer)) == (None, None)
        assert not list((utils.MEDIA_DIR / "photos").glob("*.jpg"))

    def test_refused_photo_is_deleted_and_the_next_official_page_tried(self, settings):
        checker = Checker((False, "a logo only"), (True, "ok"))
        draft = photo_draft(TC, cited=(TC, LAB), official=(LAB,))
        path, kind = self.run(settings, draft, checker)
        assert kind == "photo" and len(checker.calls) == 2
        assert not checker.calls[0]["path"].exists() and Path(path) == checker.calls[1]["path"]

    def test_search_is_bounded_in_time(self, settings, monkeypatch):
        monkeypatch.setattr(visuals, "MEDIA_SEARCH_MAX_S", -1)
        w = Web(pages_world())
        assert self.run(settings, photo_draft(), Checker(), web=w) == (None, None) and w.urls == []

    def test_old_photos_are_cleaned_up(self, settings, monkeypatch):
        monkeypatch.setattr(visuals, "PHOTOS_KEEP", 2)
        folder = utils.MEDIA_DIR / "photos"
        folder.mkdir(parents=True)
        for i in range(4):
            (folder / f"old{i}.jpg").write_bytes(b"x")
        self.run(settings, photo_draft(), Checker())
        assert len(list(folder.iterdir())) == 2

    # --- La vidéo officielle passe avant la photo -------------------------------------
    @needs_ffmpeg
    def test_official_video_clip_comes_first(self, settings, tmp_path):
        vids = Videos({META_VIDEO: make_video(tmp_path / "src.mp4", seconds=4).read_bytes()})
        checker = Checker()
        path, kind = self.run(settings, photo_draft(META, cited=(META,), official=(META,)), checker, videos=vids)
        assert kind == "video" and Path(path).suffix == ".mp4" and Path(path).parent == utils.MEDIA_DIR / "videos"
        assert vids.urls == [META_VIDEO] and checker.calls == []           # la photo n'a pas servi
        call = checker.video_calls[0]
        assert len(call["frames"]) == 3 and call["exist"] and call["headline"] == TITLES[META]
        info = visuals.probe_video(FFMPEG, Path(path))
        assert 3.5 <= info.duration <= 4.5 and info.audio
        assert not list((utils.MEDIA_DIR / "videos").glob("xbot-video-*"))  # dossier de travail effacé

    @needs_ffmpeg
    def test_refused_video_falls_back_to_the_photo(self, settings, tmp_path):
        vids = Videos({META_VIDEO: make_video(tmp_path / "src.mp4").read_bytes()})
        checker = Checker(video=(False, "a TV news report"))
        path, kind = self.run(settings, photo_draft(META, cited=(META,), official=(META,)), checker, videos=vids)
        assert kind == "photo" and len(checker.video_calls) == 1
        assert not list((utils.MEDIA_DIR / "videos").glob("*.mp4"))

    def test_video_download_failure_or_unreadable_file_falls_back_to_the_photo(self, settings):
        for vids in (Videos(), Videos({META_VIDEO: b"not a video at all"})):
            checker = Checker()
            path, kind = self.run(settings, photo_draft(META, cited=(META,), official=(META,)), checker,
                                  videos=vids)
            assert kind == "photo" and checker.video_calls == []

    def test_videos_disabled_or_without_ffmpeg(self, settings, monkeypatch):
        vids = Videos({META_VIDEO: b"x"})
        settings.videos_enabled = False
        assert self.run(settings, photo_draft(META, cited=(META,), official=(META,)), Checker(),
                        videos=vids)[1] == "photo"
        settings.videos_enabled = True
        monkeypatch.setattr(visuals, "ffmpeg_exe", lambda: None)
        assert self.run(settings, photo_draft(META, cited=(META,), official=(META,)), Checker(),
                        videos=vids)[1] == "photo"
        assert vids.urls == []

    def test_agency_credit_also_blocks_the_video(self, settings):
        world = pages_world()
        world[META] = page(TITLES[META], META_PHOTO, "Reuters", "Meta Newsroom", video=META_VIDEO)
        vids = Videos({META_VIDEO: b"x"})
        assert self.run(settings, photo_draft(META, cited=(META,), official=(META,)), Checker(), web=Web(world),
                        videos=vids) == (None, None)
        assert vids.urls == []


# ---------------------------------------------------------------------------
# Point d'entrée : prepare()
# ---------------------------------------------------------------------------
def ai_draft(prompt: str) -> Draft:
    return Draft(kind="news", texts=["the post text"], visual=Visual("ai", prompt))


class TestPrepare:
    def test_photo_of_the_story_taken_at_its_source(self, settings, web, images, monkeypatch):
        monkeypatch.setattr(visuals, "fetch_video_file", Videos())
        checker = Checker()
        path, kind = visuals.prepare(settings, photo_draft(), checker, "news_2026-11-14")
        assert kind == "photo" and images.urls == [BLOG_PHOTO] and web.urls == [TC, BLOG]

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
        monkeypatch.setattr(visuals, "_story_media", boom)
        assert visuals.prepare(settings, photo_draft(), Checker(), "t") == (None, None)

    def test_tests_never_reach_the_internet(self, settings, tmp_path):
        """Sans les faux de ce fichier, la page n'est pas lue, ni la photo ni la vidéo téléchargées."""
        assert visuals.prepare(settings, photo_draft(), Checker(), "t") == (None, None)
        with pytest.raises(AssertionError, match="accès réseau interdit"):
            visuals.fetch_image_bytes(TC_PHOTO)
        assert visuals.fetch_video_file(META_VIDEO, tmp_path / "v.mp4") is False      # coupé net, rien laissé
        assert not (tmp_path / "v.mp4").exists()


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

    @needs_ffmpeg
    def test_video_success(self):
        assert visuals.video_self_test() == (True, "")

    def test_video_without_ffmpeg(self, monkeypatch):
        monkeypatch.setattr(visuals, "ffmpeg_exe", lambda: None)
        assert visuals.video_self_test() == (False, "ffmpeg absent (paquet imageio-ffmpeg)")

    @needs_ffmpeg
    def test_video_failure_gives_the_reason(self, monkeypatch):
        monkeypatch.setattr(visuals, "make_clip", lambda src, out, exe=None: "découpage impossible (x)")
        assert visuals.video_self_test() == (False, "découpage impossible (x)")


# ---------------------------------------------------------------------------
# L'extrait vidéo : téléchargement (requests simulé) et découpage (vrai ffmpeg)
# ---------------------------------------------------------------------------
class TestFetchVideo:
    URL = "https://cdn.example.com/v.mp4"

    def test_a_video_file_is_streamed_to_disk(self, net, tmp_path):
        net.routes[self.URL] = FakeResponse(200, {"Content-Type": "video/mp4"}, b"MP4DATA" * 1000)
        dest = tmp_path / "v.mp4"
        assert visuals.fetch_video_file(self.URL, dest) and dest.read_bytes() == b"MP4DATA" * 1000

    @pytest.mark.parametrize("response", [
        FakeResponse(200, {"Content-Type": "text/html"}, b"<html>"),
        FakeResponse(200, {"Content-Type": "image/jpeg"}, b"jpeg"),
        FakeResponse(200, {"Content-Type": "video/mp4", "Content-Length": str(300 * 2**20)}, b"x"),
        FakeResponse(404),
        FakeResponse(200, {"Content-Type": "video/mp4"}, b""),
    ])
    def test_refused_and_nothing_left_behind(self, net, tmp_path, response):
        net.routes[self.URL] = response
        dest = tmp_path / "v.mp4"
        assert not visuals.fetch_video_file(self.URL, dest) and not dest.exists()

    def test_never_from_x_nor_a_private_address(self, net, tmp_path):
        for target in ("https://video.twimg.com/a.mp4", "https://internal.example.com/a.mp4"):
            assert not visuals.fetch_video_file(target, tmp_path / "v.mp4")
        assert net.calls == []

    def test_network_failure_is_a_no_never_an_error(self, net, tmp_path, monkeypatch):
        import requests

        def cut(*a, **k):
            raise requests.ConnectionError("reset")
        monkeypatch.setattr(requests, "get", cut)
        dest = tmp_path / "v.mp4"
        assert visuals.fetch_video_file(self.URL, dest) is False and not dest.exists()

    def test_too_heavy_while_streaming(self, net, tmp_path, monkeypatch):
        monkeypatch.setattr(visuals, "VIDEO_MAX_BYTES", 5000)
        net.routes[self.URL] = FakeResponse(200, {"Content-Type": "video/mp4"}, b"x" * 6000)
        dest = tmp_path / "v.mp4"
        assert not visuals.fetch_video_file(self.URL, dest) and not dest.exists()


@needs_ffmpeg
class TestClip:
    def test_first_30_seconds_in_a_format_x_plays_everywhere(self, tmp_path, monkeypatch):
        monkeypatch.setattr(visuals, "CLIP_MAX_S", 2)
        src = make_video(tmp_path / "src.mp4", seconds=5, size="1920x1080")
        out = tmp_path / "clip.mp4"
        assert visuals.make_clip(src, out) is None
        info = visuals.probe_video(FFMPEG, out)
        assert 1.8 <= info.duration <= 2.2 and (info.width, info.height) == (1280, 720) and info.audio
        import subprocess
        err = subprocess.run([FFMPEG, "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
        assert "h264 (High)" in err and "yuv420p" in err and "aac" in err
        assert out.read_bytes()[:64].find(b"ftyp") >= 0 and b"moov" in out.read_bytes()[:4096]   # lecture immédiate

    def test_portrait_video_keeps_its_shape_and_silent_video_stays_silent(self, tmp_path):
        src = make_video(tmp_path / "src.mp4", seconds=3, size="720x1280", audio=False)
        out = tmp_path / "clip.mp4"
        assert visuals.make_clip(src, out) is None
        info = visuals.probe_video(FFMPEG, out)
        assert (info.width, info.height) == (720, 1280) and not info.audio

    @pytest.mark.parametrize("seconds, size, why", [(1, "640x360", "trop courte"), (3, "1600x320", "format")])
    def test_too_short_or_strange_shape(self, tmp_path, seconds, size, why):
        src = make_video(tmp_path / "src.mp4", seconds=seconds, size=size)
        out = tmp_path / "clip.mp4"
        assert why in visuals.make_clip(src, out) and not out.exists()

    def test_unreadable_file(self, tmp_path):
        src = tmp_path / "src.mp4"
        src.write_bytes(b"not a video")
        assert visuals.make_clip(src, tmp_path / "clip.mp4") == "fichier vidéo illisible"
        assert visuals.probe_video(FFMPEG, src) is None

    def test_three_frames_for_claude(self, tmp_path):
        clip = make_video(tmp_path / "clip.mp4", seconds=4)
        frames = visuals.clip_frames(clip, tmp_path)
        assert [f.name for f in frames] == ["frame0.jpg", "frame1.jpg", "frame2.jpg"]
        from PIL import Image
        with Image.open(frames[1]) as im:
            assert im.width == 768
        assert visuals.clip_frames(tmp_path / "missing.mp4", tmp_path) == []

    def test_ffmpeg_never_reads_the_network(self, tmp_path, monkeypatch):
        """Le fichier téléchargé est lu tel quel : une liste de lecture qui pointerait vers
        Internet ou vers le serveur lui-même n'est jamais suivie."""
        playlist = tmp_path / "src.mp4"
        playlist.write_text("#EXTM3U\n#EXTINF:10,\nhttp://169.254.169.254/latest/meta-data\n")
        assert visuals.make_clip(playlist, tmp_path / "clip.mp4") is not None


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
    def test_real_writer_finds_the_source_then_looks_at_its_photo(self, settings, state, web, images,
                                                                  monkeypatch):
        from bot.writer import Writer
        from tests.fakes import FakeLLM
        monkeypatch.setattr(visuals, "fetch_video_file", Videos())
        llm = FakeLLM({"source-link": {"pick": 1, "reason": "billet de blog d'OpenAI"},
                       "photo-check": {"ok": False, "issue": "Getty Images watermark"}})
        assert visuals.prepare(settings, photo_draft(), Writer(settings, llm, state), "t") == (None, None)
        assert llm.labels() == ["source-link", "photo-check"]
        link_call, photo_call = llm.calls
        assert f"[1] launched a new wallet feature — {BLOG}" in link_call["user"]
        assert "x.com" not in link_call["user"] and "businessinsider" not in link_call["user"]
        assert len(photo_call["images"]) == 1 and Path(photo_call["images"][0]).suffix == ".jpg"
        assert TITLES[TC] in photo_call["user"] and "ChatGPT can now shop for you." in photo_call["user"]
        assert images.urls == [BLOG_PHOTO]

    def test_no_answer_means_no_photo(self, settings, state, web, images):
        from bot.writer import Writer
        from tests.fakes import FakeLLM
        assert visuals.prepare(settings, photo_draft(), Writer(settings, FakeLLM({}), state), "t") == (None, None)
        assert images.urls == []

    def test_llm_builds_an_image_block(self, tmp_path):
        from PIL import Image
        from bot.llm import _image_block
        p = tmp_path / "x.png"
        Image.new("RGB", (3200, 1800), (10, 20, 30)).save(p)
        b = _image_block(p)
        assert b["type"] == "image" and b["source"]["media_type"] == "image/jpeg" and b["source"]["data"]
        assert _image_block(tmp_path / "nonexistent.png") is None
