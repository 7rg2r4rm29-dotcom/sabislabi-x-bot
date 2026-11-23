"""Vidéos explicatives animées (consigne du propriétaire, 23/11/2026).

Le propriétaire veut des posts avec des vidéos « éducatives, en mode docu, qui expliquent
des choses intéressantes ». Claude écrit le script (`Writer.draft_explainer` : une
question, 2 à 5 scènes, une fin), tiré des articles et relu comme un post ; ce module
l'anime : texte en grand, chiffre ou mot clé qui surgit, étapes qui apparaissent une à
une, deux côtés comparés, une citation, la photo de l'actu en fond quand il y en a une
(assombrie, avec un lent zoom), sinon un fond animé ; une barre de progression en haut.

Format 4:5 (1080 × 1350, ce qui prend le plus de place dans le fil d'X sur téléphone),
24 images/s, sans voix : la plupart des gens regardent sans le son, tout est écrit à
l'écran (une piste son muette est jointe, comme pour toute vidéo). Pillow dessine chaque
image, ffmpeg (paquet imageio-ffmpeg) assemble la vidéo : rien d'autre à installer, aucun
accès réseau, aucun code venu de Claude n'est exécuté (le script n'est que du texte).

Une mise en page impossible (texte trop long) lève `LayoutError` : la vidéo n'est pas
faite, et le post part avec la photo, ou en texte seul.
"""
from __future__ import annotations

import dataclasses
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import utils

log = logging.getLogger("xbot.explainer")

W, H = 1080, 1350
FPS = 24
SCENE_KINDS = ("fact", "steps", "compare", "quote")
LABEL = "EXPLAINED"
SIGNATURE = "@sabislabi"
FOLLOW = "Follow for the future, explained."

BG = (10, 14, 28)            # bleu nuit
BG2 = (28, 40, 78)
GLOW = (70, 120, 255)        # halo bleu du fond animé
OPEN_QUOTE = chr(0x201C)
DASH = chr(0x2014)
ACCENT = (255, 209, 26)      # jaune
WHITE = (255, 255, 255)
SOFT = (200, 208, 228)
INK = (12, 14, 24)

FADE_S = 0.45                # apparition d'un élément
FADE_OUT_S = 0.3             # fin de scène
WORDS_PER_S = 2.8            # lecture à l'écran, sans se presser
SCENE_MIN_S, SCENE_MAX_S = 3.4, 9.0
STEP_S = 1.2                 # entre deux étapes qui apparaissent
TOTAL_MAX_S = 60.0
RENDER_TIMEOUT_S = 600
VIDEO_MAX_BYTES = 60 * 1024 * 1024
EXPLAINERS_KEEP = 20

FONT_FILES = {
    True: ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
           "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    False: ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf"),
}


class LayoutError(ValueError):
    """Un texte du script ne tient pas à l'écran."""


@dataclass
class Scene:
    kind: str                       # title | fact | steps | compare | quote | end
    text: str = ""
    big: str = ""                   # fact : un chiffre ou un mot, en très grand
    items: list[str] = field(default_factory=list)   # steps : 2 à 4 étapes
    left: str = ""                  # compare : les deux côtés
    right: str = ""
    who: str = ""                   # quote : qui l'a dit
    seconds: float = 0.0


# ---------------------------------------------------------------------------
# Le script → les scènes, et leur durée
# ---------------------------------------------------------------------------
def _clean(text) -> str:
    return " ".join(str(text or "").split())


def _words(*texts: str) -> int:
    return sum(len(_clean(t).split()) for t in texts if t)


def _duration(sc: Scene) -> float:
    words = _words(sc.text, sc.big, sc.left, sc.right, sc.who, *sc.items)
    seconds = 1.5 + words / WORDS_PER_S
    if sc.kind == "steps":
        seconds += STEP_S * max(0, len(sc.items) - 1)
    elif sc.kind == "compare":
        seconds += 1.0
    elif sc.kind == "end":
        seconds += 1.2                # le temps de lire la signature
    return max(SCENE_MIN_S, min(SCENE_MAX_S, seconds))


