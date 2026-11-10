"""Visuels « maison » : des cartes dessinées en HTML/CSS, rendues en PNG par Chromium.

Pourquoi : une capture d'écran de site ne se maîtrise pas (menus, bandeaux,
mises en page qui changent). Une carte, si : même identité à chaque post,
texte gros et lisible sur un téléphone, rien de coupé.

Cinq modèles, format portrait 4:5 (1080 × 1350, affiché en entier dans le fil X) :
  - prompt : la phrase à copier, dans une bulle de messagerie ;
  - steps  : 2 à 4 étapes numérotées ;
  - list   : 2 à 5 points ;
  - fact   : une affirmation en très grand, avec 0 à 3 lignes d'explication ;
  - phone  : la vraie page de l'article, vue dans un smartphone (capture mobile).

Direction artistique : une seule famille (mêmes polices, même mise en page) et
plusieurs palettes qui tournent d'un post à l'autre (THEMES). Pour suivre les
tendances, on change THEMES ; le reste du bot n'y touche pas.

Aucun réseau : polices embarquées (bot/assets/fonts, licence OFL), images en
data-URI. `render()` ne lève jamais : False = pas d'image.
"""
from __future__ import annotations

import base64
import html
import logging
import os
import re
import threading
from functools import lru_cache
from pathlib import Path

from . import utils

log = logging.getLogger("xbot.cards")

W, H = 1080, 1350                 # 4:5
RENDER_SCALE = 2                  # PNG 2160 × 2700 (texte net)
MAX_PNG_BYTES = 4_500_000         # X refuse les images de plus de 5 Mo
RENDER_TIMEOUT_S = 60
CARD_TYPES = ("prompt", "steps", "list", "fact", "phone")
FONTS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"

# Zone de page visible dans le smartphone (px CSS de la page mobile) : sert à
# cadrer la capture et à ne faire vérifier par Claude que ce qui sera montré.
PHONE_PAGE_WIDTH = 390
PHONE_PAGE_VISIBLE_HEIGHT = 430

# ---------------------------------------------------------------------------
# Palettes (la « DA »). Même structure pour toutes : fond, encre, accent, surfaces.
# ---------------------------------------------------------------------------
THEMES: dict[str, dict[str, str]] = {
    # Papier crème + vermillon : le look « éditorial » (titres serrés, italique serif).
    "cream": {
        "bg": "#F3EDE2", "glow": "rgba(255,79,31,.16)", "ink": "#16130F", "muted": "#6F665A",
        "accent": "#FF4F1F", "accent_ink": "#FFFFFF", "accent_text": "#E8400F",
        "panel": "#FFFFFF", "panel_ink": "#16130F", "line": "rgba(22,19,15,.14)",
        "badge_bg": "#16130F", "badge_ink": "#F3EDE2", "dot": "#FF4F1F",
    },
    # Nuit + citron vert : contraste maximal dans un fil clair comme dans un fil sombre.
    "midnight": {
        "bg": "#0D0E12", "glow": "rgba(200,245,60,.20)", "ink": "#F4F3EE", "muted": "#9C9EA8",
        "accent": "#C8F53C", "accent_ink": "#0D0E12", "accent_text": "#C8F53C",
        "panel": "#1B1D24", "panel_ink": "#F4F3EE", "line": "rgba(255,255,255,.14)",
        "badge_bg": "#C8F53C", "badge_ink": "#0D0E12", "dot": "#0D0E12",
    },
    # Bleu électrique + jaune : aplat de couleur, très visible.
    "cobalt": {
        "bg": "#2447F9", "glow": "rgba(255,255,255,.22)", "ink": "#FFFFFF", "muted": "rgba(255,255,255,.78)",
        "accent": "#FFE04A", "accent_ink": "#111533", "accent_text": "#FFE04A",
        "panel": "#FFFFFF", "panel_ink": "#111533", "line": "rgba(255,255,255,.28)",
        "badge_bg": "#FFFFFF", "badge_ink": "#2447F9", "dot": "#FFB400",
    },
    # Dégradé pêche → lilas + violet : doux, « lifestyle ».
    "blush": {
        "bg": "linear-gradient(155deg,#FFE4D2 0%,#FAD6EA 48%,#D7DCFF 100%)", "glow": "rgba(255,255,255,.55)",
        "ink": "#1C1A22", "muted": "#5F5A6C",
        "accent": "#6A3BF2", "accent_ink": "#FFFFFF", "accent_text": "#6A3BF2",
        "panel": "rgba(255,255,255,.82)", "panel_ink": "#1C1A22", "line": "rgba(28,26,34,.14)",
        "badge_bg": "#1C1A22", "badge_ink": "#FFFFFF", "dot": "#B79CFF",
    },
    # Vert profond + crème + pêche.
    "forest": {
        "bg": "#0F3B2D", "glow": "rgba(255,179,124,.20)", "ink": "#F6F1E4", "muted": "#A9C4B6",
        "accent": "#FFB37C", "accent_ink": "#0F3B2D", "accent_text": "#FFB37C",
        "panel": "#F6F1E4", "panel_ink": "#0F3B2D", "line": "rgba(246,241,228,.20)",
        "badge_bg": "#FFB37C", "badge_ink": "#0F3B2D", "dot": "#0F3B2D",
    },
}
THEME_ORDER = ("cream", "midnight", "blush", "cobalt", "forest")


