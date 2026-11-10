"""Visuels des posts : carte dessinée, vraie page vue dans un smartphone, ou photo IA.

Trois sortes d'images (voir bot/cards.py pour le dessin) :
  - « card »  : une carte faite maison (prompt à copier, étapes, liste, fait).
    Aucun réseau, toujours propre : c'est le visuel par défaut ;
  - « phone » : la page de l'actu citée, chargée comme sur un téléphone
    (largeur 390 px), cadrée sur son titre, puis posée dans une maquette de
    smartphone. Si la page n'est pas impeccable (bandeau, mur, titre coupé…),
    on retombe sur une carte : jamais de capture ratée publiée ;
  - « ai »    : photo générée (OpenAI), désactivée par défaut.

Règles gardées de la v2 :
  - JAMAIS de capture de x.com / twitter.com ; les requêtes vers les domaines
    de X sont même bloquées dans la page capturée ;
  - Chromium headless À PART, jetable : aucun compte, aucune session, aucun
    cookie gardé ;
  - page d'erreur / vide / mur de connexion / redirection hors des sites
    autorisés → pas de capture ;
  - Claude REGARDE la capture avant qu'elle serve (`writer.screenshot_is_clean`) ;
  - délai dur : capture et rendu tournent dans un thread de travail borné ; la
    boucle principale ne reste jamais figée.

`prepare()` ne lève jamais d'exception : (None, None) = post en texte seul.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from . import cards, utils
from .filters import off_limits_term
from .x_api import contains_url

log = logging.getLogger("xbot.visuals")

CAPTURE_TIMEOUT_S = 90
SHOTS_KEEP = 40
CARDS_KEEP = 80
GENERATED_KEEP = 50
VISIBLE_TEXT_CHARS = 1500
# La page est chargée comme sur un iPhone (mise en page mobile : une colonne,
# gros titre), en très haute définition pour rester nette dans la maquette.
MOBILE_USER_AGENT = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 "
                     "(KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1")
MOBILE_VIEWPORT_HEIGHT = 720
MOBILE_SCALE = 4
HEADLINE_MUST_END_BEFORE = 300    # px CSS : le titre entier doit tenir dans la partie montrée

# Sites de confiance (sous-domaines acceptés), SANS x.com / twitter.com. La page
# capturée est toujours celle d'une actu citée ; cette liste sert quand le lien
# de l'actu redirige vers un autre site (flux, raccourcisseur).
ALLOWED_DOMAINS = (
    # Presse tech / business / généraliste
    "techcrunch.com", "theverge.com", "arstechnica.com", "wired.com",
    "reuters.com", "bloomberg.com", "ft.com", "cnbc.com", "apnews.com",
    "forbes.com", "businessinsider.com",
    # Presse grand public / pratique
    "zdnet.com", "cnet.com", "engadget.com", "theguardian.com", "technologyreview.com",
    "tomsguide.com", "techradar.com", "mashable.com", "9to5mac.com", "9to5google.com",
    "macrumors.com", "androidauthority.com", "venturebeat.com", "axios.com", "fastcompany.com",
    "time.com", "npr.org", "pcmag.com", "lifehacker.com", "howtogeek.com",
    # Sport / mainstream
    "espn.com", "bbc.com", "bbc.co.uk", "skysports.com", "goal.com",
    "transfermarkt.com", "sofascore.com", "flashscore.com", "uefa.com",
    "fifa.com", "nba.com", "formula1.com",
    # IA / tech officiel
    "openai.com", "anthropic.com", "huggingface.co", "github.com",
    "chatgpt.com", "claude.com", "gemini.google.com", "perplexity.ai",
    "blog.google", "deepmind.google", "nvidia.com", "apple.com", "microsoft.com",
    "producthunt.com", "ycombinator.com", "about.fb.com", "meta.com", "mistral.ai",
    "elevenlabs.io", "canva.com", "notion.com", "duolingo.com",
    # Science / culture
    "nasa.gov", "imdb.com", "rottentomatoes.com",
)
# Jamais capturés, et bloqués à l'intérieur des pages capturées.
X_DOMAINS = ("x.com", "twitter.com", "t.co", "twimg.com")
_X_REQUEST_RE = re.compile(r"^[a-z]+://([^/?#]*\.)?(x\.com|twitter\.com|t\.co|twimg\.com)(:\d+)?([/?#]|$)", re.I)

# Bandeaux à fermer, toutes langues confondues
_DISMISS_SELECTORS = (
    'button:has-text("Accept all")', 'button:has-text("Accept All")',
    'button:has-text("Accept")', 'button:has-text("I agree")',
    'button:has-text("Tout accepter")', 'button:has-text("Accepter")',
    'button:has-text("Got it")', 'button:has-text("OK")',
    'button:has-text("Close")', 'button[aria-label="Close"]',
    'button[aria-label="Dismiss"]', '[data-testid="close-button"]',
    '#onetrust-accept-btn-handler', '.cc-btn.cc-dismiss',
)

# Nettoyage de la page : on masque ce qui « flotte » (menus fixes, bandeaux
# collants, pop-ups) et les barres de navigation, puis on repère le titre.
_PREPARE_JS = """() => {
  const hide = el => el.style.setProperty('display', 'none', 'important');
  const visible = e => {
    const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
    return r.width > 150 && r.height > 18 && cs.visibility !== 'hidden' && cs.display !== 'none'
      && parseFloat(cs.opacity || '1') > 0.1 && (e.innerText || '').trim().length > 8;
  };
  // Le titre d'abord : sur certains sites il est lui-même dans un bloc « collant ».
  let h = null;
  for (const sel of ['article h1', 'main h1', '[role="main"] h1', 'h1']) {
    h = [...document.querySelectorAll(sel)].find(visible);
    if (h) break;
  }
  if (!h) return false;
  // Certains murs de cookies figent la page (<body> en position fixe, défilement bloqué).
  for (const el of [document.documentElement, document.body]) {
    el.style.setProperty('overflow', 'visible', 'important');
    if (getComputedStyle(el).position === 'fixed') el.style.setProperty('position', 'static', 'important');
  }
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed' && cs.position !== 'sticky') continue;
    if (el.contains(h)) el.style.setProperty('position', 'static', 'important');  // on le « décolle »
    else hide(el);
  }
  // Bandeaux cookies, pop-ups, newsletters, chats (jamais la page ni le bloc du titre).
  for (const el of document.querySelectorAll("[class*='cookie'],[id*='cookie'],[class*='consent'],"
      + "[id*='consent'],[class*='newsletter'],[class*='popup'],[class*='modal'],iframe[src*='chat'],"
      + "[class*='intercom'],[class*='drift'],[id^='sp_message'],#onetrust-consent-sdk,#didomi-host,"
      + "#usercentrics-root,#CybotCookiebotDialog,.fc-consent-root,.qc-cmp2-container,#cmpbox,"
      + "[class*='paywall'],[class*='app-banner'],[class*='smartbanner']")) {
    if (el === document.body || el === document.documentElement || el.contains(h)) continue;
    hide(el);
  }
  // Menus du site (jamais le bloc qui contient le titre).
  for (const el of document.querySelectorAll('nav, [role="navigation"], [role="banner"], header, '
      + '[class*="navbar"], [class*="site-header"], [class*="SiteHeader"], [id*="header"]')) {
    if (!el.contains(h) && !h.contains(el)) hide(el);
  }
  window.__xbotHeadline = h;
  const y = h.getBoundingClientRect().top + window.scrollY;
  window.scrollTo(0, Math.max(0, y - 80));
  return true;
}"""

# Cadrage : le titre en haut de l'écran, avec la petite ligne qui le précède
# (rubrique, date) si elle existe, sans jamais trancher une ligne de texte.
_ALIGN_JS = """() => {
  const h = window.__xbotHeadline;
  if (!h) return null;
  let r = h.getBoundingClientRect();
  let top = r.top - 22;
  for (const el of document.querySelectorAll('a, span, p, time, div, li, strong')) {
    if (el.contains(h) || h.contains(el)) continue;
    const own = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length > 1);
    if (!own) continue;
    const b = el.getBoundingClientRect(), cs = getComputedStyle(el);
    if (b.width <= 0 || b.height <= 0 || b.height > 44) continue;
    if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity || '1') < 0.1) continue;
    // Une vraie « ligne au-dessus du titre » est alignée sur lui (à gauche, ou centrée comme lui) :
    // un bouton ou un logo de l'en-tête du site ne l'est pas.
    const aligned = Math.abs(b.left - r.left) < 60
      || Math.abs((b.left + b.right) / 2 - (r.left + r.right) / 2) < 40;
    if (aligned && b.bottom <= r.top + 2 && b.bottom > r.top - 72) top = Math.min(top, b.top - 14);
  }
  window.scrollBy(0, Math.max(top, r.top - 120));
  // Ce qui dépasse encore par le haut (logo du site, bouton) serait tranché : on le masque.
  for (const el of document.querySelectorAll('body *')) {
    if (el.contains(h)) continue;
    const b = el.getBoundingClientRect();
    if (b.width > 0 && b.height > 0 && b.top < -1 && b.bottom > 1 && b.bottom < 150)
      el.style.setProperty('visibility', 'hidden', 'important');
  }
  r = h.getBoundingClientRect();
  const doc = document.documentElement;
  return {top: r.top, bottom: r.bottom, left: r.left, right: r.right, vw: doc.clientWidth,
          pageWidth: doc.scrollWidth};
}"""


def headline_fits(m: dict | None) -> str | None:
    """Une raison si le titre mesuré ne sera pas entier et bien placé dans le
    téléphone, None si le cadrage est bon."""
    try:
        top, bottom, left, right, vw = (float(m[k]) for k in ("top", "bottom", "left", "right", "vw"))
    except (KeyError, TypeError, ValueError):
        return "titre non mesurable"
    if bottom <= top or vw <= 0:
        return "titre non mesurable"
    if top < -2:
        return "titre coupé en haut"
    if bottom > HEADLINE_MUST_END_BEFORE:
        return "titre trop long pour l'écran"
    if left < -2 or right > vw + 2:
        return "titre plus large que l'écran"
    return None


def _browser_launch_options() -> tuple[dict, dict]:
    """Options du navigateur pour la capture mobile (voir cards.launch_options
    pour XBOT_BROWSER_PATH / XBOT_BROWSER_PROXY, réservés aux tests)."""
    context: dict = {"viewport": {"width": cards.PHONE_PAGE_WIDTH, "height": MOBILE_VIEWPORT_HEIGHT},
                     "device_scale_factor": MOBILE_SCALE, "is_mobile": True, "has_touch": True,
                     "user_agent": MOBILE_USER_AGENT, "locale": "en-US", "accept_downloads": False}
    if os.getenv("XBOT_BROWSER_PROXY"):
        context["ignore_https_errors"] = True
    return cards.launch_options(), context


# Signatures de pages d'erreur / vides, toutes langues (v1). Phrases complètes,
# non ambiguës : dans le titre OU en haut du texte visible → page cassée.
_ERROR_PHRASES = (
    "doesn't exist", "doesn’t exist", "does not exist", "page not found",
    "something went wrong", "try searching for something else",
    "this account doesn't exist", "account suspended", "this post is unavailable",
    "post unavailable", "hmm...this page", "hmm…this page", "access denied",
    "page introuvable", "n'existe pas", "n’existe pas", "quelque chose s'est mal passé",
    "aucun résultat", "rate limit exceeded", "too many requests", "please log in",
    "sign in to x", "log in to x", "enable javascript", "checking your browser",
    "just a moment", "are you a robot", "verify you are human",
    "subscribe to continue", "subscribe to read", "create a free account to continue",
)
# Mots courts / ambigus : comptent UNIQUEMENT dans le titre (un article peut
# contenir « error » ou « 404 » dans son texte sans être cassé).
_ERROR_TITLE_WORDS = (
    "404", "not found", "error", "oops", "forbidden", "unavailable", "captcha",
    "cloudflare", "une erreur", "réessayer", "no results",
)

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
# URL
# ---------------------------------------------------------------------------
def host_of(url: str) -> str:
    try:
        u = urlparse((url or "").strip())
    except ValueError:
        return ""
    if u.scheme not in ("http", "https") or not u.netloc:
        return ""
    return u.netloc.lower().rsplit("@", 1)[-1].split(":")[0].rstrip(".")


def _host_in(host: str, domains: tuple[str, ...]) -> bool:
    return bool(host) and any(host == d or host.endswith("." + d) for d in domains)


def is_x_url(url: str) -> bool:
    return _host_in(host_of(url), X_DOMAINS)


def is_allowed_url(url: str) -> bool:
    """http(s), jamais X, et un site de la liste autorisée."""
    host = host_of(url)
    if not host or _host_in(host, X_DOMAINS):
        return False
    return _host_in(host, ALLOWED_DOMAINS)


_TWO_LEVEL_SUFFIXES = (".co.uk", ".org.uk", ".com.au", ".co.jp", ".co.in", ".com.br", ".co.nz", ".co.za")


def site_of(url: str) -> str:
    """« theverge.com » pour https://www.theverge.com/… (domaine du site, sans sous-domaine)."""
    host = host_of(url)
    labels = host.split(".")
    keep = 3 if host.endswith(_TWO_LEVEL_SUFFIXES) else 2
    return ".".join(labels[-keep:]) if len(labels) >= keep else host