def scenes_of(plan: dict) -> list[Scene]:
    """Les scènes de la vidéo : le titre, les scènes du script, la fin. Lève LayoutError
    si le script est incomplet."""
    title = _clean(plan.get("title"))
    ending = _clean(plan.get("ending"))
    raw = plan.get("scenes") if isinstance(plan.get("scenes"), list) else []
    if not title or not ending or not raw:
        raise LayoutError("script incomplet (titre, scènes et fin sont nécessaires)")
    out = [Scene("title", text=title)]
    for s in raw:
        if not isinstance(s, dict) or s.get("kind") not in SCENE_KINDS:
            raise LayoutError(f"scène inconnue : {s!r}"[:120])
        sc = Scene(str(s["kind"]), text=_clean(s.get("text")), big=_clean(s.get("big")),
                   items=[_clean(i) for i in (s.get("items") or []) if _clean(i)],
                   left=_clean(s.get("left")), right=_clean(s.get("right")), who=_clean(s.get("who")))
        if sc.kind == "steps" and not 2 <= len(sc.items) <= 4:
            raise LayoutError("une scène d'étapes en demande 2 à 4")
        if sc.kind == "compare" and not (sc.left and sc.right):
            raise LayoutError("une comparaison demande deux côtés")
        if sc.kind == "quote" and not (sc.text and sc.who):
            raise LayoutError("une citation demande le texte et son auteur")
        if sc.kind == "fact" and not sc.text:
            raise LayoutError("une scène sans texte")
        out.append(sc)
    out.append(Scene("end", text=ending))
    for sc in out:
        sc.seconds = _duration(sc)
    total = sum(sc.seconds for sc in out)
    if total > TOTAL_MAX_S:
        k = TOTAL_MAX_S / total
        for sc in out:
            sc.seconds = max(SCENE_MIN_S * 0.8, sc.seconds * k)
    return out


def total_seconds(scenes: list[Scene]) -> float:
    return sum(sc.seconds for sc in scenes)


def layout_problem(plan: dict) -> str | None:
    """Pourquoi le script ne tient pas à l'écran (en français), ou None : la mise en page
    est faite pour de vrai, à la taille de la vidéo, sans rien dessiner d'autre."""
    try:
        lay = Layout()
        for sc in scenes_of(plan):
            lay.elements(sc)
    except LayoutError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return f"mise en page impossible ({type(e).__name__})"
    return None


# ---------------------------------------------------------------------------
# Texte : polices, coupure en lignes, taille qui s'adapte
# ---------------------------------------------------------------------------
_FONTS: dict[tuple[int, bool], object] = {}


def has_real_font() -> bool:
    """La police DejaVu est là (sur le serveur) : guillemets et tirets typographiques. Sinon
    (police de secours de Pillow), des signes simples, que toute police sait dessiner."""
    return any(Path(p).is_file() for p in FONT_FILES[True])


def font(size: int, bold: bool = True):
    from PIL import ImageFont
    size = max(8, int(size))
    key = (size, bold)
    if key not in _FONTS:
        found = None
        for path in FONT_FILES[bold]:
            if Path(path).is_file():
                try:
                    found = ImageFont.truetype(path, size)
                    break
                except OSError:
                    continue
        _FONTS[key] = found or ImageFont.load_default(size=size)
    return _FONTS[key]


