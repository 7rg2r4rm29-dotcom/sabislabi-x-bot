"""Image des posts : la capture d'écran, façon téléphone, de la page de l'actu citée.

Ce que fait une capture (`prepare` → `_shot_visual` → `capture_with_timeout`) :
  - la page d'une actu CITÉE par le post est chargée comme sur un iPhone
    (390 × 844 px CSS, ×3 : 1170 px de large, agent mobile), dans un Chromium
    headless neuf (aucun compte, aucune session, aucun cookie gardé) ;
  - on la nettoie comme le ferait quelqu'un avant sa capture : bandeaux de
    cookies, publicités, menus collants, pop-ups, encarts « à lire aussi » ;
  - on la fait défiler jusqu'au titre de l'article (avec la petite ligne de
    rubrique au-dessus, et la grande photo si elle précède le titre), puis on
    coupe le bas entre deux lignes de texte, en portrait 4:5 au plus (le format
    qu'X affiche en entier). Ni l'heure, ni la batterie, ni la barre d'adresse :
    une capture brute, sans maquette, sans rien d'ajouté ;
  - Claude REGARDE la capture avant qu'elle serve (`writer.screenshot_is_clean`) ;
    au moindre doute, on essaie la page d'une autre actu citée, sinon le post
    part en texte seul : jamais de capture ratée publiée.
Règles gardées de la v2 :
  - JAMAIS de capture de x.com / twitter.com ; les requêtes vers les domaines
    de X sont même bloquées dans la page capturée ;
  - page d'erreur / vide / mur de connexion / redirection hors du site ou vers
    une page de rubrique → pas de capture ;
  - délai dur : la capture tourne dans un thread de travail borné ; la boucle
    principale ne reste jamais figée.
Photo IA (OpenAI) : facultative, désactivée par défaut.
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

from . import utils
from .filters import off_limits_term
from .x_api import contains_url

log = logging.getLogger("xbot.visuals")

CAPTURE_TIMEOUT_S = 90
SHOTS_KEEP = 60
GENERATED_KEEP = 50
VISIBLE_TEXT_CHARS = 1500
MAX_PAGES_TRIED = 3               # pages d'actus citées essayées avant d'abandonner l'image
# La page est chargée comme sur un iPhone : une colonne, un gros titre, et la
# définition d'une vraie capture d'écran (390 px × 3 = 1170 px de large).
MOBILE_USER_AGENT = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 "
                     "(KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1")
SHOT_WIDTH = 390                  # px CSS
SHOT_VIEWPORT_HEIGHT = 844        # hauteur d'écran d'un iPhone
SHOT_SCALE = 3
# Hauteur de la capture, en px CSS. Le maximum donne du 4:5 (1170 × 1461), le
# format portrait qu'X affiche en entier dans le fil ; le minimum, un carré large.
SHOT_MAX_HEIGHT = 487
SHOT_MIN_HEIGHT = 300
SHOT_TOP_MARGIN = 22              # un peu d'air au-dessus du titre, comme sur un vrai écran
HEADLINE_MUST_END_BEFORE = 360    # px CSS : quand une image précède le titre, il finit ici au plus tard
HEADLINE_LATEST_END = SHOT_MAX_HEIGHT - 40   # au-delà, même un très long titre ne tient plus en entier
JPEG_QUALITY = 93

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
# collants, pop-ups), les publicités et les barres de navigation, puis on
# repère le titre. On ne touche jamais au contenu de l'article lui-même.
_PREPARE_JS = """() => {
  const hide = el => el.style.setProperty('display', 'none', 'important');
  // Rectangle d'un élément ; s'il n'a pas de boîte à lui (display: contents), celui de son contenu.
  const rect = window.__xbotRect = el => {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) return r;
    const range = document.createRange();
    range.selectNodeContents(el);
    return range.getBoundingClientRect();
  };
  const visible = e => {
    const r = rect(e), cs = getComputedStyle(e);
    return r.width > 150 && r.height > 18 && cs.visibility !== 'hidden' && cs.display !== 'none'
      && parseFloat(cs.opacity || '1') > 0.1 && (e.innerText || e.textContent || '').trim().length > 8;
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
  // Publicités : emplacements (souvent vides) et leurs étiquettes « Advertisement ».
  const AD = /(^|[\\s_-])(ad|ads|advert|adverts|advertisement|advertising|adslot|adunit|adwrapper|adcontainer|dfp|gpt|sponsor|sponsored|outbrain|taboola|leaderboard|billboard|mpu|skyscraper)([\\s_-]|$)/i;
  for (const el of document.querySelectorAll('div, aside, section, ins, span, figure')) {
    if (el.contains(h) || h.contains(el)) continue;
    const cls = typeof el.className === 'string' ? el.className : '';
    const key = (el.id || '') + ' ' + cls + ' ' + (el.getAttribute('aria-label') || '') + ' '
      + (el.getAttribute('data-testid') || '') + ' ' + (el.getAttribute('data-component') || '');
    if (el.tagName === 'INS' || AD.test(key)) hide(el);
  }
  for (const f of document.querySelectorAll('iframe')) {
    const k = (f.src || '') + ' ' + (f.id || '') + ' ' + (f.name || '') + ' ' + (f.title || '');
    if (/doubleclick|googlesyndication|adservice|amazon-adsystem|adnxs|\\bads?\\b|advert/i.test(k)) hide(f);
  }
  for (const el of document.querySelectorAll('div, span, p, small, label')) {
    if (el.children.length || el.contains(h)) continue;
    if (!/^(advertisement|advertising|ad|sponsored|sponsored content|ad feedback|publicit[ée])$/i.test((el.textContent || '').trim())) continue;
    hide(el);
    const box = el.parentElement;
    if (box && !box.contains(h) && (box.innerText || '').trim().length < 4 && box.getBoundingClientRect().height < 500) hide(box);
  }
  // Encarts glissés dans l'article (« Read next », « Related », inscription à une lettre) : ce
  // n'est pas l'article. Seulement de petits blocs dont le texte COMMENCE par ces mots.
  const PROMO = /^(read next|read more|related( stories| articles| content| reading)?|recommended( for you)?|see also|more from|sign up|subscribe|don.t miss|most read|up next|watch next|trending( now)?|you (may|might) also like)\\b/i;
  for (const el of document.querySelectorAll('div, aside, section, p, ul, a')) {
    if (el.contains(h) || h.contains(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.height < 16 || r.height > 260 || r.width < 150) continue;
    if (PROMO.test((el.innerText || '').trim())) hide(el);
  }
  // Menus du site (jamais le bloc qui contient le titre).
  for (const el of document.querySelectorAll('nav, [role="navigation"], [role="banner"], header, '
      + '[class*="navbar"], [class*="site-header"], [class*="SiteHeader"], [id*="header"]')) {
    if (!el.contains(h) && !h.contains(el)) hide(el);
  }
  window.__xbotHeadline = h;
  const y = rect(h).top + window.scrollY;
  window.scrollTo(0, Math.max(0, y - 80));
  return true;
}"""

# Les images de la partie visible doivent être arrivées ; une image cassée (ou un
# lecteur vidéo vide) est retirée plutôt que photographiée.
_SETTLE_JS = """async (reach) => {
  const near = el => { const r = el.getBoundingClientRect(); return r.bottom > -50 && r.top < reach && r.width > 40; };
  const imgs = [...document.images].filter(near);
  for (const img of imgs) { if (img.loading === 'lazy') img.loading = 'eager'; }
  await Promise.all(imgs.map(img => (img.complete && img.naturalWidth > 0) ? null : new Promise(done => {
    img.addEventListener('load', done, {once: true});
    img.addEventListener('error', done, {once: true});
    setTimeout(done, 5000);
  })));
  let removed = 0;
  for (const img of imgs) {
    if (img.complete && img.naturalWidth > 0) continue;
    const box = img.closest('figure') || img;
    box.style.setProperty('display', 'none', 'important');
    removed++;
  }
  try { await document.fonts.ready; } catch (e) {}
  return removed;
}"""

# Cadrage : le titre en haut de l'écran, avec la petite ligne qui le précède
# (rubrique, date) si elle existe, sans jamais trancher une ligne de texte.
_ALIGN_JS = """([margin, maxH, headEnd]) => {
  const h = window.__xbotHeadline, rect = window.__xbotRect;
  if (!h || !rect) return null;
  let r = rect(h);
  const vw = document.documentElement.clientWidth;
  let top = r.top - margin;
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
  // Sur beaucoup de sites mobiles, la grande image est AVANT le titre : on la garde dans le
  // cadre (entière si elle tient, sinon sa partie basse), comme en faisant défiler.
  let lead = null;
  for (const el of document.querySelectorAll('img, picture, video')) {
    if (el.contains(h) || h.contains(el)) continue;
    const b = el.getBoundingClientRect();
    if (b.width < 0.6 * vw || b.height < 120 || b.height > 700) continue;
    if (b.bottom > top + 8 || b.bottom < top - 70) continue;
    if (!lead || b.height > lead.getBoundingClientRect().height) lead = el;
  }
  if (lead) {
    const b = lead.getBoundingClientRect();
    // Le titre doit finir avant headEnd (la même limite que headline_fits, côté Python) : si
    // l'image est trop haute, on n'en garde que le bas — à condition qu'il en reste assez
    // pour qu'on la reconnaisse. Sinon, on cadre sur le titre, sans elle.
    const start = Math.max(b.top, r.bottom - headEnd);
    if (b.bottom - start >= Math.max(110, 0.4 * b.height)) top = start; else lead = null;
  }
  window.scrollBy(0, Math.max(top, r.top - (lead ? maxH : 120)));
  // Un peu d'air en haut : une ligne de texte collée au bord supérieur aurait l'air coupée.
  {
    let first = null;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (!n.textContent.trim() || !n.parentElement) continue;
      const cs = getComputedStyle(n.parentElement);
      if (cs.visibility === 'hidden' || cs.display === 'none') continue;
      range.selectNodeContents(n);
      for (const rc of range.getClientRects()) {
        if (rc.width < 2 || rc.height < 6 || rc.top < 0 || rc.top > 40) continue;
        if (first === null || rc.top < first) first = rc.top;
      }
    }
    if (first !== null && first < 12) {
      const need = 14 - first, can = Math.min(need, window.scrollY);
      if (can > 0) window.scrollBy(0, -can);
      // Tout en haut de la page, on ne peut pas remonter : on ajoute la marge qui manque,
      // de la couleur du fond de l'article.
      const lack = need - can;
      if (lack > 0.5) {
        const root = document.documentElement;
        let el = document.elementFromPoint(vw / 2, 2), color = '';
        for (; el; el = el.parentElement) {
          const c = getComputedStyle(el).backgroundColor;
          if (c && c !== 'transparent' && !/^rgba\\(\\s*0,\\s*0,\\s*0,\\s*0\\s*\\)$/.test(c)) { color = c; break; }
        }
        root.style.setProperty('padding-top', ((parseFloat(root.style.paddingTop) || 0) + lack) + 'px', 'important');
        if (color) root.style.setProperty('background-color', color, 'important');
      }
    }
  }
  // Ce qui dépasse encore par le haut (logo du site, bouton) serait tranché : on le masque.
  for (const el of document.querySelectorAll('body *')) {
    if (el.contains(h) || (lead && (el.contains(lead) || lead.contains(el)))) continue;
    const b = el.getBoundingClientRect();
    if (b.width > 0 && b.height > 0 && b.top < -1 && b.bottom > 1 && b.bottom < 150)
      el.style.setProperty('visibility', 'hidden', 'important');
  }
  r = rect(h);
  const doc = document.documentElement;
  return {top: r.top, bottom: r.bottom, left: r.left, right: r.right, vw: doc.clientWidth,
          pageWidth: doc.scrollWidth, lead: !!lead};
}"""

# Où couper en bas : le plus bas possible (jusqu'au 4:5), mais entre deux lignes de
# texte, jamais au milieu d'une ligne, et sans laisser un liseré d'image ou un grand vide.
_FRAME_JS = """([minH, maxH]) => {
  const h = window.__xbotHeadline, rect = window.__xbotRect;
  if (!h || !rect) return null;
  const vw = document.documentElement.clientWidth;
  const seen = el => !el.checkVisibility || el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
  const lines = [], blocks = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (!n.textContent.trim() || !n.parentElement || !seen(n.parentElement)) continue;
    range.selectNodeContents(n);
    for (const r of range.getClientRects()) {
      if (r.width < 2 || r.height < 6 || r.right <= 0 || r.left >= vw) continue;
      if (r.bottom <= 0 || r.top >= maxH + 120) continue;
      lines.push([r.top, r.bottom]);
    }
  }
  for (const el of document.querySelectorAll('img, video, picture, canvas, iframe, svg')) {
    if (!seen(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 120 || r.height < 70 || r.bottom <= 0 || r.top >= maxH + 600) continue;
    blocks.push([r.top, r.bottom]);
  }
  // On coupe ENTRE deux lignes (2 px de marge : aucun pixel de lettre tranché)…
  const slices = y => lines.some(([t, b]) => t - 2 < y && b + 2 > y);
  // … une image coupée, c'est naturel quand on fait défiler, mais jamais un simple liseré.
  const sliver = y => blocks.some(([t, b]) => t - 3 < y && b > y && (y - t) < Math.max(110, 0.3 * (b - t)));
  const hb = rect(h).bottom;
  const floor = Math.max(minH, Math.ceil(hb) + 16);
  let cut = null;
  for (let y = maxH; y >= floor; y--) { if (!slices(y) && !sliver(y)) { cut = y; break; } }
  if (cut === null) return {cut: null, floor: floor};
  // Pas de grande bande vide en bas : on remonte juste sous le dernier contenu.
  const covered = blocks.some(([t, b]) => t < cut && b >= cut);
  if (!covered) {
    let last = hb;
    for (const [t, b] of lines) if (b <= cut && b > last) last = b;
    for (const [t, b] of blocks) if (b <= cut && b > last) last = b;
    if (cut - last > 34) cut = Math.max(floor, Math.round(last + 20));
  }
  return {cut: cut, floor: floor};
}"""


def headline_fits(m: dict | None) -> str | None:
    """Une raison si le titre mesuré ne sera pas entier et bien placé dans la
    capture, None si le cadrage est bon."""
    try:
        top, bottom, left, right, vw = (float(m[k]) for k in ("top", "bottom", "left", "right", "vw"))
    except (KeyError, TypeError, ValueError):
        return "titre non mesurable"
    if bottom <= top or vw <= 0:
        return "titre non mesurable"
    if top < -2:
        return "titre coupé en haut"
    # Une image d'ouverture laisse toujours de la place sous le titre (_ALIGN_JS) ; sans
    # elle, un très long titre peut occuper presque tout le cadre : c'est encore une capture.
    limit = HEADLINE_MUST_END_BEFORE if m.get("lead") else HEADLINE_LATEST_END
    if bottom > limit + 2:                          # 2 px : arrondis du navigateur
        return "titre trop long pour l'écran"
    if left < -2 or right > vw + 2:
        return "titre plus large que l'écran"
    return None


def frame_height(frame: dict | None) -> int | None:
    """Hauteur de la capture (px CSS) choisie par _FRAME_JS, ou None si aucun
    endroit propre où couper n'a été trouvé."""
    try:
        cut = int(round(float((frame or {}).get("cut"))))
    except (TypeError, ValueError):
        return None
    return cut if SHOT_MIN_HEIGHT <= cut <= SHOT_MAX_HEIGHT else None


