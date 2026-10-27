"""Visuels des posts : capture d'une VRAIE page web ou photo IA (optionnelle).

Repris de la v1 (page_shot.py, image_gen.py) avec ces changements :
  - JAMAIS de capture de x.com / twitter.com (plus aucun navigateur sur X en
    v2 : « Non-API automation … results in permanent suspension ») ; les
    requêtes vers les domaines de X sont même bloquées dans la page capturée
    (tweets intégrés dans un article) ;
  - Chromium headless À PART, jetable : aucun compte, aucune session
    enregistrée, aucun cookie gardé ; la page est chargée comme par n'importe
    quel visiteur ;
  - page d'erreur / vide / mur de connexion / redirection hors des sites
    autorisés → pas d'image ;
  - Claude vérifie que la page illustre vraiment le post
    (`writer.visual_matches`), sinon l'image est supprimée ;
  - délai dur (~90 s) : la capture tourne dans un thread de travail attendu
    avec join(timeout) ; la boucle principale ne reste jamais figée.

`prepare()` ne lève jamais d'exception : (None, None) = post en texte seul.
"""
from __future__ import annotations

import base64
import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from . import utils
from .filters import off_limits_term
from .x_api import contains_url

log = logging.getLogger("xbot.visuals")

CAPTURE_TIMEOUT_S = 90
SHOTS_KEEP = 40
GENERATED_KEEP = 50
VISIBLE_TEXT_CHARS = 1500