def theme_for(index: int) -> str:
    """Palette du n-ième visuel : elles tournent, deux posts de suite ne se ressemblent pas."""
    return THEME_ORDER[int(index) % len(THEME_ORDER)]


# ---------------------------------------------------------------------------
# Texte -> HTML
# ---------------------------------------------------------------------------
def _esc(text: str) -> str:
    return html.escape(str(text or ""), quote=False)


def title_html(title: str) -> str:
    """« Dinner from a *fridge photo* » : le passage entre astérisques est mis en
    valeur (italique serif, couleur d'accent). Le reste est échappé."""
    out, last = [], 0
    for m in re.finditer(r"\*([^*\n]{1,60})\*", title or ""):
        out.append(_esc(title[last:m.start()]))
        out.append(f"<em>{_esc(m.group(1))}</em>")
        last = m.end()
    out.append(_esc((title or "")[last:]))
    return "".join(out).replace("*", "")


def plain_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").replace("*", "")).strip()


@lru_cache(maxsize=1)
def _fonts_css() -> str:
    def b64(name: str) -> str:
        return base64.b64encode((FONTS_DIR / name).read_bytes()).decode("ascii")
    return (
        '@font-face{font-family:"Inter";font-weight:100 900;font-style:normal;'
        f'src:url(data:font/woff2;base64,{b64("inter.woff2")}) format("woff2")}}'
        '@font-face{font-family:"Bricolage";font-weight:200 800;font-style:normal;'
        f'src:url(data:font/woff2;base64,{b64("bricolage.woff2")}) format("woff2")}}'
        '@font-face{font-family:"Instrument";font-weight:400;font-style:italic;'
        f'src:url(data:font/woff2;base64,{b64("instrument-italic.woff2")}) format("woff2")}}'
    )


_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:1080px;height:1350px;overflow:hidden}
body{font-family:"Inter",system-ui,sans-serif;color:var(--ink);background:var(--bg);position:relative;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;--s:1}
.glow{position:absolute;right:-260px;top:-260px;width:900px;height:900px;border-radius:50%;
  background:radial-gradient(closest-side,var(--glow),transparent)}
.glow.b{left:-320px;right:auto;top:auto;bottom:-360px;opacity:.7}
.wrap{position:absolute;inset:0;padding:70px 74px 66px;display:flex;flex-direction:column}
.wrap>*{flex-shrink:0}
.head{display:flex;justify-content:space-between;align-items:center}
.badge{display:inline-flex;align-items:center;gap:14px;padding:15px 28px 15px 24px;border-radius:999px;
  background:var(--badge-bg);color:var(--badge-ink);font-size:26px;font-weight:700;letter-spacing:.09em;
  text-transform:uppercase;white-space:nowrap}
.badge i{width:13px;height:13px;border-radius:50%;background:var(--dot);box-shadow:0 0 0 5px rgba(127,127,127,.20)}
.handle{color:var(--muted);font-weight:600;font-size:30px;letter-spacing:.01em}
.title{font-family:"Bricolage","Inter",sans-serif;font-weight:800;line-height:1.0;letter-spacing:-.035em;
  margin-top:54px;text-wrap:balance;overflow-wrap:normal}
