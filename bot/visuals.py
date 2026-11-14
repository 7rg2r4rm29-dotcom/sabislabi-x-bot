"""Image des posts : la vidéo ou la photo de l'actu, prise À LA SOURCE.

Le compte est la référence : il donne la nouvelle lui-même, comme un compte d'alertes
info. Son image vient donc de la page officielle de l'entreprise, du labo ou de
l'organisation qui annonce la nouvelle — JAMAIS de la page d'un média (ni sa photo, ni
une capture de son article), jamais de X, jamais un lien dans le post.

Comment (`prepare` → `_story_media`) :
  1. Les pages officielles de l'histoire (`source_pages`) : pour chaque actu citée
     (celle que Claude a désignée, puis les autres), soit l'actu vient d'un flux
     officiel (blog de Google, newsroom de Meta…) et c'est sa page ; soit c'est un
     article de presse, et on y relève les liens du texte (`article_links`) : Claude
     désigne celui qui mène à la page officielle de l'organisation (communiqué, billet
     de blog, page produit — `writer.pick_official_link`). Les sites de presse, les
     réseaux sociaux, les revues scientifiques et les boutiques ne comptent jamais
     comme source (`is_source_url`).
  2. La vidéo d'abord : si la page officielle publie un FICHIER vidéo (pas YouTube,
     pas un flux en direct), il est téléchargé (200 Mo et 3 min au plus), un extrait
     de 30 s en est tiré (ffmpeg, MP4 H.264/AAC, 1280 px au plus) et Claude en regarde
     trois images (`writer.video_is_usable`).
  3. Sinon la photo : celle que la page officielle déclare (og:image), avec sa ligne
     de crédit ; crédit d'agence ou de banque d'images → refusée d'office. Téléchargée
     (10 Mo, 30 s), remise en JPEG propre sans métadonnées, regardée par Claude
     (`writer.photo_is_usable`).
  4. Rien de sûr : le post part en texte seul.
Tous les téléchargements suivent les redirections une à une : jamais vers X, jamais
vers une adresse privée du serveur.
Photo IA (OpenAI) : facultative, désactivée par défaut.
`prepare()` ne lève jamais d'exception : (None, None) = post en texte seul.
"""
from __future__ import annotations

import base64
import io
import ipaddress
import logging
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import BinaryIO, Callable
from urllib.parse import unquote, urljoin, urlparse

from . import utils
from .filters import off_limits_term
from .x_api import contains_url

log = logging.getLogger("xbot.visuals")

PHOTO_KIND = "photo"
VIDEO_KIND = "video"
PHOTOS_KEEP = 60
VIDEOS_KEEP = 20
GENERATED_KEEP = 50
MAX_PAGES_TRIED = 3               # actus citées dont on cherche la source officielle
MEDIA_SEARCH_MAX_S = 420          # toute la recherche (pages, vidéo, photo) : au-delà, texte seul
MAX_REDIRECTS = 5
# Photo
PHOTO_MAX_BYTES = 10 * 1024 * 1024
PHOTO_TIMEOUT_S = 15              # par requête
PHOTO_MAX_SECONDS = 30            # pour tout le téléchargement d'une photo
PHOTO_MIN_WIDTH = 600             # px : en dessous, c'est une vignette
PHOTO_MIN_HEIGHT = 315
PHOTO_MIN_RATIO = 0.5             # largeur / hauteur : ni colonne…
PHOTO_MAX_RATIO = 2.4             # … ni bandeau
PHOTO_MAX_SIDE = 2048             # px : largement assez pour X, et le fichier reste léger
PHOTO_MAX_PIXELS = 60_000_000     # au-delà, image piégée ou inutilisable
JPEG_QUALITY = 90
PHOTO_ACCEPT = "image/webp,image/png,image/jpeg,image/gif,image/*;q=0.8"
# Vidéo
VIDEO_MAX_BYTES = 200 * 1024 * 1024
VIDEO_TIMEOUT_S = 20
VIDEO_MAX_SECONDS = 180
VIDEO_ACCEPT = "video/mp4,video/webm,video/quicktime,video/*;q=0.8"
CLIP_MAX_S = 30                   # l'extrait publié : les 30 premières secondes
CLIP_MIN_S = 2.0
CLIP_MAX_SIDE = 1280
CLIP_MAX_BYTES = 60 * 1024 * 1024
CLIP_MIN_RATIO = 1 / 3            # ce qu'X accepte
CLIP_MAX_RATIO = 3.0
FFMPEG_TIMEOUT_S = 240
CLIP_FRAMES = (0.15, 0.5, 0.85)   # où Claude regarde l'extrait
# Pages
HEAD_MAX_CHARS = 400_000          # les balises <meta> sont dans l'en-tête de la page
CREDIT_BEFORE_TITLE = 2500        # la ligne de crédit de la photo d'ouverture est près du titre…
CREDIT_AFTER_TITLE = 6000         # … juste avant (photo au-dessus du titre) ou juste après
MAX_LINKS = 30                    # liens d'un article montrés à Claude pour trouver la source

# Jamais lus, jamais téléchargés : ni une page ni une image de X.
X_DOMAINS = ("x.com", "twitter.com", "t.co", "twimg.com")

