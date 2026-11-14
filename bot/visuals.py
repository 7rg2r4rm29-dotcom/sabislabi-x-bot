"""Image des posts : la photo de l'actu, jamais une capture d'écran.

Le compte donne la nouvelle lui-même, comme un compte d'alertes info : le texte dit
le fait, l'image montre ce dont il parle (le produit, le robot, l'appli, le visuel
officiel de l'annonce). Jamais la page, le logo ou le titre d'un autre média.

D'où vient la photo (`prepare` → `_photo_visual`) :
  - la page d'une actu CITÉE est lue en HTTP, comme le fait le lecteur d'articles
    (jamais x.com) : celle que Claude a désignée, puis les autres actus citées ;
  - on y prend la photo d'ouverture que le site déclare pour le partage (balises
    og:image / twitter:image) et la ligne de crédit de la photo (« Image Credits:
    OpenAI », « Photograph: … / Reuters ») ;
  - crédit d'une agence ou d'une banque d'images (Getty, Reuters, AP, AFP…), ou du
    média lui-même (« … / The Verge ») : photo refusée d'office. Les agences font
    retirer les posts, et des retraits répétés peuvent faire suspendre un compte ;
  - la photo est téléchargée (10 Mo et 30 s au plus, jamais depuis X ni depuis une
    adresse privée), vérifiée, remise en JPEG propre (2048 px au plus, sans
    métadonnées) ;
  - Claude la REGARDE, avec son crédit (`writer.photo_is_usable`) : ce dont parle le
    post, une photo ou un visuel officiel venant de l'entreprise ou de l'organisation
    de l'actu, sans logo ni titre de média, pas une capture de page. Au moindre
    doute : actu citée suivante, sinon le post part en texte seul.
Photo IA (OpenAI) : facultative, désactivée par défaut.
`prepare()` ne lève jamais d'exception : (None, None) = post en texte seul.
"""
from __future__ import annotations

import base64
import io
import ipaddress
import logging
import re
import socket
import time
import unicodedata
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urljoin, urlparse

from . import utils
from .filters import off_limits_term
from .x_api import contains_url

log = logging.getLogger("xbot.visuals")

PHOTO_KIND = "photo"
PHOTOS_KEEP = 60
GENERATED_KEEP = 50
MAX_PAGES_TRIED = 3               # actus citées essayées avant d'abandonner la photo
PHOTO_SEARCH_MAX_S = 150          # toutes les pages essayées : au-delà, le post part en texte seul
PHOTO_MAX_BYTES = 10 * 1024 * 1024
PHOTO_TIMEOUT_S = 15              # par requête
PHOTO_MAX_SECONDS = 30            # pour tout le téléchargement d'une photo
MAX_REDIRECTS = 5
PHOTO_MIN_WIDTH = 600             # px : en dessous, c'est une vignette
PHOTO_MIN_HEIGHT = 315
PHOTO_MIN_RATIO = 0.5             # largeur / hauteur : ni colonne…
PHOTO_MAX_RATIO = 2.4             # … ni bandeau
PHOTO_MAX_SIDE = 2048             # px : largement assez pour X, et le fichier reste léger
PHOTO_MAX_PIXELS = 60_000_000     # au-delà, image piégée ou inutilisable
JPEG_QUALITY = 90
HEAD_MAX_CHARS = 400_000          # les balises <meta> sont dans l'en-tête de la page
CREDIT_BEFORE_TITLE = 2500        # la ligne de crédit de la photo d'ouverture est près du titre…
CREDIT_AFTER_TITLE = 6000         # … juste avant (photo au-dessus du titre) ou juste après
PHOTO_ACCEPT = "image/webp,image/png,image/jpeg,image/gif,image/*;q=0.8"

# Jamais lus, jamais téléchargés : ni une page ni une photo de X.
X_DOMAINS = ("x.com", "twitter.com", "t.co", "twimg.com")

# Sites de presse : leur propre nom dans un crédit (« Photo by Jane Doe / The Verge »)
# veut dire une photo de leur équipe, pas de l'entreprise de l'actu.
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
    "washingtonpost.com", "wsj.com", "theinformation.com", "semafor.com", "theregister.com",
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
# Crédits qui désignent l'équipe ou les archives du média lui-même.
_STAFF_CREDITS = frozenset({"future", "future plc", "staff", "file", "file photo", "stock", "stock image",
                            "archive", "archives"})