def wrap(text: str, fnt, max_w: float) -> list[str] | None:
    """Les lignes du texte à cette taille ; None si un mot seul dépasse la largeur."""
    lines: list[str] = []
    cur = ""
    for word in _clean(text).split():
        if fnt.getlength(word) > max_w:
            return None
        trial = f"{cur} {word}".strip()
        if fnt.getlength(trial) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def fit(text: str, max_w: float, max_lines: int, start: int, smallest: int, bold: bool = True):
    """(police, lignes) : la plus grande taille, de `start` à `smallest`, où le texte tient."""
    size = int(start)
    while size >= int(smallest):
        fnt = font(size, bold)
        lines = wrap(text, fnt, max_w)
        if lines is not None and 0 < len(lines) <= max_lines:
            return fnt, lines
        size -= max(1, int(start) // 20)
    raise LayoutError(f"texte trop long pour l'écran : {_clean(text)[:60]!r}")


def text_block(lines: list[str], fnt, fill, *, align: str = "left", width: int | None = None,
               spacing: float = 1.2, shadow: bool = True):
    """Le bloc de texte, sur fond transparent (avec une ombre : lisible sur une photo)."""
    from PIL import Image, ImageDraw
    asc, desc = fnt.getmetrics()
    lh = int((asc + desc) * spacing)
    w = width or int(max(fnt.getlength(line) for line in lines)) + 6
    h = lh * (len(lines) - 1) + asc + desc + 6
    img = Image.new("RGBA", (max(1, w), max(1, h)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    off = max(2, int(fnt.size / 22)) if hasattr(fnt, "size") else 2
    for i, line in enumerate(lines):
        lw = fnt.getlength(line)
        x = (w - lw) / 2 if align == "center" else 0
        if shadow:
            d.text((x + off, i * lh + off), line, font=fnt, fill=(0, 0, 0, 170))
        d.text((x, i * lh), line, font=fnt, fill=fill)
    return img


# ---------------------------------------------------------------------------
# Mise en page : chaque scène devient des éléments qui apparaissent
# ---------------------------------------------------------------------------
@dataclass
class Element:
    img: object                    # image RGBA
    x: int
    y: int
    start: float = 0.0             # secondes après le début de la scène
    slide: int = 0                 # monte de `slide` pixels en apparaissant
    pop: bool = False              # grossit en apparaissant


class Layout:
    def __init__(self, w: int = W, h: int = H) -> None:
        self.w, self.h = w, h
        self.k = w / W
        self.margin = int(84 * self.k)
        self.inner = w - 2 * self.margin

    def s(self, v: float) -> int:
        return max(1, int(round(v * self.k)))

    # --- éléments fixes ---------------------------------------------------
    def chip(self):
        from PIL import Image, ImageDraw
        fnt = font(self.s(30))
        tw = int(fnt.getlength(LABEL))
        pad_x, pad_y = self.s(18), self.s(10)
        asc, desc = fnt.getmetrics()
        img = Image.new("RGBA", (tw + 2 * pad_x, asc + desc + 2 * pad_y), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius=self.s(10), fill=ACCENT + (255,))
        d.text((pad_x, pad_y), LABEL, font=fnt, fill=INK)
        return img

    # --- scènes --------------------------------------------------------------
    def elements(self, sc: Scene) -> list[Element]:
        fn = getattr(self, f"_{sc.kind}", None)
        if fn is None:
            raise LayoutError(f"scène inconnue : {sc.kind}")
        return fn(sc)

    def _centered(self, blocks: list[tuple[object, int, float, int, bool]], top: int, bottom: int,
                  gap: int) -> list[Element]:
        """Empile des blocs (image, x, départ, glissement, pop) au milieu de [top, bottom]."""
        total = sum(b[0].height for b in blocks) + gap * (len(blocks) - 1)
        if total > bottom - top:
            raise LayoutError("trop de texte dans une scène")
        y = top + (bottom - top - total) // 2
        out = []
        for img, x, start, slide, pop in blocks:
            out.append(Element(img, x, y, start, slide, pop))
            y += img.height + gap
        return out

    def _title(self, sc: Scene) -> list[Element]:
        fnt, lines = fit(sc.text, self.inner, 5, self.s(86), self.s(54))
        block = text_block(lines, fnt, WHITE + (255,))
        y = self.h - self.s(250) - block.height
        if y < self.s(180):
            raise LayoutError("titre trop long")
        from PIL import Image, ImageDraw
        bar = Image.new("RGBA", (self.s(150), self.s(12)), (0, 0, 0, 0))
        ImageDraw.Draw(bar).rectangle((0, 0, bar.width, bar.height), fill=ACCENT + (255,))
        return [Element(block, self.margin, y, 0.1, self.s(40)),
                Element(bar, self.margin, y + block.height + self.s(34), 0.55, 0)]

    def _fact(self, sc: Scene) -> list[Element]:
        blocks = []
        if sc.big:
            bf, bl = fit(sc.big, self.inner, 1, self.s(190), self.s(90))
            blocks.append((text_block(bl, bf, ACCENT + (255,)), self.margin, 0.1, 0, True))
        tf, tl = fit(sc.text, self.inner, 6, self.s(64), self.s(42))
        blocks.append((text_block(tl, tf, WHITE + (255,)), self.margin, 0.45 if sc.big else 0.1, self.s(36), False))
        return self._centered(blocks, self.s(170), self.h - self.s(120), self.s(34))

    def _steps(self, sc: Scene) -> list[Element]:
        from PIL import Image, ImageDraw
        out: list[tuple] = []
        if sc.text:
            hf, hl = fit(sc.text, self.inner, 2, self.s(58), self.s(42))
            out.append((text_block(hl, hf, ACCENT + (255,)), self.margin, 0.1, self.s(24), False))
        dot = self.s(66)
        text_w = self.inner - dot - self.s(30)
        for i, item in enumerate(sc.items):
            f, lines = fit(item, text_w, 3, self.s(50), self.s(36))
            txt = text_block(lines, f, WHITE + (255,))
            row = Image.new("RGBA", (self.inner, max(dot, txt.height)), (0, 0, 0, 0))
            d = ImageDraw.Draw(row)
            d.ellipse((0, 0, dot - 1, dot - 1), fill=ACCENT + (255,))
            nf = font(self.s(36))
            num = str(i + 1)
            asc, desc = nf.getmetrics()
            d.text(((dot - nf.getlength(num)) / 2, (dot - asc - desc) / 2), num, font=nf, fill=INK)
            row.alpha_composite(txt, (dot + self.s(30), max(0, (dot - txt.height) // 2) if txt.height < dot else 0))
            out.append((row, self.margin, 0.5 + i * STEP_S, self.s(30), False))
        return self._centered(out, self.s(170), self.h - self.s(120), self.s(40))

    def _compare(self, sc: Scene) -> list[Element]:
        from PIL import Image, ImageDraw
        blocks: list[tuple] = []
        if sc.text:
            hf, hl = fit(sc.text, self.inner, 2, self.s(58), self.s(42))
            blocks.append((text_block(hl, hf, ACCENT + (255,)), self.margin, 0.1, self.s(24), False))
        gap = self.s(36)
        box_w = (self.inner - gap) // 2
        pad = self.s(28)
        parts = []
        for side in (sc.left, sc.right):
            f, lines = fit(side, box_w - 2 * pad, 6, self.s(52), self.s(34))
            parts.append(text_block(lines, f, WHITE + (255,), shadow=False))
        box_h = max(p.height for p in parts) + 2 * pad
        for n, part in enumerate(parts):
            box = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
            d = ImageDraw.Draw(box)
            d.rounded_rectangle((0, 0, box_w - 1, box_h - 1), radius=self.s(22), fill=(6, 8, 18, 190),
                                outline=(ACCENT if n else SOFT) + (255,), width=self.s(5))
            box.alpha_composite(part, (pad, pad))
            parts[n] = box
        row = Image.new("RGBA", (self.inner, box_h), (0, 0, 0, 0))
        left_el, right_el = parts
        # Les deux côtés apparaissent l'un après l'autre : deux éléments sur la même ligne.
        blocks.append((row, self.margin, 0.0, 0, False))
        els = self._centered(blocks, self.s(170), self.h - self.s(120), self.s(40))
        y = els[-1].y
        els[-1] = Element(left_el, self.margin, y, 0.4, self.s(30))
        els.append(Element(right_el, self.margin + box_w + gap, y, 1.4, self.s(30)))
        return els

    def _quote(self, sc: Scene) -> list[Element]:
        typo = has_real_font()            # guillemet et tiret typographiques, si la police les a
        mark_f = font(self.s(200))
        mark = text_block([OPEN_QUOTE if typo else '"'], mark_f, ACCENT + (255,), shadow=False)
        qf, ql = fit(sc.text, self.inner, 6, self.s(58), self.s(40))
        quote = text_block(ql, qf, WHITE + (255,))
        wf, wl = fit(f"{DASH if typo else '-'} {sc.who}", self.inner, 2, self.s(40), self.s(30), bold=False)
        who = text_block(wl, wf, SOFT + (255,))
        # Le guillemet est grand mais n'occupe que le haut de son bloc : on le rapproche du texte.
        blocks = [(mark.crop((0, 0, mark.width, int(mark.height * 0.62))), self.margin, 0.1, 0, True),
                  (quote, self.margin, 0.35, self.s(30), False),
                  (who, self.margin, 1.0, self.s(20), False)]
        return self._centered(blocks, self.s(170), self.h - self.s(120), self.s(26))

    def _end(self, sc: Scene) -> list[Element]:
        tf, tl = fit(sc.text, self.inner, 5, self.s(70), self.s(46))
        main = text_block(tl, tf, WHITE + (255,), align="center", width=self.inner)
        sig_f = font(self.s(46))
        sig = text_block([SIGNATURE], sig_f, ACCENT + (255,), align="center", width=self.inner)
        fol_f, fol_l = fit(FOLLOW, self.inner, 2, self.s(34), self.s(26), bold=False)
        fol = text_block(fol_l, fol_f, SOFT + (255,), align="center", width=self.inner)
        els = self._centered([(main, self.margin, 0.1, self.s(30), False)], self.s(170), self.h - self.s(330),
                             0)
        y = self.h - self.s(260)
        els.append(Element(sig, self.margin, y, 0.9, self.s(20)))
        els.append(Element(fol, self.margin, y + sig.height + self.s(14), 1.1, self.s(20)))
        return els


# ---------------------------------------------------------------------------
# Fond : la photo de l'actu (assombrie, lent zoom) ou un dégradé animé
# ---------------------------------------------------------------------------
class Background:
    ZOOM = 0.10

    def __init__(self, w: int, h: int, photo: Path | None = None) -> None:
        from PIL import Image
        self.w, self.h = w, h
        self.photo = None          # (nette, floue) : nette sous le titre, floue sous les explications
        if photo is not None:
            try:
                with Image.open(photo) as im:
                    self.photo = self._prepare_photo(im.convert("RGB"))
            except Exception as e:  # noqa: BLE001 — photo illisible : fond animé
                log.info("Vidéo explicative : photo illisible (%s), fond animé", type(e).__name__)
                self.photo = None
        if self.photo is None:
            self.plain = self._gradient()

    def _prepare_photo(self, im):
        from PIL import Image, ImageEnhance, ImageFilter
        bw, bh = int(self.w * (1 + self.ZOOM)), int(self.h * (1 + self.ZOOM))
        scale = max(bw / im.width, bh / im.height)
        im = im.resize((max(bw, int(im.width * scale + 1)), max(bh, int(im.height * scale + 1))), Image.LANCZOS)
        left, top = (im.width - bw) // 2, (im.height - bh) // 2
        im = im.crop((left, top, left + bw, top + bh))
        dark = Image.new("RGB", (bw, bh), BG)
        # Sous le titre : la photo bien visible en haut, puis presque noire dès le milieu, là où le
        # titre s'écrit (une photo claire, ou qui contient du texte, ne doit jamais le gêner).
        shade = Image.linear_gradient("L").resize((bw, bh)).point(
            lambda v: 0 if v < 45 else min(246, int((v - 45) * 3.0)))
        sharp = Image.composite(dark, ImageEnhance.Brightness(im).enhance(0.7), shade)
        # Sous les explications : floue, sombre, teintée aux couleurs du compte, pour que le
        # texte (et seulement lui) se lise.
        soft = ImageEnhance.Brightness(im.filter(ImageFilter.GaussianBlur(max(2, int(26 * bw / W))))).enhance(0.32)
        soft = Image.blend(soft, dark, 0.4)
        return sharp, soft

    def _photo_frame(self, img, p: float):
        from PIL import Image
        z = 1 + self.ZOOM * (1 - p)               # on s'approche doucement
        cw, ch = int(img.width / z), int(img.height / z)
        left, top = (img.width - cw) // 2, (img.height - ch) // 2
        return img.crop((left, top, left + cw, top + ch)).resize((self.w, self.h), Image.BILINEAR)

    def _gradient(self):
        from PIL import Image, ImageDraw, ImageFilter
        gw, gh = self.w, int(self.h * 1.35)
        grad = Image.linear_gradient("L").resize((gw, gh))
        img = Image.composite(Image.new("RGB", (gw, gh), BG2), Image.new("RGB", (gw, gh), BG), grad)
        glow = Image.new("L", (gw, gh), 0)
        r = int(gw * 0.45)
        ImageDraw.Draw(glow).ellipse((gw - r, int(gh * 0.18), gw + r, int(gh * 0.18) + 2 * r), fill=80)
        glow = glow.filter(ImageFilter.GaussianBlur(max(2, r // 3)))
        return Image.composite(Image.new("RGB", (gw, gh), GLOW), img, glow)

    def frame(self, p: float, soft: float = 0.0):
        """L'image de fond à l'instant p (0 → 1 sur toute la vidéo) ; `soft` : 0 = la photo
        nette (titre), 1 = la photo floue (explications), entre les deux pendant le passage."""
        from PIL import Image
        p = min(1.0, max(0.0, p))
        if self.photo is not None:
            sharp, blurred = self.photo
            if soft <= 0.0:
                return self._photo_frame(sharp, p)
            if soft >= 1.0:
                return self._photo_frame(blurred, p)
            return Image.blend(self._photo_frame(sharp, p), self._photo_frame(blurred, p), soft)
        top = int((self.plain.height - self.h) * p)
        return self.plain.crop((0, top, self.w, top + self.h))


# ---------------------------------------------------------------------------
# Assemblage
# ---------------------------------------------------------------------------
def _ease(p: float) -> float:
    p = min(1.0, max(0.0, p))
    return 1 - (1 - p) ** 3


def _draw(frame, el: Element, t: float, fade_out: float) -> None:
    from PIL import Image
    if t < el.start:
        return
    e = _ease((t - el.start) / FADE_S) * fade_out
    if e <= 0.01:
        return
    img, x, y = el.img, el.x, el.y
    if el.pop and e < 1:
        s = 0.82 + 0.18 * e
        img = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), Image.BILINEAR)
        x += (el.img.width - img.width) // 2
        y += (el.img.height - img.height) // 2
    elif el.slide:
        y += int(el.slide * (1 - min(1.0, e / max(fade_out, 0.01))))
    if e < 0.99:
        img = img.copy()
        img.putalpha(img.getchannel("A").point(lambda v: int(v * e)))
    frame.alpha_composite(img, (max(0, x), max(0, y)))


def frames(scenes: list[Scene], photo: Path | None = None, *, size: tuple[int, int] = (W, H), fps: int = FPS):
    """Les images de la vidéo, une à une (RGB)."""
    from PIL import ImageDraw
    w, h = size
    lay = Layout(w, h)
    bg = Background(w, h, photo)
    chip = lay.chip()
    laid = [(sc, lay.elements(sc)) for sc in scenes]       # LayoutError AVANT la 1re image
    total = total_seconds(scenes)
    n_frames = max(1, int(round(total * fps)))
    starts, acc = [], 0.0
    for sc, _els in laid:
        starts.append(acc)
        acc += sc.seconds
    idx = 0
    for f in range(n_frames):
        t = f / fps
        while idx + 1 < len(laid) and t >= starts[idx + 1]:
            idx += 1
        sc, els = laid[idx]
        ts = t - starts[idx]
        fade_out = min(1.0, max(0.0, (sc.seconds - ts) / FADE_OUT_S)) if idx + 1 < len(laid) else 1.0
        soft = 0.0 if idx == 0 else (min(1.0, ts / FADE_S) if idx == 1 else 1.0)
        frame = bg.frame(t / total, soft).convert("RGBA")
        frame.alpha_composite(chip, (lay.margin, lay.s(64)))
        for el in els:
            _draw(frame, el, ts, fade_out)
        ImageDraw.Draw(frame).rectangle((0, 0, int(w * (t + 1 / fps) / total), lay.s(10)), fill=ACCENT + (255,))
        yield frame.convert("RGB")


def render(plan: dict, out: Path, photo: Path | None = None, *, exe: str | None = None,
           size: tuple[int, int] = (W, H), fps: int = FPS) -> str | None:
    """Fait la vidéo `out` (MP4 H.264, son muet AAC, lecture immédiate). None si c'est fait,
    sinon la raison, en français. Ne lève jamais d'exception."""
    try:
        return _render(plan, out, photo, exe=exe, size=size, fps=fps)
    except Exception as e:  # noqa: BLE001
        log.exception("Vidéo explicative : erreur inattendue : %s", e)
        from .visuals import _safe_unlink
        _safe_unlink(out)
        return f"erreur inattendue ({type(e).__name__})"


def _stop(proc) -> bytes:
    """Arrête ffmpeg et rend ce qu'il a écrit sur sa sortie d'erreur (pour dire pourquoi)."""
    for step in (proc.kill, lambda: proc.stdin and proc.stdin.close(), lambda: proc.wait(timeout=15)):
        try:
            step()
        except Exception:  # noqa: BLE001
            pass
    try:
        return proc.stderr.read() if proc.stderr else b""
    except Exception:  # noqa: BLE001
        return b""


def _last_line(err: bytes, default) -> str:
    lines = (err or b"").decode("utf-8", "replace").strip().splitlines()
    return lines[-1][:120] if lines else str(default)


def _render(plan: dict, out: Path, photo: Path | None, *, exe: str | None, size: tuple[int, int],
            fps: int) -> str | None:
    from .visuals import _safe_unlink, ffmpeg_exe, probe_video
    exe = exe or ffmpeg_exe()
    if not exe:
        return "ffmpeg absent"
    try:
        scenes = scenes_of(plan)
        images = frames(scenes, photo, size=size, fps=fps)
        first = next(images)                       # la mise en page est vérifiée ici
    except LayoutError as e:
        return f"mise en page impossible : {e}"
    except Exception as e:  # noqa: BLE001
        log.exception("Vidéo explicative : préparation impossible : %s", e)
        return f"préparation impossible ({type(e).__name__})"
    w, h = size
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "pipe:0",
           "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
           "-map", "0:v:0", "-map", "1:a:0", "-shortest",
           "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-pix_fmt", "yuv420p", "-crf", "21",
           "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", "-map_metadata", "-1", str(out)]
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError as e:
        return f"ffmpeg impossible à lancer ({type(e).__name__})"
    err = b""
    try:
        with utils.heartbeat("vidéo explicative", every_s=30.0, max_total_s=RENDER_TIMEOUT_S + 60):
            for img in _chain(first, images):
                if time.monotonic() - t0 > RENDER_TIMEOUT_S:
                    raise TimeoutError("trop long")
                proc.stdin.write(img.tobytes())
            # communicate() ferme l'entrée de ffmpeg (fin des images) et attend la fin de l'assemblage.
            _out, err = proc.communicate(timeout=180)
    except (TimeoutError, subprocess.TimeoutExpired):
        _stop(proc)
        _safe_unlink(out)
        return "fabrication trop longue"
    except Exception as e:  # noqa: BLE001 — ffmpeg arrêté en route (BrokenPipeError…), image impossible…
        err = _stop(proc)
        _safe_unlink(out)
        return f"assemblage impossible ({_last_line(err, type(e).__name__)})"
    if proc.returncode != 0 or not out.is_file():
        _safe_unlink(out)
        return f"assemblage impossible ({_last_line(err, proc.returncode)})"
    info = probe_video(exe, out)
    if info is None or info.duration < 2.0 or out.stat().st_size > VIDEO_MAX_BYTES:
        _safe_unlink(out)
        return "vidéo inutilisable"
    log.info("Vidéo explicative faite : %d scènes, %.0f s, %.1f Mo, en %.0f s", len(scenes), info.duration,
             out.stat().st_size / 1e6, time.monotonic() - t0)
    return None


def _chain(first, rest):
    yield first
    yield from rest


# ---------------------------------------------------------------------------
# Point d'entrée du planificateur
# ---------------------------------------------------------------------------
def prepare(settings, draft, writer, tag: str = "explainer", *,
            find_photo: Callable | None = None) -> tuple[str | None, str | None, str | None]:
    """(chemin de la vidéo, "video", None). La photo de l'actu sert de fond quand il y en a
    une (cherchée comme pour un post : page officielle, puis l'article ; vérifiée par Claude).
    Vidéo impossible : (photo, "photo", None), sinon (None, None, None) = texte seul.
    Ne lève jamais d'exception."""
    try:
        plan = getattr(draft, "video_plan", None)
        if not isinstance(plan, dict) or not plan:
            return None, None, None
        from . import visuals
        photo = None
        if getattr(draft, "visual", None) is not None and getattr(settings, "photos_enabled", False):
            finder = find_photo or visuals.prepare_media
            # Une photo seulement : ni l'extrait vidéo de l'actu, ni la 2e image (logo, drapeau).
            only_photo = (dataclasses.replace(settings, videos_enabled=False, second_image_enabled=False)
                          if dataclasses.is_dataclass(settings) else settings)
            res = finder(only_photo, draft, writer, tag)
            path, kind = (tuple(res) + (None, None))[:2] if isinstance(res, tuple) else (None, None)
            if path and kind == "photo":
                photo = Path(path)
        folder = utils.MEDIA_DIR / "explainers"
        out = folder / f"{visuals._safe_tag(tag)}_{time.strftime('%Y%m%d-%H%M%S')}.mp4"
        why = render(plan, out, photo)
        if why is None:
            visuals._cleanup_old(folder, EXPLAINERS_KEEP)
            return str(out), "video", None
        log.warning("Vidéo explicative impossible (%s) : post %s", why,
                    "avec la photo de l'actu" if photo else "en texte seul")
        return (str(photo), "photo", None) if photo else (None, None, None)
    except Exception as e:  # noqa: BLE001
        log.exception("Vidéo explicative : erreur inattendue (%s), post en texte seul", e)
        return None, None, None