# Sites de presse : jamais une source d'image. Leur propre nom dans un crédit (« Photo by
# Jane Doe / The Verge ») veut dire une photo de leur équipe.
PRESS_SITES = (
    "techcrunch.com", "theverge.com", "arstechnica.com", "wired.com", "reuters.com", "bloomberg.com",
    "ft.com", "cnbc.com", "apnews.com", "forbes.com", "businessinsider.com", "zdnet.com", "cnet.com",
    "engadget.com", "theguardian.com", "technologyreview.com", "tomsguide.com", "techradar.com",
    "mashable.com", "9to5mac.com", "9to5google.com", "macrumors.com", "androidauthority.com",
    "venturebeat.com", "axios.com", "fastcompany.com", "time.com", "npr.org", "pcmag.com", "lifehacker.com",
    "howtogeek.com", "bbc.com", "bbc.co.uk", "newatlas.com", "popsci.com", "sciencealert.com", "dexerto.com",
    "uniladtech.com", "futurism.com", "livescience.com", "singularityhub.com", "zmescience.com", "space.com",
    "independent.co.uk", "nbcnews.com", "ign.com", "gamesradar.com", "uploadvr.com", "spectrum.ieee.org",
    "humanoidsdaily.com", "interestingengineering.com", "electrek.co", "dronedj.com", "thedebrief.org",
    "sciencenews.org", "gizmodo.com", "newscientist.com", "digitaltrends.com", "cnn.com", "nytimes.com",
    "washingtonpost.com", "wsj.com", "theinformation.com", "semafor.com", "theregister.com", "pymnts.com",
    "techspot.com", "neowin.net", "xda-developers.com", "androidpolice.com", "kotaku.com", "polygon.com",
    "eurogamer.net", "pcgamer.com", "roadtovr.com", "insideevs.com", "theatlantic.com", "vox.com",
    "qz.com", "restofworld.org", "404media.co", "the-decoder.com", "euronews.com", "skynews.com",
    "news.sky.com", "foxnews.com", "cbsnews.com", "abcnews.go.com", "dailymail.co.uk", "phys.org",
    "techxplore.com", "medicalxpress.com", "sciencedaily.com", "gadgets360.com", "tomshardware.com",
    "strictlyvc.com", "macworld.com", "pcworld.com", "computerworld.com", "nikkei.com",
    "slashdot.org", "news.ycombinator.com", "decrypt.co", "coindesk.com",
)
# Jamais une source d'image non plus : réseaux sociaux et vidéos (dont YouTube : télécharger
# y est interdit), revues scientifiques, boutiques, raccourcisseurs, encyclopédies, moteurs.
NON_SOURCE_DOMAINS = (
    "facebook.com", "instagram.com", "threads.net", "threads.com", "tiktok.com", "linkedin.com",
    "reddit.com", "youtube.com", "youtu.be", "youtube-nocookie.com", "vimeo.com", "bsky.app",
    "mastodon.social", "discord.gg", "discord.com", "t.me", "telegram.me", "whatsapp.com", "pinterest.com",
    "snapchat.com", "twitch.tv", "medium.com", "substack.com", "tumblr.com",
    "doi.org", "nature.com", "science.org", "sciencemag.org", "wiley.com", "springer.com",
    "sciencedirect.com", "elsevier.com", "cell.com", "pnas.org", "arxiv.org", "biorxiv.org", "medrxiv.org",
    "ieeexplore.ieee.org", "acm.org", "plos.org", "frontiersin.org", "mdpi.com", "researchgate.net",
    "ssrn.com", "jamanetwork.com", "thelancet.com", "nejm.org", "bmj.com", "academic.oup.com",
    "tandfonline.com", "sagepub.com", "iop.org", "aps.org", "rsc.org", "acs.org", "semanticscholar.org",
    "amazon.com", "amazon.co.uk", "amazon.de", "amazon.fr", "ebay.com", "bestbuy.com", "walmart.com",
    "target.com", "aliexpress.com", "bit.ly", "tinyurl.com", "ow.ly", "lnkd.in", "buff.ly", "trib.al",
    "dlvr.it", "geni.us", "howl.me", "go.redirectingat.com", "go.skimresources.com", "shareasale.com",
    "awin1.com", "fave.co", "wikipedia.org", "wikimedia.org", "www.google.com", "news.google.com",
    "maps.google.com", "docs.google.com", "drive.google.com", "bing.com", "duckduckgo.com",
    "archive.org", "github.com", "apps.apple.com", "play.google.com", "ziffdavis.com",
)
# Lecteurs vidéo de régies publicitaires et de médias : jamais une vidéo de la source.
AD_VIDEO_HOSTS = (
    "ex.co", "connatix.com", "jwplatform.com", "jwpcdn.com", "jwpsrv.com", "brightcove.net",
    "brightcovecdn.com", "vidazoo.com", "primis.tech", "aniview.com", "teads.tv", "anyclip.com",
    "minutemedia-prebid.com", "vdo.ai", "springserve.com", "spotxchange.com", "ooyala.com", "dailymotion.com",
    "akamaized.net", "outbrain.com", "taboola.com",
)

# Crédits d'agences de presse et de banques d'images : jamais repris.
_AGENCY_RE = re.compile(
    r"\b(?:getty|reuters|associated press|ap photo|ap images|afp|agence france|bloomberg|shutterstock|alamy|"
    r"epa|efe|sipa|nurphoto|anadolu|zuma|sopa images|istock|istockphoto|adobe stock|dreamstime|"
    r"depositphotos|123rf|corbis|pa images|pa wire|pa media|press association|xinhua|kyodo|yonhap|imago|"
    r"abaca|dpa|ddp|picture alliance|picture-alliance|science photo library|bridgeman|unsplash|pexels|"
    r"pixabay|pool|wireimage|filmmagic|ullstein|eyevine|redux|polaris|newscom|splash news|backgrid|"
    r"rex features|stock photo)\b", re.I)
_AP_RE = re.compile(r"(?<![A-Za-z])AP(?![A-Za-z])")
# Crédits qui désignent l'équipe ou les archives d'un média.
_STAFF_CREDITS = frozenset({"future", "future plc", "staff", "file", "file photo", "stock", "stock image",
                            "archive", "archives"})
# Image « par défaut » d'un site (son logo, une vignette de partage générique).
# (Le nom du FICHIER commence par ces mots : « openai-logo.jpg » reste permis, c'est Claude qui juge.)
_DEFAULT_IMAGE_RE = re.compile(r"(?:^|/)(?:logo|default|placeholder|fallback|og-default|share-default|"
                               r"social-default|site-image)[^/]*$", re.I)
_VIDEO_FILE_RE = re.compile(r"\.(?:mp4|m4v|mov|webm)$", re.I)
_STREAM_RE = re.compile(r"\.(?:m3u8|mpd|f4m|ism)(?:$|/)", re.I)

# Ajouté à TOUTES les descriptions de photo IA (v1) : pas de texte (l'IA rate
# les lettres), pas de logo ni de vraie personne, rendu photo crédible.
STYLE_SUFFIX = (
    " Photorealistic photograph, shot on a full-frame camera with a 35mm lens, "
    "natural light, realistic skin and materials, subtle film grain, shallow depth "
    "of field. It must look like a real photo taken by a person, NOT an illustration, "
    "NOT a 3D render, NOT digital art, no neon, no cyberpunk, no cartoon, no glow. "
    "No text, no letters, no numbers, no captions, no watermarks. No real logos, "
    "brands, or recognizable real people. Muted, believable colors."
)