# Image « par défaut » d'un site (son logo, une vignette de partage générique).
# (Le nom du FICHIER commence par ces mots : « openai-logo.jpg » reste permis, c'est Claude qui juge.)
_DEFAULT_IMAGE_RE = re.compile(r"(?:^|/)(?:logo|default|placeholder|fallback|og-default|share-default|"
                               r"social-default|site-image)[^/]*$", re.I)

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
# La page de l'actu : sa photo d'ouverture et la ligne de crédit de cette photo
# ---------------------------------------------------------------------------
@dataclass
class PagePhoto:
    image_url: str      # la photo d'ouverture déclarée par la page
    credit: str = ""    # sa ligne de crédit, quand la page en donne une
    site: str = ""      # le nom du site (og:site_name), ou son domaine


class _HeadMeta(HTMLParser):
    """Les balises <meta> d'une page (og:image, twitter:image, og:site_name, og:title)."""

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
    """La ligne de crédit de la photo d'ouverture, cherchée près du titre de l'article
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
    """Pourquoi la photo ne doit PAS être reprise d'après son crédit, ou None. Sans
    crédit, rien n'est décidé ici : c'est Claude qui juge sur l'image."""
    c = (credit or "").strip()
    if not c:
        return None
    if _AGENCY_RE.search(c) or _AP_RE.search(c):
        return f"photo d'agence ou de banque d'images ({c[:40]})"
    if c.lower().strip(" .") in _STAFF_CREDITS:
        return f"photo du média ({c[:40]})"
    if _host_in(host_of(page_url), PRESS_SITES):
        names = (site_name, site_of(page_url).split(".")[0])
        if any(_names_the_site(c, n) for n in names):
            return f"photo du média lui-même ({c[:40]})"
    return None


def find_story_photo(url: str, title: str = "", http_get: Callable | None = None) -> PagePhoto | None:
    """La photo d'ouverture d'une page d'actu et son crédit, ou None (page illisible,
    pas de photo, post X). Ne lève jamais."""
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")) or is_x_url(url):
        return None
    try:
        if http_get is None:
            from . import sources
            status, page = sources.default_http_get(url, accept=sources.HTML_ACCEPT)
        else:
            status, page = http_get(url)
    except Exception as e:  # noqa: BLE001 — réseau, délai, encodage
        log.info("Photo : page illisible (%s) : %s", type(e).__name__, host_of(url))
        return None
    if status != 200 or not page:
        log.info("Photo : page illisible (HTTP %s) : %s", status, host_of(url))
        return None
    meta = page_meta(page)
    image = lead_image_url(meta, url)
    if not image:
        log.info("Photo : la page ne déclare aucune photo (%s)", host_of(url))
        return None
    site = meta.get("og:site_name") or meta.get("application-name") or site_of(url)
    return PagePhoto(image_url=image, credit=photo_credit(page, title or meta.get("og:title", "")), site=site)


# ---------------------------------------------------------------------------
# Téléchargement et mise au propre
# ---------------------------------------------------------------------------
def _user_agent() -> str:
    from .sources import USER_AGENT
    return USER_AGENT