.title em{font-family:"Instrument",Georgia,serif;font-style:italic;font-weight:400;letter-spacing:-.012em;
  color:var(--accent-text);font-size:1.07em;padding-right:.04em}
.body{flex:1 0 auto;display:flex;flex-direction:column;justify-content:flex-end;margin-top:calc(46px*var(--s))}
.note{display:flex;align-items:center;gap:16px;margin-top:calc(34px*var(--s));color:var(--muted);
  font-size:calc(31px*var(--s));font-weight:500;line-height:1.25}
.note svg{flex:none;width:34px;height:34px}

/* prompt : bulle de messagerie */
.composer{background:var(--panel);color:var(--panel-ink);border-radius:46px;padding:40px 46px 34px;
  box-shadow:0 30px 70px rgba(0,0,0,.16)}
.composer .label{font-size:calc(24px*var(--s));font-weight:700;letter-spacing:.12em;text-transform:uppercase;opacity:.5}
.composer .ptext{margin-top:calc(22px*var(--s));font-size:calc(47px*var(--s));font-weight:560;line-height:1.24;
  letter-spacing:-.012em;overflow-wrap:anywhere}
.composer .row{display:flex;justify-content:space-between;align-items:center;margin-top:calc(34px*var(--s))}
.plus{width:calc(64px*var(--s));height:calc(64px*var(--s));border-radius:50%;border:2.5px solid currentColor;opacity:.3;
  display:flex;align-items:center;justify-content:center}
.plus svg{width:55%;height:55%}
.right{display:flex;align-items:center;gap:calc(26px*var(--s))}
.mic{width:calc(40px*var(--s));height:calc(40px*var(--s));opacity:.34}
.send{width:calc(78px*var(--s));height:calc(78px*var(--s));border-radius:50%;background:var(--accent);display:flex;
  align-items:center;justify-content:center}
.send svg{width:50%;height:50%}

/* steps / list */
.rows{list-style:none}
.rows li{display:flex;align-items:center;gap:calc(34px*var(--s));padding:calc(30px*var(--s)) 0;
  border-top:2.5px solid var(--line)}
.rows li:last-child{border-bottom:2.5px solid var(--line)}
.rows b{flex:none;width:calc(84px*var(--s));height:calc(84px*var(--s));border-radius:50%;background:var(--accent);
  color:var(--accent-ink);display:flex;align-items:center;justify-content:center;
  font-family:"Bricolage",sans-serif;font-weight:800;font-size:calc(44px*var(--s))}
.rows b svg{width:calc(40px*var(--s));height:calc(40px*var(--s))}
.rows span{font-size:calc(45px*var(--s));font-weight:600;line-height:1.16;letter-spacing:-.015em;overflow-wrap:anywhere}

/* fact */
.fact .title{margin-top:70px}
.lines p{font-size:calc(43px*var(--s));font-weight:500;line-height:1.28;letter-spacing:-.01em;color:var(--ink);
  padding:calc(26px*var(--s)) 0;border-top:2.5px solid var(--line);overflow-wrap:anywhere}
.lines p:first-child{border-top:5px solid var(--accent)}
/* fact sans ligne : l'affirmation seule, en très grand, centrée dans la hauteur */
.solo .title{margin-top:auto}
.solo .body{flex:0 0 auto;margin-top:56px;margin-bottom:auto}
.bar{width:168px;height:14px;border-radius:7px;background:var(--accent)}

/* phone : la vraie page, dans un smartphone qui sort par le bas de l'image */
.phonecard{padding-bottom:0}
.phonecard .title{margin-top:44px}
.phone{flex:1 0 780px;min-height:0;width:824px;margin:50px auto 0;background:#09090B;border-radius:108px 108px 0 0;
  padding:22px 22px 0;box-shadow:0 0 0 3px rgba(255,255,255,.14),0 50px 120px rgba(0,0,0,.42);display:flex}
.screen{flex:1;min-height:0;border-radius:88px 88px 0 0;overflow:hidden;background:var(--chrome);color:var(--chrome-ink);
  display:flex;flex-direction:column;position:relative}
.status{flex:none;height:104px;display:flex;justify-content:space-between;align-items:center;padding:14px 62px 0;
  font-size:33px;font-weight:650;letter-spacing:-.01em}