# ---------------------------------------------------------------------------
# Adresses
# ---------------------------------------------------------------------------
def host_of(url: str) -> str:
    """Hôte d'une adresse, lu comme le lit un navigateur : « \\ » vaut « / »
    (https://x.com\\@site.com ouvre x.com), lettres pleine chasse et %2E ramenées
    à la normale."""
    try:
        u = urlparse((url or "").strip().replace("\\", "/"))
        host = u.hostname or ""
    except ValueError:
        return ""
    if u.scheme not in ("http", "https") or not host:
        return ""
    return unicodedata.normalize("NFKC", unquote(host)).lower().rstrip(".")


def _host_in(host: str, domains: tuple[str, ...]) -> bool:
    return bool(host) and any(host == d or host.endswith("." + d) for d in domains)


def is_x_url(url: str) -> bool:
    return _host_in(host_of(url), X_DOMAINS)


_TWO_LEVEL_SUFFIXES = (".co.uk", ".org.uk", ".com.au", ".co.jp", ".co.in", ".com.br", ".co.nz", ".co.za")


def site_of(url: str) -> str:
    """« theverge.com » pour https://www.theverge.com/… (domaine du site, sans sous-domaine)."""
    host = host_of(url)
    labels = host.split(".")
    keep = 3 if host.endswith(_TWO_LEVEL_SUFFIXES) else 2
    return ".".join(labels[-keep:]) if len(labels) >= keep else host


def is_press_url(url: str) -> bool:
    return _host_in(host_of(url), PRESS_SITES)


def is_source_url(url: str) -> bool:
    """Une page qui peut être la SOURCE officielle d'une histoire : http(s), jamais X, ni un
    site de presse, ni un réseau social, une revue scientifique, une boutique…"""
    host = host_of(url)
    if not host or _host_in(host, X_DOMAINS):
        return False
    return not (is_press_url(url) or _host_in(host, NON_SOURCE_DOMAINS))


def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)]


def public_host(host: str) -> bool:
    """L'hôte est-il sur Internet ? Jamais la machine elle-même, un réseau privé ou le
    service de métadonnées du serveur (169.254.169.254) : une page piégée pourrait y
    faire pointer sa « photo »."""
    host = (host or "").strip().lower().strip("[]")
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")):
        return False
    try:
        addrs = _resolve(host)
    except (OSError, UnicodeError, ValueError):
        return False
    try:
        return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Fichiers
# ---------------------------------------------------------------------------
def _safe_tag(tag: str) -> str:
    t = re.sub(r"[^A-Za-z0-9_-]+", "-", tag or "post").strip("-")
    return (t or "post")[:40]