def fetch_image_bytes(url: str) -> bytes | None:
    """Télécharge une image : redirections suivies une à une (jamais vers X ni vers une
    adresse privée), 10 Mo et 30 s au plus, un vrai type d'image. None sinon."""
    import requests

    headers = {"User-Agent": _user_agent(), "Accept": PHOTO_ACCEPT}
    deadline = time.monotonic() + PHOTO_MAX_SECONDS
    for _hop in range(MAX_REDIRECTS + 1):
        host = host_of(url)
        if not host or _host_in(host, X_DOMAINS):
            log.info("Photo : adresse refusée (%s)", host or url[:60])
            return None
        if not public_host(host):
            log.info("Photo : adresse privée ou introuvable, refusée (%s)", host)
            return None
        with requests.get(url, timeout=PHOTO_TIMEOUT_S, headers=headers, stream=True,
                          allow_redirects=False) as resp:
            if resp.is_redirect:
                location = resp.headers.get("Location")
                if not location:
                    return None
                url = urljoin(url, location)
                continue
            if resp.status_code != 200:
                log.info("Photo : HTTP %s (%s)", resp.status_code, host)
                return None
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype and not (ctype.startswith("image/") or ctype == "application/octet-stream"):
                log.info("Photo : ce n'est pas une image (%s, %s)", ctype, host)
                return None
            declared = (resp.headers.get("Content-Length") or "").strip()
            if declared.isdigit() and int(declared) > PHOTO_MAX_BYTES:
                log.info("Photo : trop lourde (%s octets, %s)", declared, host)
                return None
            chunks: list[bytes] = []
            size = 0
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                size += len(chunk)
                if size > PHOTO_MAX_BYTES:
                    log.info("Photo : trop lourde (> %d Mo, %s)", PHOTO_MAX_BYTES // 2**20, host)
                    return None
                chunks.append(chunk)
                if time.monotonic() > deadline:
                    log.info("Photo : téléchargement trop lent (%s)", host)
                    return None
            return b"".join(chunks)
    log.info("Photo : trop de redirections")
    return None


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
# Quelle photo pour CE post
# ---------------------------------------------------------------------------
def photo_candidates(draft) -> list:
    """Les actus CITÉES dont on essaie la photo, dans l'ordre : celle que Claude a
    désignée, puis les autres (même histoire, autre site). Jamais X, jamais deux fois
    la même page."""
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


def _photo_visual(settings, draft, writer, tag: str, *, http_get: Callable | None = None,
                  fetch: Callable | None = None) -> tuple[str | None, str | None]:
    if not getattr(settings, "photos_enabled", False):
        log.info("Photo ignorée : PHOTOS_ENABLED=false")
        return None, None
    check = getattr(writer, "photo_is_usable", None)
    if not callable(check):
        log.info("Photo ignorée : aucun contrôle par Claude possible")
        return None, None
    texts = getattr(draft, "texts", None) or [""]
    first_text = str(texts[0] or "") if isinstance(texts, list) and texts else ""
    folder = utils.MEDIA_DIR / "photos"
    deadline = time.monotonic() + PHOTO_SEARCH_MAX_S
    for n, src in enumerate(photo_candidates(draft), start=1):
        if time.monotonic() > deadline:
            log.info("Photo : recherche trop longue, le post part en texte seul")
            break
        url = str(src.url)
        title = str(getattr(src, "title", "") or "")
        official = bool(getattr(src, "official", False))
        found = find_story_photo(url, title, http_get=http_get)
        if not found:
            continue
        # Même sur la page de l'entreprise : une photo d'agence reste une photo d'agence.
        why = credit_problem(found.credit, found.site, url)
        if why:
            log.info("Photo écartée (%s) : %s", why, url[:100])
            continue
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = download_photo(found.image_url, folder / f"{tag}{'-' + str(n) if n > 1 else ''}_{stamp}.jpg",
                              fetch=fetch)
        if not path:
            continue
        # Claude REGARDE la photo ; sans réponse claire, elle n'est jamais utilisée.
        try:
            ok, issue = check(path, headline=title, post=first_text, credit=found.credit, official=official)
        except Exception as e:  # noqa: BLE001
            ok, issue = False, f"vérification impossible ({type(e).__name__})"
        if ok is True:
            log.info("Photo prête : %s (%s, crédit : %s)", path.name, url[:100], found.credit or "aucun")
            _cleanup_old(folder, PHOTOS_KEEP)
            return str(path), PHOTO_KIND
        log.info("Photo écartée (%s) : %s", issue or "pas utilisable", url[:100])
        _safe_unlink(path)
    return None, None


def self_test() -> tuple[bool, str]:
    """Le serveur sait-il mettre une photo au propre ? Une image d'essai est fabriquée
    sur place (aucun réseau) puis passée par la même mise au propre (`--check`)."""
    import tempfile
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
    """(chemin de l'image, "photo" | "ai") ou (None, None). Ne lève jamais."""
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
        return _photo_visual(settings, draft, writer, tag)
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