def final_url_ok(final_url: str, requested_url: str) -> bool:
    """La page réellement affichée est-elle bien celle qu'on voulait montrer ?
    Même site que le lien demandé, ou un site de confiance (lien de flux qui
    redirige) ; jamais X."""
    if is_x_url(final_url) or not host_of(final_url):
        return False
    return site_of(final_url) == site_of(requested_url) or is_allowed_url(final_url)


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
# Page (fonctions sur un objet `page` Playwright)
# ---------------------------------------------------------------------------
def _title_and_body(page) -> tuple[str, str]:
    try:
        title = (page.title() or "").strip()
    except Exception:  # noqa: BLE001
        title = ""
    try:
        body = page.evaluate("() => (document.body && document.body.innerText) || ''") or ""
    except Exception:  # noqa: BLE001
        body = ""
    return title, str(body)


def page_looks_broken(title: str, body: str) -> str | None:
    """Une raison si la page ressemble à une erreur / page vide / mur de
    connexion, None si elle a l'air normale."""
    title = (title or "").strip().lower()
    body = (body or "").strip().lower()
    if len(body) < 80:
        return f"page quasi vide ({len(body)} caractères)"
    head = body[:600]
    for sig in _ERROR_PHRASES:
        if sig in title:
            return f"titre d'erreur : {title[:60]!r}"
        if sig in head:
            return f"signature d'erreur en haut de page : {sig!r}"
    for w in _ERROR_TITLE_WORDS:
        if w in title:
            return f"titre d'erreur : {title[:60]!r}"
    return None