def _safe_unlink(path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def _cleanup_old(folder: Path, keep: int) -> None:
    try:
        files = sorted((p for p in folder.iterdir() if p.is_file()),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            _safe_unlink(old)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Lecture d'une page : ses balises <meta>, son texte, les liens de son article
# ---------------------------------------------------------------------------
class _HeadMeta(HTMLParser):
    """Les balises <meta> d'une page (og:image, og:video, og:site_name, og:title…)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "meta":
            return
        a = {str(k).lower(): str(v or "") for k, v in attrs if k}
        key = (a.get("property") or a.get("name") or a.get("itemprop") or "").strip().lower()
        value = a.get("content", "").strip()
        if key and value:
            self.meta.setdefault(key, value)


def page_meta(page_html: str) -> dict[str, str]:
    """Les balises <meta> de l'en-tête de la page (clé en minuscules → contenu)."""
    head = (page_html or "")[:HEAD_MAX_CHARS]
    end = head.lower().find("</head>")
    if end > 0:
        head = head[:end]
    parser = _HeadMeta()
    try:
        parser.feed(head)
        parser.close()
    except Exception:  # noqa: BLE001 — HTML très cassé : on garde ce qui a été lu
        pass
    return parser.meta


class _TextLines(HTMLParser):
    """Le texte visible d'une page, une ligne par bloc (paragraphe, légende, titre…)."""
    BLOCK = {"p", "br", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th",
             "blockquote", "section", "article", "figure", "figcaption", "table", "hr", "header", "footer",
             "aside", "main", "nav", "picture", "time", "small", "cite"}
    SKIP = {"script", "style", "noscript", "iframe", "svg", "object", "template", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        else:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.parts.append("\n" if tag in self.BLOCK else " ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")
        else:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def page_lines(page_html: str) -> str:
    parser = _TextLines()
    try:
        parser.feed(page_html or "")
        parser.close()
    except Exception:  # noqa: BLE001
        pass
    lines = (re.sub(r"\s+", " ", ln).strip() for ln in "".join(parser.parts).splitlines())
    return "\n".join(ln for ln in lines if ln)


class _ArticleLinks(HTMLParser):
    """Les liens écrits DANS les paragraphes d'un article (pas les menus ni le pied de page)."""
    SKIP = {"script", "style", "noscript", "nav", "footer", "aside", "header", "form"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._para = 0
        self._skip = 0
        self._current: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "p":
            self._para += 1
        elif tag == "a" and self._para and not self._skip:
            href = dict((str(k).lower(), str(v or "")) for k, v in attrs if k).get("href", "")
            self._current = [href, ""]

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "p":
            self._para = max(0, self._para - 1)
        elif tag == "a" and self._current is not None:
            self.links.append((self._current[0], self._current[1]))
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current[1] += data


def article_links(page_html: str, page_url: str) -> list[tuple[str, str]]:
    """(adresse, texte du lien) des liens de l'article qui POURRAIENT mener à la source
    officielle : vers un autre site, jamais X, un média, un réseau social, une revue, une
    boutique. Sans doublon, `MAX_LINKS` au plus."""
    parser = _ArticleLinks()
    try:
        parser.feed(page_html or "")
        parser.close()
    except Exception:  # noqa: BLE001
        pass
    own = site_of(page_url)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, text in parser.links:
        url = urljoin(page_url or "", unescape(href.strip())).split("#")[0]
        if not url.lower().startswith(("http://", "https://")) or site_of(url) == own:
            continue
        if not is_source_url(url):
            continue
        key = url.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((url, re.sub(r"\s+", " ", text).strip()[:100]))
        if len(out) >= MAX_LINKS:
            break
    return out


# ---------------------------------------------------------------------------
# Ce que la page officielle montre : sa photo d'ouverture (et son crédit), sa vidéo
# ---------------------------------------------------------------------------
_IMAGE_META_KEYS = ("og:image:secure_url", "og:image", "og:image:url", "twitter:image", "twitter:image:src",
                    "image", "thumbnailurl")


def lead_image_url(meta: dict[str, str], page_url: str) -> str:
    """L'adresse de la photo d'ouverture déclarée par la page ("" sans photo utilisable).
    Jamais une image de X ; jamais l'image « par défaut » du site (son logo)."""
    for key in _IMAGE_META_KEYS:
        raw = (meta.get(key) or "").strip()
        if not raw:
            continue
        url = urljoin(page_url or "", unescape(raw))
        if not url.lower().startswith(("http://", "https://")) or is_x_url(url):
            continue
        if _DEFAULT_IMAGE_RE.search(urlparse(url).path or ""):
            log.info("Photo : image par défaut du site ignorée (%s)", url[:100])
            continue
        return url
    return ""


_VIDEO_TAG_RE = re.compile(r"<(?:video|source)\b[^>]*?\bsrc\s*=\s*[\"']([^\"'>]+)[\"'][^>]*>", re.I)
_LD_VIDEO_RE = re.compile(r"\"contentUrl\"\s*:\s*\"([^\"]+)\"", re.I)


def _usable_video_url(url: str, declared_type: str = "") -> bool:
    if not url.lower().startswith(("http://", "https://")) or is_x_url(url):
        return False
    host, path = host_of(url), urlparse(url).path or ""
    if _host_in(host, AD_VIDEO_HOSTS) or _host_in(host, NON_SOURCE_DOMAINS) or _STREAM_RE.search(path):
        return False
    declared = (declared_type or "").lower()
    return bool(_VIDEO_FILE_RE.search(path)) or declared in ("video/mp4", "video/webm", "video/quicktime")


def lead_video_url(meta: dict[str, str], page_html: str, page_url: str) -> str:
    """L'adresse du FICHIER vidéo que la page publie ("" sinon) : og:video, le lecteur
    déclaré pour le partage, une balise <video>, une vidéo décrite pour les moteurs.
    Jamais YouTube (télécharger y est interdit), jamais un flux en direct, jamais une régie
    publicitaire, jamais X."""
    vtype = meta.get("og:video:type", "")
    for key in ("og:video:secure_url", "og:video:url", "og:video"):
        raw = (meta.get(key) or "").strip()
        if raw:
            url = urljoin(page_url or "", unescape(raw))
            if _usable_video_url(url, vtype):
                return url
    raw = (meta.get("twitter:player:stream") or "").strip()
    if raw:
        url = urljoin(page_url or "", unescape(raw))
        if _usable_video_url(url, meta.get("twitter:player:stream:content_type", "")):
            return url
    for rx in (_VIDEO_TAG_RE, _LD_VIDEO_RE):
        for m in rx.finditer(page_html or ""):
            url = urljoin(page_url or "", unescape(m.group(1).strip().replace("\\/", "/")))
            if _usable_video_url(url):
                return url
    return ""


_CREDIT_RES = (
    # « Image Credits: OpenAI », « (Image credit: Google) », « Photo credit: … »
    re.compile(r"\b(?:image|photo|photograph|picture|illustration|video|header image|lead image|top image)s?"
               r"\s+credits?\s*[:：]\s*(?P<c>[^\n]{2,90})", re.I),
    # « Credit: Google »
    re.compile(r"\bcredits?\s*[:：]\s*(?P<c>[^\n]{2,90})", re.I),
    # « Photograph: Jane Doe/Reuters », « Image: Google », « Photo: … »
    re.compile(r"\b(?:image|photo|photograph|picture|illustration|screenshot|graphic|rendering|render)"
               r"\s*[:：]\s*(?P<c>[^\n]{2,90})", re.I),
    # « Photo by Jane Doe / The Verge », « Image via Google »
    re.compile(r"\b(?:photo|image|photograph|illustration|screenshot|picture)\s+(?:by|from|via)\s+"
               r"(?P<c>[^\n]{2,90})", re.I),
    # « Courtesy of Google »
    re.compile(r"\bcourtesy(?:\s+of)?\s+(?P<c>[^\n]{2,70})", re.I),
)


def _clean_credit(raw: str) -> str:
    # Texte réservé aux lecteurs d'écran, collé au lien du crédit (« (opens in a new window) »).
    raw = re.sub(r"\(?\s*opens? in (?:a )?new (?:window|tab)\s*\)?", " ", raw or "", flags=re.I)
    c = re.split(r"\s{2,}|\s[|•·]\s|(?<=[a-z])\.\s", raw)[0]
    c = c.strip(" \t:;,.()[]")
    return c[:80]


def photo_credit(page_html: str, title: str = "") -> str:
    """La ligne de crédit de l'image d'ouverture, cherchée près du titre de la page
    ("" quand la page n'en donne pas)."""
    text = page_lines(page_html)
    if not text:
        return ""
    start, end = 0, min(len(text), CREDIT_AFTER_TITLE * 2)
    key = re.sub(r"\s+", " ", (title or "").strip())[:60].lower()
    if len(key) >= 12:
        pos = text.lower().find(key)
        if pos >= 0:
            start, end = max(0, pos - CREDIT_BEFORE_TITLE), pos + CREDIT_AFTER_TITLE
    zone = text[start:end]
    best: tuple[int, str] | None = None
    for rx in _CREDIT_RES:
        m = rx.search(zone)
        if m and (best is None or m.start() < best[0]):
            credit = _clean_credit(m.group("c"))
            if credit:
                best = (m.start(), credit)
    return best[1] if best else ""


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", text or "").lower())


def _names_the_site(credit: str, name: str) -> bool:
    """Le crédit nomme-t-il ce site ? Mot pour mot (« IGN » n'est pas dans « Signal ») ;
    un nom long se reconnaît aussi collé (« The Verge » / « theverge »)."""
    credit_words, name_words = " ".join(_words(credit)), " ".join(_words(name))
    if not name_words:
        return False
    if f" {name_words} " in f" {credit_words} ":
        return True
    glued = name_words.replace(" ", "")
    return len(glued) >= 6 and glued in credit_words.replace(" ", "")


def credit_problem(credit: str, site_name: str = "", page_url: str = "") -> str | None:
    """Pourquoi l'image ne doit PAS être reprise d'après son crédit, ou None. Sans
    crédit, rien n'est décidé ici : c'est Claude qui juge sur l'image."""
    c = (credit or "").strip()
    if not c:
        return None
    if _AGENCY_RE.search(c) or _AP_RE.search(c):
        return f"photo d'agence ou de banque d'images ({c[:40]})"
    if c.lower().strip(" .") in _STAFF_CREDITS:
        return f"photo du média ({c[:40]})"
    if is_press_url(page_url):
        names = (site_name, site_of(page_url).split(".")[0])
        if any(_names_the_site(c, n) for n in names):
            return f"photo du média lui-même ({c[:40]})"
    return None


@dataclass
class SourcePage:
    """La page officielle d'une histoire, lue une seule fois."""
    url: str
    title: str                       # le titre de l'actu citée (pour Claude et le crédit)
    html: str
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def site(self) -> str:
        return self.meta.get("og:site_name") or self.meta.get("application-name") or site_of(self.url)

    @property
    def credit(self) -> str:
        return photo_credit(self.html, self.title or self.meta.get("og:title", ""))


def read_page(url: str, http_get: Callable | None = None) -> str:
    """Le HTML d'une page ("" si illisible ou si c'est X). Ne lève jamais."""
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")) or is_x_url(url):
        return ""
    try:
        if http_get is None:
            from . import sources
            status, page = sources.default_http_get(url, accept=sources.HTML_ACCEPT)
        else:
            status, page = http_get(url)
    except Exception as e:  # noqa: BLE001 — réseau, délai, encodage
        log.info("Image : page illisible (%s) : %s", type(e).__name__, host_of(url))
        return ""
    if status != 200 or not page:
        log.info("Image : page illisible (HTTP %s) : %s", status, host_of(url))
        return ""
    return page


def story_candidates(draft) -> list:
    """Les actus CITÉES dont on cherche la source, dans l'ordre : celle que Claude a
    désignée, puis les autres. Jamais X, jamais deux fois la même page."""
    visual = getattr(draft, "visual", None)
    wanted = str(getattr(visual, "target", "") or "").strip().rstrip("/").lower()
    cited = [src for src in (getattr(draft, "sources", None) or [])
             if str(getattr(src, "url", "") or "").lower().startswith(("http://", "https://"))
             and not is_x_url(str(getattr(src, "url", "") or ""))]
    cited.sort(key=lambda src: str(getattr(src, "url", "") or "").strip().rstrip("/").lower() != wanted)
    out, seen = [], set()
    for src in cited:
        key = str(src.url).strip().rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(src)
    return out[:MAX_PAGES_TRIED]


def source_pages(draft, writer, *, http_get: Callable | None = None, post: str = "",
                 deadline: float | None = None) -> list[SourcePage]:
    """Les pages OFFICIELLES de l'histoire du post (jamais celle d'un média) : la page d'une
    actu venue d'un flux officiel, ou la page vers laquelle l'article de presse renvoie,
    désignée par Claude parmi les liens de l'article."""
    pick = getattr(writer, "pick_official_link", None)
    pages: list[SourcePage] = []
    seen: set[str] = set()
    for src in story_candidates(draft):
        if deadline is not None and time.monotonic() > deadline:
            break
        url, title = str(src.url), str(getattr(src, "title", "") or "")
        if getattr(src, "official", False) and is_source_url(url):
            target, html = url, ""
        else:
            article = read_page(url, http_get)
            links = article_links(article, url) if article else []
            if not links:
                if article:
                    log.info("Image : l'article ne renvoie à aucune page officielle (%s)", host_of(url))
                continue
            n = 0
            if callable(pick):
                try:
                    n = int(pick(title, post, links) or 0)
                except Exception as e:  # noqa: BLE001
                    log.info("Image : choix de la source impossible (%s)", type(e).__name__)
            if not 1 <= n <= len(links):
                log.info("Image : pas de page officielle parmi les %d liens de %s", len(links), host_of(url))
                continue
            target, html = links[n - 1][0], ""
        key = target.rstrip("/").lower()
        if key in seen or not is_source_url(target):
            continue
        seen.add(key)
        html = html or read_page(target, http_get)
        if html:
            pages.append(SourcePage(url=target, title=title, html=html, meta=page_meta(html)))
            log.info("Image : source officielle %s (pour « %s »)", target[:100], title[:60])
    return pages


# ---------------------------------------------------------------------------
# Téléchargements
# ---------------------------------------------------------------------------
def _user_agent() -> str:
    from .sources import USER_AGENT
    return USER_AGENT


class _Refused(Exception):
    pass


def _download(url: str, out: BinaryIO, *, accept: str, kinds: tuple[str, ...], max_bytes: int,
              max_seconds: float, timeout: float, what: str) -> bool:
    """Télécharge `url` dans `out` : redirections suivies une à une (jamais vers X ni vers
    une adresse privée), taille et durée bornées, un type de fichier attendu. True si fait."""
    import requests

    headers = {"User-Agent": _user_agent(), "Accept": accept}
    deadline = time.monotonic() + max_seconds
    for _hop in range(MAX_REDIRECTS + 1):
        host = host_of(url)
        if not host or _host_in(host, X_DOMAINS):
            log.info("%s : adresse refusée (%s)", what, host or url[:60])
            return False
        if not public_host(host):
            log.info("%s : adresse privée ou introuvable, refusée (%s)", what, host)
            return False
        with requests.get(url, timeout=timeout, headers=headers, stream=True, allow_redirects=False) as resp:
            if resp.is_redirect:
                location = resp.headers.get("Location")
                if not location:
                    return False
                url = urljoin(url, location)
                continue
            if resp.status_code != 200:
                log.info("%s : HTTP %s (%s)", what, resp.status_code, host)
                return False
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype and not (ctype.startswith(kinds) or ctype in ("application/octet-stream",
                                                                     "binary/octet-stream")):
                log.info("%s : type inattendu (%s, %s)", what, ctype, host)
                return False
            declared = (resp.headers.get("Content-Length") or "").strip()
            if declared.isdigit() and int(declared) > max_bytes:
                log.info("%s : trop lourd (%s octets, %s)", what, declared, host)
                return False
            size = 0
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    log.info("%s : trop lourd (> %d Mo, %s)", what, max_bytes // 2**20, host)
                    return False
                out.write(chunk)
                if time.monotonic() > deadline:
                    log.info("%s : téléchargement trop lent (%s)", what, host)
                    return False
            return size > 0
    log.info("%s : trop de redirections", what)
    return False


def fetch_image_bytes(url: str) -> bytes | None:
    """Télécharge une image : 10 Mo et 30 s au plus, un vrai type d'image. None sinon."""
    buf = io.BytesIO()
    ok = _download(url, buf, accept=PHOTO_ACCEPT, kinds=("image/",), max_bytes=PHOTO_MAX_BYTES,
                   max_seconds=PHOTO_MAX_SECONDS, timeout=PHOTO_TIMEOUT_S, what="Photo")
    return buf.getvalue() if ok else None


def fetch_video_file(url: str, dest: Path) -> bool:
    """Télécharge une vidéo dans `dest` : 200 Mo et 3 min au plus, un vrai type vidéo."""
    try:
        with open(dest, "wb") as out, utils.heartbeat("vidéo : téléchargement", every_s=30.0,
                                                      max_total_s=VIDEO_MAX_SECONDS + 60):
            ok = _download(url, out, accept=VIDEO_ACCEPT, kinds=("video/", "application/mp4"),
                           max_bytes=VIDEO_MAX_BYTES, max_seconds=VIDEO_MAX_SECONDS,
                           timeout=VIDEO_TIMEOUT_S, what="Vidéo")
    except Exception as e:  # noqa: BLE001 — réseau coupé, disque plein : la photo prendra le relais
        log.info("Vidéo : téléchargement impossible (%s) : %s", type(e).__name__, host_of(url))
        ok = False
    if not ok:
        _safe_unlink(dest)
    return ok


def normalize_photo(raw: bytes, out_path: Path) -> str | None:
    """Remet l'image en JPEG propre dans `out_path` : bien orientée, en RGB (fond blanc
    sous la transparence), 2048 px au plus, sans métadonnées. None si c'est fait, sinon
    la raison du refus (illisible, vignette, format bizarre)."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return "Pillow n'est pas installé"
    try:
        with Image.open(io.BytesIO(raw or b"")) as im:
            w, h = im.size
            if w * h > PHOTO_MAX_PIXELS:
                return f"image démesurée ({w}×{h})"
            im.seek(0)                              # 1re image d'un GIF ou d'un WebP animé
            img = ImageOps.exif_transpose(im) or im
            w, h = img.size
            if w < PHOTO_MIN_WIDTH or h < PHOTO_MIN_HEIGHT:
                return f"trop petite ({w}×{h})"
            if not PHOTO_MIN_RATIO <= w / h <= PHOTO_MAX_RATIO:
                return f"format inhabituel ({w}×{h})"
            if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                img = flat
            else:
                img = img.convert("RGB")
            img.thumbnail((PHOTO_MAX_SIDE, PHOTO_MAX_SIDE), Image.Resampling.LANCZOS)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(out_path, "JPEG", quality=JPEG_QUALITY, optimize=True)   # sans EXIF ni autres métadonnées
    except Exception as e:  # noqa: BLE001 — fichier tronqué, format inconnu, image piégée
        _safe_unlink(out_path)
        return f"image illisible ({type(e).__name__})"
    return None


def download_photo(url: str, out_path: Path, fetch: Callable[[str], bytes | None] | None = None) -> Path | None:
    """Télécharge la photo et la met au propre. Le chemin, ou None. Ne lève jamais."""
    try:
        raw = (fetch or fetch_image_bytes)(url)
    except Exception as e:  # noqa: BLE001
        log.info("Photo : téléchargement impossible (%s) : %s", type(e).__name__, host_of(url))
        return None
    if not raw:
        return None
    why = normalize_photo(raw, out_path)
    if why:
        log.info("Photo écartée (%s) : %s", why, url[:100])
        return None
    return out_path


# ---------------------------------------------------------------------------
# L'extrait vidéo (ffmpeg, livré par le paquet imageio-ffmpeg : rien à installer à la main)
# ---------------------------------------------------------------------------
def ffmpeg_exe() -> str | None:
    """Le programme ffmpeg : celui du paquet imageio-ffmpeg, sinon celui du système."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return exe
    except Exception:  # noqa: BLE001 — paquet absent ou binaire introuvable
        pass
    return shutil.which("ffmpeg")


@dataclass
class VideoInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    audio: bool = False


def probe_video(exe: str, path: Path) -> VideoInfo | None:
    """Durée, taille de l'image et présence du son, lus dans ce qu'affiche ffmpeg. None si
    le fichier n'a pas de piste vidéo lisible."""
    try:
        res = subprocess.run([exe, "-hide_banner", "-nostdin", "-protocol_whitelist", "file",
                              "-i", str(path)], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    err = res.stderr or ""
    dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    video = re.search(r"Stream #\S+.*?Video:.*?(\d{2,5})x(\d{2,5})", err)
    if not (dur and video):
        return None
    h, m, s = dur.groups()
    info = VideoInfo(duration=int(h) * 3600 + int(m) * 60 + float(s), width=int(video.group(1)),
                     height=int(video.group(2)), audio=bool(re.search(r"Stream #\S+.*?Audio:", err)))
    rot = re.search(r"rotation of (-?\d+(?:\.\d+)?)|rotate\s*:\s*(-?\d+)", err)
    if rot and abs(round(float(rot.group(1) or rot.group(2)))) % 180 == 90:
        info.width, info.height = info.height, info.width
    return info


def make_clip(src: Path, out: Path, exe: str | None = None) -> str | None:
    """L'extrait publié : les `CLIP_MAX_S` premières secondes, en MP4 H.264/AAC (ce qu'X
    lit partout), 1280 px au plus, 30 images/s au plus, lecture immédiate. ffmpeg ne lit
    QUE le fichier téléchargé (aucun accès réseau). None si c'est fait, sinon la raison."""
    exe = exe or ffmpeg_exe()
    if not exe:
        return "ffmpeg absent"
    info = probe_video(exe, src)
    if info is None:
        return "fichier vidéo illisible"
    if info.duration < CLIP_MIN_S:
        return f"vidéo trop courte ({info.duration:.1f} s)"
    if not info.width or not info.height or not CLIP_MIN_RATIO <= info.width / info.height <= CLIP_MAX_RATIO:
        return f"format inhabituel ({info.width}×{info.height})"
    scale = (f"scale=w='min({CLIP_MAX_SIDE},iw)':h='min({CLIP_MAX_SIDE},ih)':force_original_aspect_ratio=decrease,"
             "scale=trunc(iw/2)*2:trunc(ih/2)*2")
    cmd = [exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-protocol_whitelist", "file",
           "-i", str(src), "-t", str(CLIP_MAX_S), "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
           "-vf", scale, "-fpsmax", "30", "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
           "-pix_fmt", "yuv420p", "-crf", "23", "-maxrate", "6M", "-bufsize", "12M",
           "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "44100",
           "-movflags", "+faststart", "-map_metadata", "-1", str(out)]
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with utils.heartbeat("vidéo : extrait", every_s=30.0, max_total_s=FFMPEG_TIMEOUT_S + 60):
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _safe_unlink(out)
        return "découpage trop long"
    except OSError as e:
        _safe_unlink(out)
        return f"ffmpeg impossible à lancer ({type(e).__name__})"
    if res.returncode != 0 or not out.is_file():
        _safe_unlink(out)
        lines = (res.stderr or "").strip().splitlines()
        return f"découpage impossible ({lines[-1][:120] if lines else res.returncode})"
    done = probe_video(exe, out)
    if done is None or done.duration < CLIP_MIN_S or out.stat().st_size > CLIP_MAX_BYTES:
        _safe_unlink(out)
        return "extrait inutilisable"
    return None


def clip_frames(clip: Path, folder: Path, exe: str | None = None) -> list[Path]:
    """Trois images de l'extrait (début, milieu, fin) : ce que Claude regarde."""
    exe = exe or ffmpeg_exe()
    info = probe_video(exe, clip) if exe else None
    if info is None:
        return []
    frames: list[Path] = []
    for i, at in enumerate(CLIP_FRAMES):
        out = folder / f"frame{i}.jpg"
        try:
            subprocess.run([exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                            "-protocol_whitelist", "file", "-ss", f"{info.duration * at:.2f}", "-i", str(clip),
                            "-frames:v", "1", "-vf", "scale=768:-2", "-q:v", "3", str(out)],
                           capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.is_file() and out.stat().st_size > 0:
            frames.append(out)
    return frames


def video_self_test() -> tuple[bool, str]:
    """Le serveur sait-il tirer un extrait ? Une vidéo d'essai d'une seconde est fabriquée
    sur place (aucun réseau), puis découpée comme une vraie (`--check`)."""
    exe = ffmpeg_exe()
    if not exe:
        return False, "ffmpeg absent (paquet imageio-ffmpeg)"
    try:
        with tempfile.TemporaryDirectory(prefix="xbot-video-") as tmp:
            src, out = Path(tmp) / "src.mp4", Path(tmp) / "clip.mp4"
            gen = subprocess.run([exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                                  "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25",
                                  "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                                  "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)],
                                 capture_output=True, text=True, timeout=120)
            if gen.returncode != 0:
                return False, (gen.stderr or "vidéo d'essai impossible").strip().splitlines()[-1][:160]
            why = make_clip(src, out, exe)
            if why:
                return False, why
            if not clip_frames(out, Path(tmp), exe):
                return False, "images de l'extrait impossibles"
    except Exception as e:  # noqa: BLE001
        return False, (str(e).splitlines()[0][:160] if str(e) else type(e).__name__)
    return True, ""


def self_test() -> tuple[bool, str]:
    """Le serveur sait-il mettre une photo au propre ? Une image d'essai est fabriquée
    sur place (aucun réseau) puis passée par la même mise au propre (`--check`)."""
    try:
        from PIL import Image
    except ImportError:
        return False, "Pillow n'est pas installé"
    try:
        buf = io.BytesIO()
        Image.new("RGBA", (1200, 675), (30, 120, 200, 255)).save(buf, "PNG")
        with tempfile.TemporaryDirectory(prefix="xbot-photo-") as tmp:
            out = Path(tmp) / "check.jpg"
            why = normalize_photo(buf.getvalue(), out)
            if why:
                return False, why
            if not (out.is_file() and out.stat().st_size > 1000):
                return False, "la photo d'essai est vide"
    except Exception as e:  # noqa: BLE001
        return False, (str(e).splitlines()[0][:160] if str(e) else type(e).__name__)
    return True, ""


# ---------------------------------------------------------------------------
# Quelle image pour CE post : la vidéo, sinon la photo, de la source officielle
# ---------------------------------------------------------------------------
def _try_video(page: SourcePage, check, tag: str, post: str, fetch_video: Callable | None) -> str | None:
    vurl = lead_video_url(page.meta, page.html, page.url)
    if not vurl:
        return None
    credit = page.credit
    why = credit_problem(credit, page.site, page.url)
    if why:
        log.info("Vidéo écartée (%s) : %s", why, page.url[:100])
        return None
    exe = ffmpeg_exe()
    if not exe:
        log.info("Vidéo ignorée : ffmpeg absent (paquet imageio-ffmpeg)")
        return None
    folder = utils.MEDIA_DIR / "videos"
    folder.mkdir(parents=True, exist_ok=True)
    clip = folder / f"{tag}_{time.strftime('%Y%m%d-%H%M%S')}.mp4"
    with tempfile.TemporaryDirectory(prefix="xbot-video-", dir=str(folder)) as tmp:
        src = Path(tmp) / "source"
        if not (fetch_video or fetch_video_file)(vurl, src):
            return None
        why = make_clip(src, clip, exe)
        if why:
            log.info("Vidéo écartée (%s) : %s", why, vurl[:100])
            return None
        frames = clip_frames(clip, Path(tmp), exe)
        if not frames:
            log.info("Vidéo écartée (images illisibles) : %s", vurl[:100])
            _safe_unlink(clip)
            return None
        try:
            ok, issue = check(frames, headline=page.title, post=post, credit=credit, official=True)
        except Exception as e:  # noqa: BLE001
            ok, issue = False, f"vérification impossible ({type(e).__name__})"
    if ok is True:
        log.info("Vidéo prête : %s (%s, extrait de %d s au plus)", clip.name, vurl[:100], CLIP_MAX_S)
        _cleanup_old(folder, VIDEOS_KEEP)
        return str(clip)
    log.info("Vidéo écartée (%s) : %s", issue or "pas utilisable", vurl[:100])
    _safe_unlink(clip)
    return None


def _try_photo(page: SourcePage, check, tag: str, n: int, post: str, fetch: Callable | None) -> str | None:
    image = lead_image_url(page.meta, page.url)
    if not image:
        log.info("Photo : la page officielle n'en déclare aucune (%s)", host_of(page.url))
        return None
    credit = page.credit
    why = credit_problem(credit, page.site, page.url)
    if why:
        log.info("Photo écartée (%s) : %s", why, page.url[:100])
        return None
    folder = utils.MEDIA_DIR / "photos"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = download_photo(image, folder / f"{tag}{'-' + str(n) if n > 1 else ''}_{stamp}.jpg", fetch=fetch)
    if not path:
        return None
    # Claude REGARDE la photo ; sans réponse claire, elle n'est jamais utilisée.
    try:
        ok, issue = check(path, headline=page.title, post=post, credit=credit, official=True)
    except Exception as e:  # noqa: BLE001
        ok, issue = False, f"vérification impossible ({type(e).__name__})"
    if ok is True:
        log.info("Photo prête : %s (%s, crédit : %s)", path.name, page.url[:100], credit or "aucun")
        _cleanup_old(folder, PHOTOS_KEEP)
        return str(path)
    log.info("Photo écartée (%s) : %s", issue or "pas utilisable", page.url[:100])
    _safe_unlink(path)
    return None


def _story_media(settings, draft, writer, tag: str, *, http_get: Callable | None = None,
                 fetch: Callable | None = None, fetch_video: Callable | None = None) -> tuple[str | None, str | None]:
    photos_on = bool(getattr(settings, "photos_enabled", False))
    videos_on = bool(getattr(settings, "videos_enabled", False))
    check_photo = getattr(writer, "photo_is_usable", None)
    check_video = getattr(writer, "video_is_usable", None)
    photos_on = photos_on and callable(check_photo)
    videos_on = videos_on and callable(check_video)
    if not (photos_on or videos_on):
        log.info("Image ignorée : photos et vidéos coupées, ou aucun contrôle par Claude possible")
        return None, None
    texts = getattr(draft, "texts", None) or [""]
    post = str(texts[0] or "") if isinstance(texts, list) and texts else ""
    deadline = time.monotonic() + MEDIA_SEARCH_MAX_S
    pages = source_pages(draft, writer, http_get=http_get, post=post, deadline=deadline)
    if not pages:
        log.info("Image : aucune page officielle de la source, le post part en texte seul")
        return None, None
    if videos_on:
        for page in pages:
            if time.monotonic() > deadline:
                break
            path = _try_video(page, check_video, tag, post, fetch_video)
            if path:
                return path, VIDEO_KIND
    if photos_on:
        for n, page in enumerate(pages, start=1):
            if time.monotonic() > deadline:
                log.info("Image : recherche trop longue, le post part en texte seul")
                break
            path = _try_photo(page, check_photo, tag, n, post, fetch)
            if path:
                return path, PHOTO_KIND
    return None, None


# ---------------------------------------------------------------------------
# Photo IA (OpenAI, désactivée par défaut)
# ---------------------------------------------------------------------------
def generate_ai_image(settings, prompt: str, tag: str = "post") -> Path | None:
    """PNG généré à partir de `prompt`, ou None (clé absente, SDK manquant,
    erreur API). 120 s par essai, 1 nouvel essai au plus."""
    prompt = (prompt or "").strip()
    if not prompt:
        return None
    if not settings.openai_api_key:
        log.info("Photo IA : OPENAI_API_KEY absente, post en texte seul")
        return None
    try:
        from openai import OpenAI
    except ImportError:
        log.warning("Photo IA : le paquet 'openai' n'est pas installé (pip install openai)")
        return None
    model = settings.image_model or "gpt-image-1"
    size = settings.image_size or "1024x1024"
    quality = settings.image_quality or "medium"
    folder = utils.MEDIA_DIR / "generated"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        client = OpenAI(api_key=settings.openai_api_key, timeout=120.0, max_retries=1)
        kwargs = {"model": model, "prompt": prompt + STYLE_SUFFIX, "size": size, "n": 1}
        if model.startswith("gpt-image"):
            kwargs["quality"] = quality
        t0 = time.time()
        with utils.heartbeat("photo IA", every_s=60.0, max_total_s=300.0):
            resp = client.images.generate(**kwargs)
        item = resp.data[0]
        out_path = folder / f"{tag}_{time.strftime('%Y%m%d-%H%M%S')}.png"
        b64 = getattr(item, "b64_json", None)
        if b64:
            out_path.write_bytes(base64.b64decode(b64))
        else:
            url = getattr(item, "url", None)
            if not url:
                log.warning("Photo IA : réponse sans image")
                return None
            import requests
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            out_path.write_bytes(r.content)
        log.info("Photo IA générée (%s, %s, %.0f s) : %s — %r", model, size, time.time() - t0,
                 out_path.name, prompt[:100])
        _cleanup_old(folder, GENERATED_KEEP)
        return out_path
    except Exception as e:  # noqa: BLE001 — ne jamais logger la clé : type + statut seulement
        log.warning("Photo IA : échec (%s%s)", type(e).__name__,
                    f", HTTP {e.status_code}" if getattr(e, "status_code", None) else "")
        return None


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------
def prepare(settings, draft, writer, tag: str = "post") -> tuple[str | None, str | None]:
    """(chemin, "video" | "photo" | "ai") ou (None, None). Ne lève jamais."""
    try:
        return _prepare(settings, draft, writer, _safe_tag(tag))
    except Exception as e:  # noqa: BLE001
        log.exception("Visuel : erreur inattendue (%s), post en texte seul", e)
        return None, None


def _prepare(settings, draft, writer, tag: str) -> tuple[str | None, str | None]:
    if getattr(draft, "poll", None):
        log.info("Visuel ignoré : X n'accepte pas d'image sur un sondage")
        return None, None
    visual = getattr(draft, "visual", None)
    if visual is None:
        return None, None
    kind = str(getattr(visual, "kind", "") or "").strip().lower()
    target = str(getattr(visual, "target", "") or "").strip()
    if kind in (PHOTO_KIND, "shot"):          # « shot » : brouillon d'avant novembre 2026
        return _story_media(settings, draft, writer, tag)
    if kind == "ai" and target:
        if not (getattr(settings, "image_ai_enabled", False) and getattr(settings, "openai_api_key", "")):
            log.info("Photo IA ignorée : IMAGE_AI_ENABLED=false ou OPENAI_API_KEY absente")
            return None, None
        if contains_url(target) or off_limits_term(target):
            log.info("Photo IA refusée (lien ou sujet interdit dans la description)")
            return None, None
        path = generate_ai_image(settings, target, tag)
        return (str(path), "ai") if path else (None, None)
    return None, None