.island{position:absolute;left:50%;top:26px;width:216px;height:62px;margin-left:-108px;border-radius:31px;background:#09090B}
.sys{display:flex;align-items:center;gap:13px}
.sys svg{height:25px}
.urlbar{flex:none;margin:6px 30px 16px;height:78px;border-radius:24px;background:var(--pill);display:flex;
  align-items:center;justify-content:center;gap:13px;font-size:31px;font-weight:500}
.urlbar svg{width:25px;height:25px;opacity:.6}
.page{flex:1;min-height:0;overflow:hidden;background:#fff}
.page img{display:block;width:100%}
"""

_ICON_INFO = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round">'
              '<circle cx="12" cy="12" r="9.2"/><path d="M12 11v5.2M12 7.6v.2"/></svg>')
_ICON_ARROW = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" '
               'stroke-linejoin="round"><path d="M4.5 12h15M13 5.5l6.5 6.5-6.5 6.5"/></svg>')
_ICON_SEND = ('<svg viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="3.2" stroke-linecap="round" '
              'stroke-linejoin="round"><path d="M12 19.5v-15M5.5 11 12 4.5 18.5 11"/></svg>')
_ICON_PLUS = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round">'
              '<path d="M12 5v14M5 12h14"/></svg>')
_ICON_MIC = ('<svg class="mic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round">'
             '<rect x="8.6" y="2.8" width="6.8" height="11.4" rx="3.4"/><path d="M5.2 11.4a6.8 6.8 0 0 0 13.6 0M12 18.2v3"/></svg>')
_ICON_LOCK = ('<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 10V8a5 5 0 0 1 10 0v2h.5A1.5 1.5 0 0 1 19 11.5v8'
              'A1.5 1.5 0 0 1 17.5 21h-11A1.5 1.5 0 0 1 5 19.5v-8A1.5 1.5 0 0 1 6.5 10H7Zm2.2 0h5.6V8a2.8 2.8 0 0 0-5.6 0v2Z"/></svg>')
_ICON_SIGNAL = ('<svg viewBox="0 0 34 24" fill="currentColor"><rect x="1" y="15" width="6" height="8" rx="1.6"/>'
                '<rect x="10" y="10.5" width="6" height="12.5" rx="1.6"/><rect x="19" y="6" width="6" height="17" rx="1.6"/>'
                '<rect x="28" y="1" width="6" height="22" rx="1.6"/></svg>')
_ICON_WIFI = ('<svg viewBox="0 0 32 24" fill="none" stroke="currentColor" stroke-width="3.4" stroke-linecap="round">'
              '<path d="M3 9.2c7.4-6.6 18.6-6.6 26 0M8.2 14.6c4.5-3.9 11.1-3.9 15.6 0"/>'
              '<circle cx="16" cy="20" r="2.4" fill="currentColor" stroke="none"/></svg>')
_ICON_BATTERY = ('<svg viewBox="0 0 50 24" fill="none"><rect x="1.5" y="1.5" width="42" height="21" rx="6.5" '
                 'stroke="currentColor" stroke-opacity=".45" stroke-width="2.4"/>'
                 '<rect x="4.6" y="4.6" width="35.8" height="14.8" rx="3.8" fill="currentColor"/>'
                 '<path d="M46.5 8.5v7c1.6-.6 2.6-1.9 2.6-3.5s-1-2.9-2.6-3.5Z" fill="currentColor" fill-opacity=".5"/></svg>')

# Ajuste les tailles : on réduit tant que ça dépasse, puis on agrandit (titre
# d'abord, corps ensuite) tant qu'il reste de la place : pas de grand vide.
_FIT_JS = """() => {
  const wrap = document.querySelector('.wrap'), title = document.querySelector('.title');
  const body = document.querySelector('.body');
  // Un mot du titre ne doit jamais être coupé en deux : s'il dépasse, on réduit le titre.
  const wordsFit = () => title.scrollWidth <= title.clientWidth + 1;
  const fits = () => wrap.scrollHeight <= wrap.clientHeight + 1 && wrap.scrollWidth <= wrap.clientWidth + 1
    && wordsFit();
  const min = +title.dataset.min || 64, max = +title.dataset.max || 150, maxScale = +title.dataset.maxScale || 1.3;
  let size = parseFloat(getComputedStyle(title).fontSize), scale = 1, guard = 0;
  const apply = () => { title.style.fontSize = size + 'px'; document.body.style.setProperty('--s', scale.toFixed(3)); };
  while (!wordsFit() && size > min) { size -= 3; apply(); }
  // Mot démesuré, trop large même au plus petit : là seulement, on accepte de le couper.
  if (!wordsFit()) title.style.overflowWrap = 'anywhere';
  while (!fits() && guard++ < 160) {
    if (size > min) size -= 3; else if (scale > 0.74) scale -= 0.03; else break;
    apply();
  }
  if (!fits()) return {ok: false, title: size, scale: scale};
  if (body) {
    // Place libre = bas de la zone utile - bas du contenu quand il est collé sous le titre.
    body.style.justifyContent = 'flex-start';
    const free = () => {
      const last = body.lastElementChild; if (!last) return 0;
      const pad = parseFloat(getComputedStyle(wrap).paddingBottom);
      return wrap.getBoundingClientRect().bottom - pad - last.getBoundingClientRect().bottom;
    };
    const target = 90;   // on garde un peu d'air entre le titre et le corps
    guard = 0;
    while (guard++ < 200 && free() > target) {
      const s0 = size, k0 = scale;
      if (size < max) size += 3; else if (scale < maxScale) scale += 0.02; else break;
      apply();
      if (!fits() || free() < 40) { size = s0; scale = k0; apply();
        if (s0 < max && scale < maxScale) { scale += 0.02; apply(); if (!fits() || free() < 40) { scale = k0; apply(); break; } }
        else break; }
    }
    body.style.justifyContent = '';
  }
  return {ok: fits(), title: size, scale: scale};
}"""


def _vars(theme: dict[str, str], extra: dict[str, str] | None = None) -> str:
    pairs = {"--bg": theme["bg"], "--glow": theme["glow"], "--ink": theme["ink"], "--muted": theme["muted"],
             "--accent": theme["accent"], "--accent-ink": theme["accent_ink"], "--accent-text": theme["accent_text"],
             "--panel": theme["panel"], "--panel-ink": theme["panel_ink"], "--line": theme["line"],
             "--badge-bg": theme["badge_bg"], "--badge-ink": theme["badge_ink"],
             "--dot": theme.get("dot", theme["accent"])}
    pairs.update(extra or {})
    return ";".join(f"{k}:{v}" for k, v in pairs.items())


def _note_html(note: str) -> str:
    return f'<div class="note">{_ICON_INFO}<span>{_esc(note)}</span></div>' if (note or "").strip() else ""


def _title_size(title: str, big: int, small: int) -> int:
    """Taille de départ du titre selon sa longueur (le script d'ajustement affine)."""
    n = len(plain_title(title))
    if n <= 22:
        return big
    if n <= 40:
        return int(big * 0.88)
    if n <= 60:
        return int(big * 0.76)
    return small


def build_html(card: dict, *, theme: str, badge: str, handle: str, image_data_uri: str | None = None,
               source: str = "", chrome: tuple[str, str, str] | None = None) -> str:
    """HTML complet d'une carte. `card` : {"type", "title", "lines": [...], "note"}."""
    t = THEMES.get(theme) or THEMES[THEME_ORDER[0]]
    ctype = card.get("type")
    title = str(card.get("title") or "")
    lines = [str(x) for x in (card.get("lines") or []) if str(x).strip()]
    note = str(card.get("note") or "")
    head = (f'<div class="head"><span class="badge"><i></i>{_esc(badge)}</span>'
            f'<span class="handle">@{_esc(handle.lstrip("@"))}</span></div>')
    extra_vars: dict[str, str] = {}
    wrap_class = "wrap"

    if ctype == "prompt":
        size = _title_size(title, 104, 76)
        body = (
            '<div class="composer"><div class="label">Copy this</div>'
            f'<div class="ptext">&ldquo;{_esc(lines[0] if lines else "")}&rdquo;</div>'
            f'<div class="row"><div class="plus">{_ICON_PLUS}</div><div class="right">{_ICON_MIC}'
            f'<div class="send">{_ICON_SEND.format(c=t["accent_ink"])}</div></div></div></div>{_note_html(note)}')
    elif ctype in ("steps", "list"):
        size = _title_size(title, 104, 74)
        rows = "".join(
            f"<li><b>{i if ctype == 'steps' else _ICON_ARROW}</b><span>{_esc(x)}</span></li>"
            for i, x in enumerate(lines, start=1))
        body = f'<ul class="rows">{rows}</ul>{_note_html(note)}'
    elif ctype == "fact":
        size = _title_size(title, 132, 88)
        solo = not lines
        wrap_class = "wrap fact solo" if solo else "wrap fact"
        paras = "".join(f"<p>{_esc(x)}</p>" for x in lines)
        body = (f'<div class="lines">{paras}</div>' if paras else ('<div class="bar"></div>' if solo else "")) \
            + _note_html(note)
    elif ctype == "phone":
        size = _title_size(title, 88, 66)
        wrap_class = "wrap phonecard"
        bg, ink, pill = chrome or ("#FFFFFF", "#000000", "#EEEEF1")
        extra_vars = {"--chrome": bg, "--chrome-ink": ink, "--pill": pill}
        body = (
            '<div class="phone"><div class="screen">'
            f'<div class="status"><span>9:41</span><span class="island"></span>'
            f'<span class="sys">{_ICON_SIGNAL}{_ICON_WIFI}{_ICON_BATTERY}</span></div>'
            f'<div class="urlbar">{_ICON_LOCK}<span>{_esc(source)}</span></div>'
            f'<div class="page"><img src="{image_data_uri or ""}" alt=""></div>'
            '</div></div>')
    else:
        raise ValueError(f"type de carte inconnu : {ctype!r}")

    min_size = 58 if ctype == "phone" else 64
    max_size = {"fact": 176, "phone": 92}.get(ctype, 150)
    if "solo" in wrap_class:
        max_size = 210
    title_block = (f'<div class="title" data-min="{min_size}" data-max="{max_size}" '
                   f'style="font-size:{size}px">{title_html(title)}</div>')
    inner = head + title_block + (body if ctype == "phone" else f'<div class="body">{body}</div>')
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<style>{_fonts_css()}{_CSS}</style></head>"
        f'<body style="{_vars(t, extra_vars)}"><div class="glow"></div><div class="glow b"></div>'
        f'<div class="{wrap_class}">{inner}</div></body></html>')


# ---------------------------------------------------------------------------
# Rendu (Chromium headless, sans réseau)
# ---------------------------------------------------------------------------
def launch_options() -> dict:
    """Options de lancement de Chromium. XBOT_BROWSER_PATH / XBOT_BROWSER_PROXY ne
    servent qu'aux environnements de test (navigateur préinstallé, proxy)."""
    launch: dict = {"headless": True, "args": ["--disable-dev-shm-usage", "--no-first-run", "--disable-extensions"]}
    if os.getenv("XBOT_BROWSER_PATH"):
        launch["executable_path"] = os.environ["XBOT_BROWSER_PATH"]
    if os.getenv("XBOT_BROWSER_PROXY"):
        launch["proxy"] = {"server": os.environ["XBOT_BROWSER_PROXY"]}
    return launch


LAST_ERROR = ""   # dernière raison d'échec d'un rendu (affichée par `--check`)


def _render_in_browser(page_html: str, out_path: Path) -> bool:
    global LAST_ERROR
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        LAST_ERROR = "Playwright n'est pas installé"
        log.warning("Carte impossible : Playwright n'est pas installé")
        return False
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_options())
        try:
            page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=RENDER_SCALE)
            # Rien ne doit sortir sur le réseau : tout est dans la page.
            page.route(re.compile(r"^https?://", re.I), lambda route: route.abort())
            page.set_content(page_html, wait_until="load", timeout=20000)
            page.evaluate("() => document.fonts.ready.then(() => true)")
            fit = page.evaluate(_FIT_JS) or {}
            if not fit.get("ok"):
                LAST_ERROR = "le texte ne tient pas sur la carte"
                log.warning("Carte refusée : le texte ne tient pas (titre %s px, échelle %s)",
                            fit.get("title"), fit.get("scale"))
                return False
            page.screenshot(path=str(out_path), clip={"x": 0, "y": 0, "width": W, "height": H})
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
    return True