# Sites autorisés (liste v1 page_shot.ALLOWED_DOMAINS) SANS x.com / twitter.com.
# Sous-domaines acceptés.
ALLOWED_DOMAINS = (
    # Presse tech / business / généraliste
    "techcrunch.com", "theverge.com", "arstechnica.com", "wired.com",
    "reuters.com", "bloomberg.com", "ft.com", "cnbc.com", "apnews.com",
    "forbes.com", "businessinsider.com",
    # Sport / mainstream
    "espn.com", "bbc.com", "bbc.co.uk", "skysports.com", "goal.com",
    "transfermarkt.com", "sofascore.com", "flashscore.com", "uefa.com",
    "fifa.com", "nba.com", "formula1.com",
    # IA / tech officiel
    "openai.com", "anthropic.com", "huggingface.co", "github.com",
    "chatgpt.com", "claude.com", "gemini.google.com", "perplexity.ai",
    "blog.google", "deepmind.google", "nvidia.com", "apple.com", "microsoft.com",
    "producthunt.com", "ycombinator.com",
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

# Recadrage : on masque ce qui « flotte » (menus fixes, bandeaux collants) et les
# barres de navigation, on repère le titre de l'article et on le place en haut.
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
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed' && cs.position !== 'sticky') continue;
    if (el.contains(h)) el.style.setProperty('position', 'static', 'important');  // on le « décolle »
    else hide(el);
  }
  // Bandeaux cookies, pop-ups, newsletters, chats (jamais la page ni le bloc du titre).
  for (const el of document.querySelectorAll("[class*='cookie'],[id*='cookie'],[class*='consent'],"
      + "[id*='consent'],[class*='newsletter'],[class*='popup'],[class*='modal'],iframe[src*='chat'],"
      + "[class*='intercom'],[class*='drift']")) {
    if (el === document.body || el === document.documentElement || el.contains(h)) continue;
    hide(el);
  }
  for (const el of [document.documentElement, document.body]) el.style.setProperty('overflow', 'visible', 'important');
  // Menus du site (jamais le bloc qui contient le titre).
  for (const el of document.querySelectorAll('nav, [role="navigation"], [role="banner"], header, '
      + '[class*="navbar"], [class*="site-header"], [class*="SiteHeader"], [id*="header"]')) {
    if (!el.contains(h) && !h.contains(el)) hide(el);
  }
  window.__xbotHeadline = h;
  const y = h.getBoundingClientRect().top + window.scrollY;
  window.scrollTo(0, Math.max(0, y - 64));
  return true;
}"""

# Position du titre + lignes de texte proches du bas de la zone (pour ne pas en couper une).
_MEASURE_JS = """() => {
  const h = window.__xbotHeadline;
  if (!h) return null;
  const r = h.getBoundingClientRect();
  const vw = document.documentElement.clientWidth, vh = window.innerHeight;
  const lines = [];
  const limit = r.top + vw * 9 / 16 + 80;
  for (const el of document.querySelectorAll('p, li, span, a, time, h2, h3, h4, figcaption, blockquote, div, button')) {
    const own = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length > 1);
    if (!own) continue;
    const b = el.getBoundingClientRect();
    if (b.height <= 0 || b.width <= 0 || b.bottom < r.top - 80 || b.top > limit) continue;
    lines.push([b.top, b.bottom]);
    if (lines.length > 400) break;
  }
  return {top: r.top, bottom: r.bottom, vw: vw, vh: vh, lines: lines};
}"""

CLIP_MIN_HEIGHT_BELOW_TITLE = 60   # px CSS sous le titre, au minimum


def compute_clip(m: dict) -> dict | None:
    """Zone à capturer : toute la largeur de la page (rien de coupé sur les côtés),
    du dessus du titre jusqu'à 16:9 au plus, en remontant le bas pour ne trancher
    aucune ligne de texte. None si la mesure est absurde."""
    try:
        vw, vh = float(m["vw"]), float(m["vh"])
        top, bottom = float(m["top"]), float(m["bottom"])
    except (KeyError, TypeError, ValueError):
        return None
    if vw <= 0 or vh <= 0 or bottom <= top:
        return None
    y = max(0.0, top - 32)
    lines = m.get("lines") or []
    # Une ligne tranchée par le haut (ex. « < Retour aux actus ») : on descend sous elle.
    for _ in range(20):
        cut_top = [lb for lt, lb in lines if lt < y - 2 and lb > y + 2 and lb <= top - 2]
        if not cut_top:
            break
        y = max(cut_top) + 6
    h = min(vw * 9 / 16, vh - y)
    end = y + h
    floor = bottom + CLIP_MIN_HEIGHT_BELOW_TITLE
    for _ in range(50):  # remonter tant qu'une ligne est coupée par le bas
        cut = [lt for lt, lb in (m.get("lines") or []) if lt < end - 2 and lb > end + 2]
        if not cut:
            break
        new_end = min(cut) - 8
        if new_end <= floor or new_end >= end:
            break
        end = new_end
    if end - y < (bottom - y) + 20:
        return None
    return {"x": 0, "y": round(y), "width": round(vw), "height": round(end - y)}


def _browser_launch_options() -> tuple[dict, dict]:
    """Options du navigateur. XBOT_BROWSER_PATH / XBOT_BROWSER_PROXY : seulement pour
    les environnements de test (navigateur préinstallé, proxy) ; vides en production."""
    import os
    launch: dict = {"headless": True, "args": ["--disable-dev-shm-usage", "--no-first-run", "--disable-extensions"]}
    # 1100 px : mise en page sur une seule colonne pour la plupart des sites (capture plus nette).
    context: dict = {"viewport": {"width": 1100, "height": 900}, "device_scale_factor": 2, "locale": "en-US",
                     "accept_downloads": False}
    if os.getenv("XBOT_BROWSER_PATH"):
        launch["executable_path"] = os.environ["XBOT_BROWSER_PATH"]
    if os.getenv("XBOT_BROWSER_PROXY"):
        launch["proxy"] = {"server": os.environ["XBOT_BROWSER_PROXY"]}
        context["ignore_https_errors"] = True
    return launch, context


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


def frame_image(raw_path: Path, out_path: Path, source: str) -> bool:
    """Cadre sobre (fond sombre, coins arrondis, « source: domaine » en bas).
    Si Pillow manque ou plante, on garde la capture brute (False)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    try:
        img = Image.open(raw_path).convert("RGB")
        # Image finale exactement en 16:9 : X l'affiche entière dans le fil, sans recadrer.
        cw, ch = 1600, 900
        pad, bar = 32, 48
        box_w, box_h = cw - 2 * pad, ch - 2 * pad - bar
        scale = min(box_w / img.width, box_h / img.height)
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
        w, h = img.size
        canvas = Image.new("RGB", (cw, ch), (16, 18, 24))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w, h), radius=18, fill=255)
        x0 = (cw - w) // 2
        y0 = max(pad, (ch - (h + bar)) // 2)   # bloc image + « source » centré verticalement
        canvas.paste(img, (x0, y0), mask)
        d = ImageDraw.Draw(canvas)
        font = None
        for candidate in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                          "/Library/Fonts/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"):
            try:
                font = ImageFont.truetype(candidate, 22)
                break
            except Exception:  # noqa: BLE001
                continue
        if font is None:
            try:
                font = ImageFont.load_default(size=22)
            except TypeError:  # Pillow ancien
                font = ImageFont.load_default()
        d.text((x0, y0 + h + 12), f"source: {source}", fill=(150, 156, 170), font=font)
        canvas.save(out_path, "PNG", optimize=True)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("Cadre de la capture impossible (%s), capture brute conservée", type(e).__name__)
        return False


# ---------------------------------------------------------------------------
# Capture (Chromium headless jetable)
# ---------------------------------------------------------------------------
class _Cancelled(Exception):
    pass