def visible_text(title: str, body: str, max_chars: int = VISIBLE_TEXT_CHARS) -> str:
    """Titre + début du texte visible, pour la vérification par Claude."""
    body = re.sub(r"\s+", " ", body or "").strip()
    return f"{(title or '').strip()}\n{body[:max_chars]}".strip()


# ---------------------------------------------------------------------------
# Capture (Chromium headless jetable)
# ---------------------------------------------------------------------------
class _Cancelled(Exception):
    pass


def _capture_in_browser(url: str, tag: str, cancel: threading.Event) -> tuple[Path, str] | None:
    """Charge `url` comme sur un téléphone dans un Chromium headless neuf (aucune
    session), vérifie la page, ferme les bandeaux, cadre sur le titre et capture
    le haut de l'écran. (chemin JPEG, texte visible) ou None. Tourne dans le thread
    de travail de `capture_with_timeout`."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Capture impossible : Playwright n'est pas installé "
                    "(pip install playwright && python -m playwright install chromium)")
        return None

    def check() -> None:
        if cancel.is_set():
            raise _Cancelled()

    folder = utils.MEDIA_DIR / "shots"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    raw_path = folder / f"{tag}_{stamp}_mobile.jpg"
    try:
        with sync_playwright() as pw:
            launch_opts, context_opts = _browser_launch_options()
            browser = pw.chromium.launch(**launch_opts)
            try:
                context = browser.new_context(**context_opts)
                # Aucun appel aux serveurs de X, même pour un tweet intégré dans un article.
                context.route(_X_REQUEST_RE, lambda route: route.abort())
                page = context.new_page()
                page.set_default_timeout(15000)
                check()
                resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                try:
                    status = resp.status if resp is not None else None
                except Exception:  # noqa: BLE001
                    status = None
                if status is not None and status >= 400:
                    log.warning("Capture refusée : HTTP %s sur %s", status, url)
                    return None
                final_url = page.url or url
                if not final_url_ok(final_url, url):
                    log.warning("Capture refusée : le lien mène sur un autre site (%s → %s)",
                                url, host_of(final_url) or final_url[:80])
                    return None
                check()
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(2.0)
                check()
                broken = page_looks_broken(*_title_and_body(page))
                if broken:
                    log.warning("Capture refusée : %s — %s", broken, url)
                    return None
                for sel in _DISMISS_SELECTORS:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible():
                            btn.click(timeout=2000)
                            time.sleep(0.5)
                    except Exception:  # noqa: BLE001
                        continue
                # (Les bandeaux restants sont masqués par _PREPARE_JS, sans jamais toucher à
                # la page elle-même : sur certains sites, <body> a une classe « modal… ».)
                time.sleep(0.8)
                check()
                # Certains sites ne révèlent leur 404 / mur qu'après le bandeau cookies.
                title, body = _title_and_body(page)
                broken = page_looks_broken(title, body)
                if broken:
                    log.warning("Capture refusée (après bandeaux) : %s — %s", broken, url)
                    return None
                if not final_url_ok(page.url or url, url):
                    log.warning("Capture refusée : la page a changé de site (%s)", url)
                    return None
                text = visible_text(title, body)
                # Cadrage sur le titre de l'article ; sans titre repérable ou bien placé, pas
                # de capture (une carte dessinée prendra le relais).
                if not page.evaluate(_PREPARE_JS):
                    log.warning("Capture refusée : aucun titre d'article repérable sur %s", url)
                    return None
                time.sleep(0.6)
                problem = headline_fits(page.evaluate(_ALIGN_JS))
                if problem:
                    log.warning("Capture refusée : %s sur %s", problem, url)
                    return None
                time.sleep(1.5)  # images chargées à l'arrivée dans l'écran
                check()
                page.screenshot(path=str(raw_path), type="jpeg", quality=90,
                                clip={"x": 0, "y": 0, "width": cards.PHONE_PAGE_WIDTH,
                                      "height": cards.PHONE_PAGE_VISIBLE_HEIGHT})
            finally:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
    except _Cancelled:
        _safe_unlink(raw_path)
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("Capture échouée (%s) : %s", url, str(e).splitlines()[0][:200] if str(e) else type(e).__name__)
        _safe_unlink(raw_path)
        return None

    if not raw_path.is_file():
        return None
    _cleanup_old(folder, SHOTS_KEEP)
    log.info("Capture mobile OK : %s → %s", url, raw_path.name)
    return raw_path, text


def capture_with_timeout(url: str, tag: str = "post",
                         timeout_s: float = CAPTURE_TIMEOUT_S) -> tuple[Path, str] | None:
    """Capture dans un thread de travail, abandonnée après `timeout_s` (le
    thread ferme alors son navigateur dès qu'il reprend la main)."""
    result: dict[str, tuple[Path, str] | None] = {}
    cancel = threading.Event()

    def work() -> None:
        try:
            res = _capture_in_browser(url, tag, cancel)
        except Exception as e:  # noqa: BLE001
            log.warning("Capture : erreur inattendue (%s)", type(e).__name__)
            res = None
        if res and cancel.is_set():
            _safe_unlink(res[0])  # arrivée trop tard : personne ne l'attend plus
            res = None
        result["value"] = res

    worker = threading.Thread(target=work, daemon=True, name="page-shot")
    with utils.heartbeat("capture de page", every_s=30.0, max_total_s=timeout_s + 60):
        worker.start()
        worker.join(timeout_s)
    if worker.is_alive():
        cancel.set()
        log.warning("Capture abandonnée : plus de %.0f s pour %s", timeout_s, url)
        return None
    return result.get("value")


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
# Cartes (bot/cards.py)
# ---------------------------------------------------------------------------
BADGES = {"tip": "Everyday AI", "news": "AI news, made simple", "reactive": "AI news, made simple",
          "thread": "Everyday AI", "gm": "Good morning"}
THREAD_BADGES = {"tutorial": "Step by step", "timely": "AI news, made simple", "weekly": "This week in AI"}


def _next_card_number(writer, key: str) -> int:
    """Compteur gardé dans l'état du bot (palette qui tourne, numéro de série des astuces)."""
    state = getattr(writer, "state", None)
    data = getattr(state, "data", None)
    if not isinstance(data, dict):
        return 0
    counters = data.get("cards")
    if not isinstance(counters, dict):
        counters = data["cards"] = {}
    counters[key] = int(counters.get(key) or 0) + 1
    try:
        state.flush()
    except Exception:  # noqa: BLE001
        pass
    return counters[key]


def card_badge(draft, writer) -> str:
    """Petite étiquette en haut de la carte. Les astuces sont numérotées : une
    série qu'on reconnaît et qu'on a envie de suivre."""
    kind = str(getattr(draft, "kind", "") or "")
    if kind == "thread":
        return THREAD_BADGES.get(str(getattr(draft, "angle", "") or ""), BADGES["thread"])
    badge = BADGES.get(kind, "AI, made simple")
    # Une carte d'actu sur un créneau « astuce » (ou l'inverse) garde l'étiquette de son contenu.
    if kind == "tip" and getattr(draft, "sources", None):
        return BADGES["news"]
    if kind == "news" and not getattr(draft, "sources", None):
        badge = BADGES["tip"]
    if badge == BADGES["tip"]:
        number = _next_card_number(writer, "tips")
        if number > 0:
            badge += f" · No. {number}"
    return badge


def _fallback_card(card: dict) -> dict | None:
    """Carte dessinée à la place d'une capture ratée (mêmes titre et lignes)."""
    lines = [x for x in (card.get("lines") or []) if str(x).strip()]
    if not str(card.get("title") or "").strip():
        return None
    return dict(card, type="list" if len(lines) >= 2 else "fact", lines=lines)


def _card_visual(settings, draft, writer, tag: str) -> tuple[str | None, str | None]:
    card = dict(getattr(draft, "card", None) or {})
    if card.get("type") not in cards.CARD_TYPES:
        return None, None
    handle = str(getattr(settings, "handle", "") or getattr(settings, "x_handle", "") or "").lstrip("@")
    theme = cards.theme_for(_next_card_number(writer, "seq"))
    folder = utils.MEDIA_DIR / "cards"
    out_path = folder / f"{tag}_{time.strftime('%Y%m%d-%H%M%S')}.png"
    badge = card_badge(draft, writer)

    if card["type"] == "phone":
        visual = getattr(draft, "visual", None)
        target = str(getattr(visual, "target", "") or "").strip()
        shot = None
        if target and getattr(settings, "screenshots_enabled", False) and _capturable(draft, target):
            shot = capture_with_timeout(target, tag)
        if shot:
            raw_path, _text = shot
            # Claude REGARDE la capture ; sans relecteur, elle n'est jamais utilisée à l'aveugle.
            clean, issue = False, "aucun contrôle visuel possible"
            check = getattr(writer, "screenshot_is_clean", None)
            if callable(check):
                try:
                    clean, issue = check(raw_path)
                except Exception as e:  # noqa: BLE001
                    clean, issue = False, f"vérification impossible ({type(e).__name__})"
            if clean and cards.render(card, out_path, theme=theme, badge=badge, handle=handle,
                                      shot_path=raw_path, source=host_of(target).removeprefix("www.")):
                _safe_unlink(raw_path)
                _cleanup_old(folder, CARDS_KEEP)
                log.info("Visuel « smartphone » prêt : %s (%s)", out_path.name, target[:100])
                return str(out_path), "phone"
            log.info("Capture écartée (%s) : carte dessinée à la place", issue or "rendu impossible")
            _safe_unlink(raw_path)
        fallback = _fallback_card(card)
        if fallback is None:
            return None, None
        card = fallback

    if cards.render(card, out_path, theme=theme, badge=badge, handle=handle):
        _cleanup_old(folder, CARDS_KEEP)
        log.info("Carte prête : %s (%s, palette %s)", out_path.name, card["type"], theme)
        return str(out_path), "card"
    return None, None


def _capturable(draft, target: str) -> bool:
    """Page d'une actu citée par le post, ou site de la liste ; jamais X."""
    if is_x_url(target) or urlparse(target).scheme not in ("http", "https"):
        return False
    cited = {str(getattr(src, "url", "") or "").strip().rstrip("/").lower()
             for src in (getattr(draft, "sources", None) or [])}
    return target.rstrip("/").lower() in cited or is_allowed_url(target)


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------
def prepare(settings, draft, writer, tag: str = "post") -> tuple[str | None, str | None]:
    """(chemin de l'image, "card" | "phone" | "ai") ou (None, None). Ne lève jamais."""
    try:
        return _prepare(settings, draft, writer, _safe_tag(tag))
    except Exception as e:  # noqa: BLE001
        log.exception("Visuel : erreur inattendue (%s), post en texte seul", e)
        return None, None


def _prepare(settings, draft, writer, tag: str) -> tuple[str | None, str | None]:
    if getattr(draft, "poll", None):
        log.info("Visuel ignoré : X n'accepte pas d'image sur un sondage")
        return None, None
    if getattr(draft, "card", None):
        path, kind = _card_visual(settings, draft, writer, tag)
        if path:
            return path, kind
    visual = getattr(draft, "visual", None)
    if visual is None:
        return None, None
    kind = str(getattr(visual, "kind", "") or "").strip().lower()
    target = str(getattr(visual, "target", "") or "").strip()
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