def _shrink_if_too_big(path: Path) -> None:
    """X refuse au-delà de 5 Mo : on réduit à 1,5× puis 1× si besoin."""
    try:
        if path.stat().st_size <= MAX_PNG_BYTES:
            return
        from PIL import Image
        for factor in (1.5, 1.0):
            img = Image.open(path).convert("RGB")
            img = img.resize((int(W * factor), int(H * factor)), Image.LANCZOS)
            img.save(path, "PNG", optimize=True)
            if path.stat().st_size <= MAX_PNG_BYTES:
                return
    except Exception as e:  # noqa: BLE001
        log.warning("Carte : réduction impossible (%s)", type(e).__name__)


def render_html(page_html: str, out_path: Path, timeout_s: float = RENDER_TIMEOUT_S) -> bool:
    """Rend `page_html` en PNG dans un thread de travail borné dans le temps.
    True si `out_path` existe et fait la bonne taille. Ne lève jamais."""
    result: dict[str, bool] = {}

    def work() -> None:
        global LAST_ERROR
        try:
            result["ok"] = _render_in_browser(page_html, out_path)
        except Exception as e:  # noqa: BLE001
            LAST_ERROR = str(e).splitlines()[0][:160] if str(e) else type(e).__name__
            log.warning("Carte : rendu échoué (%s)", LAST_ERROR)
            result["ok"] = False

    worker = threading.Thread(target=work, daemon=True, name="card-render")
    with utils.heartbeat("rendu de carte", every_s=30.0, max_total_s=timeout_s + 30):
        worker.start()
        worker.join(timeout_s)
    if worker.is_alive():
        global LAST_ERROR
        LAST_ERROR = f"rendu trop long (plus de {timeout_s:.0f} s)"
        log.warning("Carte abandonnée : plus de %.0f s", timeout_s)
        return False
    if not result.get("ok") or not out_path.is_file():
        return False
    _shrink_if_too_big(out_path)
    return True


