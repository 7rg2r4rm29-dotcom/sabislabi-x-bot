"""Voix off des vidéos explicatives (accord du propriétaire, 23/11/2026 : « Oui, ajoute-la »).

Kokoro (modèle de synthèse vocale, licence Apache 2.0), lu par le paquet kokoro-onnx sur le
processeur du serveur, sans réseau. Les fichiers du modèle sont dans ~/xbot-models/kokoro/ : HORS
du dossier du bot, pour qu'une installation ne les efface jamais. Ils viennent de
github.com/thewh1teagle/kokoro-onnx/releases (model-files-v1.0) et s'installent une fois avec
`deploy/install_voice.sh` (tailles et empreintes vérifiées). Mesuré sur le serveur le 23/11/2026 :
18 s de calcul pour 11 s de voix, 670 Mo de mémoire au plus.

La synthèse tourne dans un processus à part (`python -m bot.voice TRAVAIL.json`) : sa mémoire est
rendue dès la fin, et un plantage ne touche pas le bot. Au moindre problème, la vidéo part sans voix.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import re
import subprocess
import sys
import wave
from pathlib import Path

from . import utils

log = logging.getLogger("xbot.voice")

MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"
MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
MODEL_SHA256 = {MODEL_FILE: "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
                VOICES_FILE: "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"}
DEFAULT_VOICE = "af_heart"       # la voix la mieux notée du modèle (anglais américain)
VOICE_LANG = "en-us"
SPEED = 1.0
SYNTH_TIMEOUT_S = 600
_VOICE_RE = re.compile(r"^[a-z]{2}_[a-z]+$")


def model_dir(settings) -> Path:
    raw = str(getattr(settings, "voice_model_dir", "") or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / "xbot-models" / "kokoro"


def voice_name(settings) -> str:
    name = str(getattr(settings, "explainer_voice", "") or "").strip().lower()
    return name if _VOICE_RE.match(name) else DEFAULT_VOICE


# ---------------------------------------------------------------------------
# Ce qui est écrit → ce qui se dit
# ---------------------------------------------------------------------------
# La voix lit les signes tels quels : « $29,990 » devenait « dollar twenty-nine thousand… », « GTA VI »
# « G T A V I », « 3x » « three ex », « vs. » « V S » (vérifié le 24/11/2026 sur les sons calculés par le
# modèle). `spoken` écrit ces signes en toutes lettres pour la voix seulement : mêmes mots, même sens,
# rien de nouveau à relire ; l'écran garde le texte d'origine.
_CURRENCIES = {"$": ("dollar", "dollars"), "€": ("euro", "euros"), "£": ("pound", "pounds"), "¥": ("yen", "yen")}
_SCALES = {"k": "thousand", "m": "million", "mn": "million", "b": "billion", "bn": "billion"}
_MONEY_RE = re.compile(r"(\b[Aa]n?\s+)?(?<![\w$€£¥])(?:US)?([$€£¥])\s?(\d[\d,]*(?:\.\d+)?)"
                       r"(?:\s?(thousand|million|billion|trillion)\b|([kKmMbB]n?)\b)?")
_ROMANS = {"II": 2, "III": 3, "IV": 4, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "XI": 11, "XII": 12, "XIII": 13,
           "XIV": 14, "XV": 15, "XVI": 16}
_ROMAN_RE = re.compile(r"\b([A-Z][\w’'.-]*)\s+(XVI|XIV|XIII|XII|XI|XV|IX|VIII|VII|VI|IV|III|II)\b")
_UNITS = {"km/h": ("kilometer per hour", "kilometers per hour"), "mph": ("mile per hour", "miles per hour"),
          "ft/s": ("foot per second", "feet per second"), "m/s": ("meter per second", "meters per second"),
          "kWh": ("kilowatt hour", "kilowatt hours"), "lbs": ("pound", "pounds"), "lb": ("pound", "pounds"),
          "kg": ("kilogram", "kilograms"), "km": ("kilometer", "kilometers"), "cm": ("centimeter", "centimeters"),
          "mm": ("millimeter", "millimeters"), "ft": ("foot", "feet"), "KB": ("kilobyte", "kilobytes"),
          "MB": ("megabyte", "megabytes"), "GB": ("gigabyte", "gigabytes"), "TB": ("terabyte", "terabytes")}
_UNIT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s?(" + "|".join(re.escape(u) for u in sorted(_UNITS, key=len, reverse=True))
                      + r")(?![\w/])")
_PER = {"mo": "month", "yr": "year"}
_PER_RE = re.compile(r"\s?/\s?(month|year|week|day|hour|minute|second|person|mo|yr)\b")
_SPOKEN_SUBS = (
    (re.compile(r"\bGTA\s+V\b"), "GTA 5"),
    (re.compile(r"\b24/7\b"), "24 7"),
    (re.compile(r"(?<=\d)\s?°\s?C\b"), " degrees Celsius"),
    (re.compile(r"(?<=\d)\s?°\s?F\b"), " degrees Fahrenheit"),
    (re.compile(r"(?<=\d)\s?°"), " degrees"),
    (re.compile(r"(?<=\d)x\b"), " times"),                    # « 3x faster » ; « 4x4 », « 1080x1920 » restent
    (re.compile(r"(?<=\d)\s?×(?!\s?\d)"), " times"),
    (re.compile(r"\bvs\.?(?!\w)", re.I), "versus"),
    (re.compile(r"\bNo\.\s?(?=\d)"), "number "),
    (re.compile(r"(?<!\w)#(?=\d)"), "number "),
    (re.compile(r"[~≈]\s?(?=\d)"), "about "),
    (re.compile(r"[→←↑↓•·▶►]"), " "),
)


def _money(m: re.Match) -> str:
    article = m.group(1) or ""
    one, many = _CURRENCIES[m.group(2)]
    number = m.group(3)
    scale = m.group(4) or _SCALES.get((m.group(5) or "").lower())
    if article:                                           # « a $299 plan » : « a 299 dollar plan »
        return f"{article}{number} {scale + ' ' if scale else ''}{one}"
    if scale:
        return f"{number} {scale} {many}"
    whole, _, cents = number.partition(".")
    units = int(whole.replace(",", "") or 0)
    if len(cents) == 2 and m.group(2) == "$":             # « $4.99 » : dollars et cents
        if units == 0:
            return f"{int(cents)} cents"
        main = f"{whole} {one if units == 1 else many}"
        return main if cents == "00" else f"{main} and {int(cents)} cents"
    return f"{number} {one if (units == 1 and not cents) else many}"


def _unit(m: re.Match) -> str:
    one, many = _UNITS[m.group(2)]
    return f"{m.group(1)} {one if m.group(1) == '1' else many}"


def spoken(text: str) -> str:
    """Le texte tel que la voix doit le dire : montants (« $29,990 » → « 29,990 dollars »), chiffres
    romains après un nom (« GTA VI » → « GTA 6 »), « 3x » → « 3 times », « vs. » → « versus »,
    unités abrégées, flèches retirées. Les mots, eux, ne changent jamais."""
    out = str(text or "")
    out = _MONEY_RE.sub(_money, out)
    out = _ROMAN_RE.sub(lambda m: f"{m.group(1)} {_ROMANS[m.group(2)]}", out)
    out = _UNIT_RE.sub(_unit, out)
    out = _PER_RE.sub(lambda m: f" per {_PER.get(m.group(1), m.group(1))}", out)
    for rx, repl in _SPOKEN_SUBS:
        out = rx.sub(repl, out)
    return " ".join(out.split())


def problem(settings) -> str | None:
    """Pourquoi la voix off ne peut pas servir, en français ; None si elle est prête."""
    if not getattr(settings, "explainer_voice_enabled", False):
        return "voix off coupée (EXPLAINER_VOICE_ENABLED)"
    if importlib.util.find_spec("kokoro_onnx") is None:
        return "paquet kokoro-onnx absent"
    folder = model_dir(settings)
    for name in (MODEL_FILE, VOICES_FILE):
        if not (folder / name).is_file():
            return f"fichier du modèle absent ({folder / name}) : lancer deploy/install_voice.sh"
    return None


def synthesize(settings, texts: list[str], out_dir: Path) -> list[tuple[Path, float]] | None:
    """La voix de chaque texte, dans l'ordre : [(fichier WAV, durée en secondes)]. None si la voix
    n'a pas pu se faire (la vidéo part alors sans voix). Ne lève jamais d'exception."""
    texts = [spoken(t) for t in texts or []]
    if not texts or not all(texts):
        return None
    why = problem(settings)
    if why:
        log.info("Voix off : %s", why)
        return None
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        folder = model_dir(settings)
        job = {"texts": texts, "out": str(out_dir), "voice": voice_name(settings), "speed": SPEED,
               "lang": VOICE_LANG, "model": str(folder / MODEL_FILE), "voices": str(folder / VOICES_FILE)}
        job_path = out_dir / "job.json"
        job_path.write_text(json.dumps(job), encoding="utf-8")
        with utils.heartbeat("voix off", every_s=30.0, max_total_s=SYNTH_TIMEOUT_S + 60):
            res = subprocess.run([sys.executable, "-m", "bot.voice", str(job_path)], cwd=str(utils.PROJECT_ROOT),
                                 capture_output=True, text=True, timeout=SYNTH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        log.warning("Voix off : trop longue (plus de %d s), vidéo sans voix", SYNTH_TIMEOUT_S)
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("Voix off impossible à lancer (%s), vidéo sans voix", type(e).__name__)
        return None
    if res.returncode != 0:
        lines = (res.stderr or "").strip().splitlines()
        log.warning("Voix off impossible (%s), vidéo sans voix", lines[-1][:160] if lines else res.returncode)
        return None
    try:
        files = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))["files"]
        out = [(Path(f["path"]), float(f["seconds"])) for f in files]
    except Exception as e:  # noqa: BLE001
        log.warning("Voix off : résultat illisible (%s), vidéo sans voix", type(e).__name__)
        return None
    if len(out) != len(texts) or not all(p.is_file() and s > 0 for p, s in out):
        log.warning("Voix off : %d fichier(s) pour %d texte(s), vidéo sans voix", len(out), len(texts))
        return None
    log.info("Voix off faite (%s) : %d phrase(s), %.0f s de voix", job["voice"], len(out), sum(s for _p, s in out))
    return out


def self_test(settings) -> tuple[bool, str]:
    """`--check` : une petite phrase dite pour de vrai. (True, voix) ou (False, la raison)."""
    import tempfile
    why = problem(settings)
    if why:
        return False, why
    with tempfile.TemporaryDirectory(prefix="xbot-voice-check-") as tmp:
        res = synthesize(settings, ["This is a test."], Path(tmp))
    if not res:
        return False, "la synthèse a échoué (voir le journal)"
    return True, f"{voice_name(settings)}, {res[0][1]:.1f} s de voix"


# ---------------------------------------------------------------------------
# Le processus à part : `python -m bot.voice TRAVAIL.json`
# ---------------------------------------------------------------------------
def _pcm16(samples) -> bytes:
    """Échantillons (-1..1) → PCM 16 bits petit-boutiste."""
    try:
        import numpy as np
        return (np.clip(np.asarray(samples, dtype="float32"), -1.0, 1.0) * 32767).astype("<i2").tobytes()
    except ImportError:
        import array
        pcm = array.array("h", (int(max(-1.0, min(1.0, float(x))) * 32767) for x in samples))
        if sys.byteorder != "little":
            pcm.byteswap()
        return pcm.tobytes()


def write_wav(path: Path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm)


def run_job(job: dict) -> dict:
    from kokoro_onnx import Kokoro
    engine = Kokoro(job["model"], job["voices"])
    out = Path(job["out"])
    files = []
    for i, text in enumerate(job["texts"]):
        samples, rate = engine.create(text, voice=job["voice"], speed=float(job["speed"]), lang=job["lang"])
        path = out / f"beat_{i:03d}.wav"
        write_wav(path, _pcm16(samples), rate)
        files.append({"path": str(path), "seconds": len(samples) / float(rate), "rate": int(rate)})
    return {"files": files}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage : python -m bot.voice TRAVAIL.json", file=sys.stderr)
        return 2
    job = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    result = run_job(job)
    (Path(job["out"]) / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
