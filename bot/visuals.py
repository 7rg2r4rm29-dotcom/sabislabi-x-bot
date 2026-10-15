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
        w, h = img.size
        target_w = 1600
        if w > target_w:
            img = img.resize((target_w, int(h * target_w / w)), Image.LANCZOS)
            w, h = img.size
        max_h = int(w * 0.62)
        if h > max_h:
            img = img.crop((0, 0, w, max_h))
            w, h = img.size
        pad, bar = 28, 44
        canvas = Image.new("RGB", (w + 2 * pad, h + 2 * pad + bar), (16, 18, 24))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w, h), radius=18, fill=255)
        canvas.paste(img, (pad, pad), mask)
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
        d.text((pad, h + pad + 10), f"source: {source}", fill=(150, 156, 170), font=font)
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
            browser = pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--no-first-run",
                                                              "--disable-extensions"])
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="en-US",
                                              accept_downloads=False)
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
                try:
                    page.add_style_tag(content=(
                        "[class*='cookie'],[id*='cookie'],[class*='consent'],[id*='consent'],"
                        "[class*='newsletter'],[class*='popup'],[class*='modal'],"
                        "iframe[src*='chat'],[class*='intercom'],[class*='drift']"
                        "{display:none !important}"))
                except Exception:  # noqa: BLE001
                    pass
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
                page.screenshot(path=str(raw_path), full_page=False)
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
        if not is_allowed_url(target):
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