def chrome_colors(image_path: Path) -> tuple[str, str, str]:
    """Couleurs de la barre du téléphone d'après le haut de la capture (comme un
    vrai navigateur mobile) : (fond, texte, pastille d'adresse)."""
    try:
        from PIL import Image
        img = Image.open(image_path).convert("RGB")
        strip = img.crop((0, 0, img.width, max(1, min(6, img.height)))).resize((1, 1), Image.BILINEAR)
        r, g, b = strip.getpixel((0, 0))
    except Exception:  # noqa: BLE001
        return "#FFFFFF", "#000000", "#EEEEF1"
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    dark = lum < 140
    ink = "#FFFFFF" if dark else "#000000"
    mix = 255 if dark else 0
    pr, pg, pb = (int(c + (mix - c) * 0.10) for c in (r, g, b))
    return f"#{r:02X}{g:02X}{b:02X}", ink, f"#{pr:02X}{pg:02X}{pb:02X}"


def _image_data_uri(path: Path) -> str:
    mime = "image/jpeg" if Path(path).suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")


def render(card: dict, out_path: Path, *, theme: str, badge: str, handle: str,
           shot_path: Path | None = None, source: str = "") -> bool:
    """Rend une carte en PNG. `shot_path` + `source` : seulement pour le type « phone »."""
    try:
        if card.get("type") == "phone":
            if not shot_path or not Path(shot_path).is_file():
                return False
            page_html = build_html(card, theme=theme, badge=badge, handle=handle,
                                   image_data_uri=_image_data_uri(Path(shot_path)), source=source,
                                   chrome=chrome_colors(Path(shot_path)))
        else:
            page_html = build_html(card, theme=theme, badge=badge, handle=handle)
    except Exception as e:  # noqa: BLE001
        log.warning("Carte : construction impossible (%s)", type(e).__name__)
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return render_html(page_html, out_path)


SELF_TEST_CARD = {"type": "list", "title": "Card *check*", "lines": ["First line", "Second line"], "note": ""}


def self_test() -> tuple[bool, str]:
    """Dessine une carte d'essai dans un dossier temporaire (commande `--check`) :
    (True, "") si le serveur sait faire les images, sinon (False, raison)."""
    import tempfile
    try:
        with tempfile.TemporaryDirectory(prefix="xbot-card-") as tmp:
            if render(SELF_TEST_CARD, Path(tmp) / "check.png", theme=THEME_ORDER[0], badge="Check",
                      handle="check"):
                return True, ""
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__
    return False, LAST_ERROR or "Chromium n'a pas pu dessiner la carte"