def _capture_in_browser(url: str, tag: str, cancel: threading.Event) -> tuple[Path, str] | None:
    """Charge `url` dans un Chromium headless neuf (aucune session), vérifie la
    page, ferme les bandeaux, capture et encadre. (chemin PNG, texte visible)
    ou None. Tourne dans le thread de travail de `capture_with_timeout`."""
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
    raw_path = folder / f"{tag}_{stamp}_raw.png"
    out_path = folder / f"{tag}_{stamp}.png"
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
                if not is_allowed_url(final_url):
                    log.warning("Capture refusée : redirection hors des sites autorisés (%s → %s)",
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
                if not is_allowed_url(page.url or url):
                    log.warning("Capture refusée : la page a quitté les sites autorisés (%s)", url)
                    return None
                text = visible_text(title, body)
                # Recadrage autour du titre de l'article ; sans titre repérable, pas d'image
                # (mieux vaut un post sans image qu'une capture de menu ou de bandeau).
                if not page.evaluate(_PREPARE_JS):
                    log.warning("Capture refusée : aucun titre d'article repérable sur %s", url)
                    return None
                time.sleep(1.5)  # images chargées à l'arrivée dans l'écran
                check()
                clip = compute_clip(page.evaluate(_MEASURE_JS) or {})
                if clip is None:
                    log.warning("Capture refusée : cadrage impossible sur %s", url)
                    return None
                page.screenshot(path=str(raw_path), clip=clip)
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

    host = host_of(url).removeprefix("www.")
    if frame_image(raw_path, out_path, host):
        _safe_unlink(raw_path)
    else:
        try:
            raw_path.replace(out_path)
        except OSError:
            return None
    _cleanup_old(folder, SHOTS_KEEP)
    log.info("Capture OK : %s → %s", url, out_path.name)
    return out_path, text


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
# Point d'entrée
# ---------------------------------------------------------------------------
def prepare(settings, draft, writer, tag: str = "post") -> tuple[str | None, str | None]:
    """(chemin de l'image, "screenshot" | "ai") ou (None, None). Ne lève jamais."""
    try:
        return _prepare(settings, draft, writer, _safe_tag(tag))
    except Exception as e:  # noqa: BLE001
        log.exception("Visuel : erreur inattendue (%s), post en texte seul", e)
        return None, None


def _prepare(settings, draft, writer, tag: str) -> tuple[str | None, str | None]:
    visual = getattr(draft, "visual", None)
    if visual is None:
        return None, None
    if getattr(draft, "poll", None):
        log.info("Visuel ignoré : X n'accepte pas d'image sur un sondage")
        return None, None
    kind = str(getattr(visual, "kind", "") or "").strip().lower()
    target = str(getattr(visual, "target", "") or "").strip()
    if not target:
        return None, None

    if kind == "screenshot":
        if not getattr(settings, "screenshots_enabled", False):
            log.info("Capture ignorée : SCREENSHOTS_ENABLED=false")
            return None, None
        cited = {str(getattr(src, "url", "") or "").strip().rstrip("/").lower()
                 for src in (getattr(draft, "sources", None) or [])}
        is_cited = target.rstrip("/").lower() in cited and not is_x_url(target) \
            and urlparse(target).scheme in ("http", "https")
        if not (is_allowed_url(target) or is_cited):
            log.warning("Capture refusée (x.com / twitter.com ou site hors liste) : %s", target[:120])
            return None, None
        shot = capture_with_timeout(target, tag)
        if not shot:
            return None, None
        path, text = shot
        post_text = (getattr(draft, "texts", None) or [""])[0]
        try:
            ok, why = writer.visual_matches(post_text, target, text)
        except Exception as e:  # noqa: BLE001
            ok, why = False, f"vérification impossible ({type(e).__name__})"
        if not ok:
            log.info("Capture écartée, elle n'illustre pas le post : %s", why)
            _safe_unlink(path)
            return None, None
        clean_check = getattr(writer, "screenshot_is_clean", None)
        if callable(clean_check):
            try:
                clean, issue = clean_check(path)
            except Exception as e:  # noqa: BLE001
                clean, issue = False, f"vérification impossible ({type(e).__name__})"
            if not clean:
                log.info("Capture écartée, pas assez propre : %s", issue)
                _safe_unlink(path)
                return None, None
        log.info("Capture retenue : %s (%s)", Path(path).name, why)
        return str(path), "screenshot"

    if kind == "ai":
        if not (getattr(settings, "image_ai_enabled", False) and getattr(settings, "openai_api_key", "")):
            log.info("Photo IA ignorée : IMAGE_AI_ENABLED=false ou OPENAI_API_KEY absente")
            return None, None
        if contains_url(target) or off_limits_term(target):
            log.info("Photo IA refusée (lien ou sujet interdit dans la description)")
            return None, None
        path = generate_ai_image(settings, target, tag)
        return (str(path), "ai") if path else (None, None)

    log.warning("Visuel de type inconnu %r ignoré", kind)
    return None, None