FLAT_ROW_TOLERANCE = 18           # écart de couleur toléré pour dire qu'une ligne de pixels est unie
FLAT_BAND_MAX = 40                # px CSS : au-delà, une bande unie en bas est retirée
FLAT_BAND_KEEP = 20               # px CSS gardés sous le dernier contenu


def trim_flat_bottom(path: Path) -> bool:
    """Retire, en bas de la capture, une grande bande unie (fond vide sous un titre
    court, encart noir…) : on garde FLAT_BAND_KEEP px CSS sous le dernier contenu,
    sans descendre sous SHOT_MIN_HEIGHT. True si l'image a été recoupée."""
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        with Image.open(path) as src:
            im = src.convert("RGB")
        w, h = im.size
        px = im.load()
        ref = px[w // 2, h - 1]
        step = max(1, w // 120)

        def flat(y: int) -> bool:
            return all(max(abs(a - b) for a, b in zip(px[x, y], ref)) <= FLAT_ROW_TOLERANCE
                       for x in range(0, w, step))

        band = 0
        while band < h and flat(h - 1 - band):
            band += 1
        new_h = max(SHOT_MIN_HEIGHT * SHOT_SCALE, h - band + FLAT_BAND_KEEP * SHOT_SCALE)
        if band <= FLAT_BAND_MAX * SHOT_SCALE or new_h >= h:
            return False
        im.crop((0, 0, w, new_h)).save(path, "JPEG", quality=JPEG_QUALITY)
        return True
    except Exception as e:  # noqa: BLE001
        log.info("Bande vide non retirée (%s)", type(e).__name__)
        return False


def launch_options() -> dict:
    """Options de lancement de Chromium. XBOT_BROWSER_PATH / XBOT_BROWSER_PROXY ne
    servent qu'aux environnements de test (navigateur préinstallé, proxy)."""
    launch: dict = {"headless": True, "args": ["--disable-dev-shm-usage", "--no-first-run", "--disable-extensions"]}
    if os.getenv("XBOT_BROWSER_PATH"):
        launch["executable_path"] = os.environ["XBOT_BROWSER_PATH"]
    if os.getenv("XBOT_BROWSER_PROXY"):
        launch["proxy"] = {"server": os.environ["XBOT_BROWSER_PROXY"]}
    return launch


def _browser_launch_options() -> tuple[dict, dict]:
    """(options de lancement, options de la page) pour une capture comme sur un iPhone."""
    context: dict = {"viewport": {"width": SHOT_WIDTH, "height": SHOT_VIEWPORT_HEIGHT},
                     "device_scale_factor": SHOT_SCALE, "is_mobile": True, "has_touch": True,
                     "user_agent": MOBILE_USER_AGENT, "locale": "en-US", "accept_downloads": False}
    if os.getenv("XBOT_BROWSER_PROXY"):
        context["ignore_https_errors"] = True       # proxy de test seulement
    return launch_options(), context


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


def same_article(final_url: str, requested_url: str) -> bool:
    """Le lien mène-t-il encore à l'article ? Non s'il renvoie vers l'accueil ou une page de
    rubrique (le chemin demandé, raccourci) ; une adresse qui a l'air d'un article passe."""
    req = [s for s in urlparse(requested_url or "").path.split("/") if s]
    fin = [s for s in urlparse(final_url or "").path.split("/") if s]
    if not req or fin == req:
        return True
    if not fin or (len(fin) < len(req) and req[:len(fin)] == fin):
        return False
    return any(len(s) >= 12 or (len(s) >= 6 and any(c.isdigit() for c in s)) for s in fin)



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
    session), vérifie la page, ferme les bandeaux, cadre sur le titre et
    photographie l'écran, coupé proprement en bas. (chemin JPEG, texte visible)
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
    out_path = folder / f"{tag}_{stamp}.jpg"
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
                if not same_article(final_url, url):
                    log.warning("Capture refusée : le lien ne mène plus à l'article (%s → %s)", url, final_url[:100])
                    return None
                check()
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(2.0)
                check()
                broken = page_looks_broken(*_title_and_body(page))
                for _ in range(4):   # page encore vide (serveur lent, contenu qui arrive après) : on patiente
                    if not (broken or "").startswith("page quasi vide"):
                        break
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
                if not final_url_ok(page.url or url, url) or not same_article(page.url or url, url):
                    log.warning("Capture refusée : la page a changé (%s → %s)", url, (page.url or "")[:100])
                    return None
                text = visible_text(title, body)
                # Cadrage sur le titre de l'article ; sans titre repérable ou bien placé, pas
                # de capture (le post partira sans image).
                if not page.evaluate(_PREPARE_JS):
                    log.warning("Capture refusée : aucun titre d'article repérable sur %s", url)
                    return None
                time.sleep(0.6)
                check()
                try:   # images de l'écran arrivées, images cassées retirées
                    page.evaluate(_SETTLE_JS, SHOT_VIEWPORT_HEIGHT + 200)
                except Exception:  # noqa: BLE001
                    pass
                # Cadrage en plusieurs temps : la page bouge encore pendant que les images de
                # l'écran arrivent. On recadre jusqu'à ce que le titre ne bouge plus.
                measure = None
                for _ in range(4):
                    measure = page.evaluate(_ALIGN_JS, [SHOT_TOP_MARGIN, SHOT_MAX_HEIGHT, HEADLINE_MUST_END_BEFORE])
                    time.sleep(0.7)
                    check()
                    try:
                        page.evaluate(_SETTLE_JS, SHOT_MAX_HEIGHT + 60)
                    except Exception:  # noqa: BLE001
                        pass
                    after = page.evaluate(_ALIGN_JS, [SHOT_TOP_MARGIN, SHOT_MAX_HEIGHT, HEADLINE_MUST_END_BEFORE])
                    moved = abs(float((after or {}).get("top", 0)) - float((measure or {}).get("top", 0)))
                    measure = after
                    if moved < 2:
                        break
                problem = headline_fits(measure)
                if problem:
                    log.warning("Capture refusée : %s sur %s", problem, url)
                    return None
                height = frame_height(page.evaluate(_FRAME_JS, [SHOT_MIN_HEIGHT, SHOT_MAX_HEIGHT]))
                if height is None:
                    log.warning("Capture refusée : aucun endroit propre où couper le bas sur %s", url)
                    return None
                check()
                page.screenshot(path=str(out_path), type="jpeg", quality=JPEG_QUALITY,
                                clip={"x": 0, "y": 0, "width": SHOT_WIDTH, "height": height})
            finally:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
    except _Cancelled:
        _safe_unlink(out_path)
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("Capture échouée (%s) : %s", url, str(e).splitlines()[0][:200] if str(e) else type(e).__name__)
        _safe_unlink(out_path)
        return None

    if not out_path.is_file():
        return None
    trimmed = trim_flat_bottom(out_path)
    _cleanup_old(folder, SHOTS_KEEP)
    log.info("Capture OK : %s → %s (%d px de haut%s)", url, out_path.name, height * SHOT_SCALE,
             ", bande vide retirée en bas" if trimmed else "")
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
# Captures d'écran : quelles pages, et le contrôle par Claude
# ---------------------------------------------------------------------------
def _capturable(draft, target: str) -> bool:
    """Page d'une actu citée par le post, ou site de la liste ; jamais X."""
    if is_x_url(target) or urlparse(target).scheme not in ("http", "https"):
        return False
    cited = {str(getattr(src, "url", "") or "").strip().rstrip("/").lower()
             for src in (getattr(draft, "sources", None) or [])}
    return target.rstrip("/").lower() in cited or is_allowed_url(target)


def shot_candidates(draft) -> list[str]:
    """Pages à essayer, dans l'ordre : celle que Claude a désignée, puis celles des
    autres actus citées (même histoire, autre site). Jamais X, jamais deux fois la même."""
    visual = getattr(draft, "visual", None)
    wanted = [str(getattr(visual, "target", "") or "").strip()]
    wanted += [str(getattr(src, "url", "") or "").strip() for src in (getattr(draft, "sources", None) or [])]
    out: list[str] = []
    seen: set[str] = set()
    for url in wanted:
        key = url.rstrip("/").lower()
        if not url or key in seen or not _capturable(draft, url):
            continue
        seen.add(key)
        out.append(url)
    return out[:MAX_PAGES_TRIED]


def title_on_page(title: str, page_text: str) -> bool:
    """La page capturée parle-t-elle bien de l'actu citée ? Au moins la moitié des mots
    « qui comptent » du titre de l'actu doivent se retrouver dans le texte de la page
    (son titre et son début). Garde-fou contre une redirection, un direct, une page
    « à lire aussi » ; sans titre ou sans texte, on ne conclut rien."""
    words = {w for w in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(w) >= 4}
    seen = set(re.findall(r"[a-z0-9]+", (page_text or "").lower()))
    if len(words) < 3 or not seen:
        return True
    return len(words & seen) * 2 >= len(words)


def _shot_visual(settings, draft, writer, tag: str) -> tuple[str | None, str | None]:
    if not getattr(settings, "screenshots_enabled", False):
        log.info("Capture ignorée : SCREENSHOTS_ENABLED=false")
        return None, None
    check = getattr(writer, "screenshot_is_clean", None)
    titles = {str(getattr(src, "url", "") or "").rstrip("/").lower(): str(getattr(src, "title", "") or "")
              for src in (getattr(draft, "sources", None) or [])}
    texts = getattr(draft, "texts", None) or [""]
    first_text = str(texts[0] or "") if isinstance(texts, list) and texts else ""
    for n, url in enumerate(shot_candidates(draft), start=1):
        shot = capture_with_timeout(url, f"{tag}-{n}" if n > 1 else tag)
        if not shot:
            continue
        path, text = shot
        if not title_on_page(titles.get(url.rstrip("/").lower(), ""), text):
            log.info("Capture écartée (la page ne semble pas être celle de l'actu citée) : %s", url[:100])
            _safe_unlink(path)
            continue
        # Claude REGARDE la capture ; sans relecteur, elle n'est jamais utilisée à l'aveugle.
        clean, issue = False, "aucun contrôle visuel possible"
        if callable(check):
            try:
                clean, issue = check(path, headline=titles.get(url.rstrip("/").lower(), ""),
                                     post=first_text)
            except Exception as e:  # noqa: BLE001
                clean, issue = False, f"vérification impossible ({type(e).__name__})"
        if clean:
            log.info("Capture prête : %s (%s)", Path(path).name, url[:100])
            return str(path), "shot"
        log.info("Capture écartée (%s) : %s", issue or "pas assez propre", url[:100])
        _safe_unlink(path)
    return None, None


def self_test() -> tuple[bool, str]:
    """Le serveur sait-il faire une capture ? Une page d'essai locale (aucun réseau)
    est chargée comme sur un téléphone et photographiée (commande `--check`)."""
    import tempfile
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "Playwright n'est pas installé"
    html = ("<!doctype html><meta name='viewport' content='width=device-width'><article>"
            "<h1>Screenshot check</h1><p>" + "A line of text. " * 40 + "</p></article>")
    result: dict = {}

    def work() -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="xbot-shot-") as tmp, sync_playwright() as pw:
                launch_opts, context_opts = _browser_launch_options()
                browser = pw.chromium.launch(**launch_opts)
                try:
                    page = browser.new_context(**context_opts).new_page()
                    page.set_content(html, wait_until="load", timeout=20000)
                    out = Path(tmp) / "check.jpg"
                    page.screenshot(path=str(out), type="jpeg", quality=JPEG_QUALITY,
                                    clip={"x": 0, "y": 0, "width": SHOT_WIDTH, "height": SHOT_MIN_HEIGHT})
                    result["ok"] = out.is_file() and out.stat().st_size > 2000
                finally:
                    browser.close()
        except Exception as e:  # noqa: BLE001
            result["why"] = str(e).splitlines()[0][:160] if str(e) else type(e).__name__

    worker = threading.Thread(target=work, daemon=True, name="shot-check")
    worker.start()
    worker.join(60)
    if worker.is_alive():
        return False, "le navigateur ne répond pas (plus de 60 s)"
    if result.get("ok"):
        return True, ""
    return False, result.get("why") or "le navigateur n'a pas pu faire la capture d'essai"


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------
def prepare(settings, draft, writer, tag: str = "post") -> tuple[str | None, str | None]:
    """(chemin de l'image, "shot" | "ai") ou (None, None). Ne lève jamais."""
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
    if kind == "shot":
        return _shot_visual(settings, draft, writer, tag)
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
