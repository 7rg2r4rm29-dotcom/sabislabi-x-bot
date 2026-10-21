"""Garde-fous éditoriaux appliqués à TOUT texte avant de te le proposer.

Repris de la v1 (ligne éditoriale d'octobre 2026) : zéro crypto/NFT, zéro
politique/drames, rien de contraire à la foi du propriétaire, pas de jargon,
pas de chiffres empilés, pas de copie d'une source, pas de fuite JSON.
Nouveau en v2 : pas de lien (0,20 $ le post via l'API au lieu de 0,015 $ ; l'algorithme,
lui, ne pénalise plus les liens), et
longueur pondérée comme X la compte.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata

log = logging.getLogger("xbot.filters")

MAX_WEIGHTED = 280

# ---------------------------------------------------------------------------
# Similarité (anti-plagiat / anti-répétition)
# ---------------------------------------------------------------------------
def _normalize_words(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def is_too_similar(candidate: str, sources: list[str], threshold: float = 0.7) -> bool:
    cand = _normalize_words(candidate)
    if len(cand) < 3:
        return False
    for src in sources:
        sw = _normalize_words(src)
        if sw and len(cand & sw) / len(cand) > threshold:
            return True
    return False


# ---------------------------------------------------------------------------
# Nettoyage
# ---------------------------------------------------------------------------
def clean_output(raw: str) -> str:
    """Guillemets englobants, hashtags, @mentions, espaces. Garde les sauts de ligne."""
    t = (raw or "").strip()
    t = re.sub(r"^(here'?s (the |a |your )?(post|reply|tweet)\s*:?\s*|(post|reply|tweet)\s*:\s*)", "", t,
               flags=re.I).strip()
    for q in ('"', "'", "“", "”", "‘", "’", "`"):
        if t.startswith(q) and t.endswith(q) and len(t) > 2:
            t = t[1:-1].strip()
    t = re.sub(r"(?<![\w&])#\w+", "", t)
    t = re.sub(r"(?<![\w.])@\w+", "", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t


def strip_thread_numbering(t: str) -> str:
    return re.sub(r"^\s*\d+\s*[/.)]\s*\d*\s*[-–:]?\s*", "", t or "").strip()


# ---------------------------------------------------------------------------
# Jargon / chiffres
# ---------------------------------------------------------------------------
_JARGON_PATTERNS = [
    r"\bopen interest\b", r"\bfunding rates?\b", r"\bliquidity cascades?\b",
    r"\bliquidation heat ?maps?\b", r"\bdominance thesis\b", r"\border ?books?\b", r"\bperps?\b",
    r"\bperpetuals?\b", r"\bbasis trade\b", r"\bon-?chain flows?\b", r"\brealized cap\b",
    r"\bMVRV\b", r"\bSOPR\b", r"\bTVL\b", r"\bMEV\b", r"\brollups?\b",
]
_JARGON_RE = re.compile("|".join(_JARGON_PATTERNS), re.I)


def find_jargon(text: str) -> str | None:
    m = _JARGON_RE.search(text or "")
    return m.group(0) if m else None


def too_many_numbers(text: str, max_numbers: int = 1) -> bool:
    nums = re.findall(r"(?<![:\w])\$?\d[\d,.]*\s*[kKmMbB%]?(?![:\w])", text or "")
    big = [n for n in nums if re.search(r"[kKmMbB%$]|\d{3,}", n)]
    return len(big) > max_numbers


# ---------------------------------------------------------------------------
# Ligne éditoriale : crypto, sujets sensibles, foi
# ---------------------------------------------------------------------------
_CRYPTO_RE = re.compile("|".join([
    r"\bnfts?\b", r"\bcrypto(?!graph)\w*", r"\bbitcoin\w*", r"\bbtc\b", r"\bethereum\b", r"\beth\b",
    r"\bsolana\b", r"\bmeme ?coins?\b", r"\baltcoins?\b", r"\bshitcoins?\b", r"\bstablecoins?\b",
    r"\bweb ?3\b", r"\bdefi\b", r"\bblockchains?\b", r"\bon-?chain\b", r"\bairdrops?\b", r"\bdegens?\b",
    r"\bwagmi\b", r"\bngmi\b", r"\bhodl\w*", r"\brug ?pulls?\b", r"\brugged\b", r"\bopensea\b",
    r"\bbinance\b", r"\bcoinbase\b", r"\bmetamask\b", r"\bdogecoin\b", r"\bsatoshi\b", r"\busdt\b",
    r"\busdc\b", r"\bjpegs\b", r"\bfloor prices?\b", r"\bseed phrase\b", r"\bfrens\b", r"\bser\b",
    r"\banon\b", r"\blfg\b", r"\brekt\b",
    # Paris / trading spéculatif (proches du crypto, contraires à la ligne éditoriale)
    r"\bpolymarket\b", r"\bkalshi\b", r"\bprediction markets?\b", r"\b(?:day|swing|copy)[ -]?trad\w*",
    r"\bforex\b", r"\bxrp\b", r"\btokens? (?:sale|launch)\b", r"\bico\b", r"\bmemestocks?\b",
]), re.I)

_SENSITIVE_RE = re.compile("|".join([
    r"\belections?\b", r"\btrump\b", r"\bbiden\b", r"\bkamala\b", r"\bmacron\b", r"\bputin\b",
    r"\bnetanyahu\b", r"\bzelensk\w*", r"\bgaza\b", r"\bisrael\w*", r"\bpalestin\w*", r"\bukrain\w*",
    r"\bhamas\b", r"\bhezbollah\b", r"\bterroris\w*", r"\bterror attacks?\b", r"\bmass shooting\b",
    r"\bgenocide\b", r"\babortion\b", r"\bimmigra\w*", r"\bdeport\w*", r"\bsuicide\b", r"\bmassacre\b",
    r"\bwar crimes?\b",
]), re.I)

# Volontairement littéral : « Apple's big gamble » ou « betting on AI » restent
# autorisés ; « gambling », « sports betting », « casino »… ne le sont pas.
_FAITH_RE = re.compile("|".join([
    r"\bporn\w*", r"\bnsfw\b", r"\bonlyfans\b", r"\bnudes?\b", r"\bnudity\b", r"\bsex\w*",
    r"\bhookups?\b", r"\bescorts?\b", r"\bstrip ?clubs?\b", r"\bcasinos?\b", r"\bgambling\b",
    r"\bsports ?betting\b", r"\bbetting (?:apps?|sites?|odds|tips|slips?)\b", r"\bsportsbooks?\b",
    r"\bbookmakers?\b", r"\bpoker\b", r"\blotter(?:y|ies)\b", r"\bjackpots?\b", r"\broulette\b",
    r"\bblackjack\b", r"\bslot machines?\b", r"\bparlays?\b", r"\bbet365\b", r"\bbeers?\b",
    r"\bwines?\b", r"\bvodka\b", r"\bwhiske?y\b", r"\btequila\b", r"\bcocktails?\b", r"\balcohol\w*",
    r"\bdrunk\b", r"\bbooze\b", r"\bpork\b", r"\bbacon\b", r"\bcannabis\b", r"\bmarijuana\b",
    r"\bcocaine\b", r"\bhoroscopes?\b", r"\bastrolog\w*", r"\bzodiac\b", r"\btarot\b",
    r"\bpayday loans?\b", r"\bbuy now,? pay later\b",
]), re.I)

# Sujets où l'on ne veut JAMAIS réagir, même intelligemment (radar / actu).
HARD_BLOCK = (
    "election", "vote", "ballot", "president", "senate", "congress", "parliament", "democrat",
    "republican", "trump", "biden", "macron", "putin", "netanyahu", "israel", "gaza", "palestine",
    "ukraine", "russia", " war ", "ceasefire", "hamas", "shooting", "shot dead", "killed", "died",
    "death", " dead ", "rip ", "r.i.p", "funeral", "murder", "stabbed", "terror", "bomb", "explosion",
    "earthquake", "hurricane", "flood", "wildfire", "tsunami", "victims", "cancer", "hospital",
    "suicide", "overdose", "pandemic", "outbreak", "abortion", "immigration", "migrant", "deport",
    "racis", "genocide", "islam", "muslim", "christian", "jewish", "church", "mosque", "bible",
    "quran", "police brutality", "protest", "riot", "arrested", "indicted", "lawsuit",
)

NOISE_PATTERNS = (
    "giveaway", "give away", "giving away", "airdrop", "whitelist", " wl ", "like + rt", "like and rt",
    "like & rt", "rt + like", "retweet and", "drop your wallet", "shill me", "drop the ticker",
    "tag 3", "tag a friend", "follow me", "must follow", "winner will", "comment to win",
    "reply to win", "free mint", "claim your", "claim now",
)


def mentions_crypto(text: str) -> str | None:
    m = _CRYPTO_RE.search(text or "")
    return m.group(0) if m else None


def mentions_sensitive(text: str) -> str | None:
    m = _SENSITIVE_RE.search(text or "")
    return m.group(0) if m else None


def mentions_haram(text: str) -> str | None:
    m = _FAITH_RE.search(text or "")
    return m.group(0) if m else None


def off_limits_term(text: str) -> str | None:
    return mentions_crypto(text) or mentions_sensitive(text) or mentions_haram(text)


def _hard_block_regex(term: str) -> re.Pattern:
    """Le mot-clé doit COMMENCER un mot (« killed » ne touche pas « skilled », ni
    « shooting » « troubleshooting », ni « rip » « trip »). Espace final = mot entier."""
    core = term.strip()
    right = r"(?![a-z0-9])" if term.endswith(" ") else ""
    return re.compile(r"(?<![a-z0-9])" + re.escape(core) + right, re.I)


_HARD_BLOCK_RES = tuple((t.strip(), _hard_block_regex(t)) for t in HARD_BLOCK if t.strip())


def hard_blocked(text: str) -> str | None:
    for term, rx in _HARD_BLOCK_RES:
        if rx.search(text or ""):
            return term
    return None


def is_noise(text: str) -> bool:
    low = f" {(text or '').lower()} "
    return any(p in low for p in NOISE_PATTERNS)


# ---------------------------------------------------------------------------
# Fuite JSON / format
# ---------------------------------------------------------------------------
def looks_like_json_leak(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    if t.startswith("{") or t.endswith("}"):
        return True
    if re.search(r'"\s*(text|reply|post|reason|angle|image_prompt|visual_url|tweets|visual)\s*"\s*:', t):
        return True
    if re.search(r"\b(text|image_prompt|visual_url|reply|tweets)\s*:\s*\"", t):
        return True
    return "\\n" in t or '\\"' in t


def parse_json_object(raw: str) -> dict | None:
    """Secours si la sortie structurée n'est pas exploitable : premier objet
    JSON {...} d'un texte (fences markdown tolérées)."""
    t = (raw or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(t[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


# ---------------------------------------------------------------------------
# Longueur pondérée (règles twitter-text v3 : la plupart des caractères latins
# comptent 1, le reste — emojis, CJK… — compte 2)
# ---------------------------------------------------------------------------
_LIGHT_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))


def weighted_length(text: str) -> int:
    total = 0
    for ch in unicodedata.normalize("NFC", text or ""):
        cp = ord(ch)
        if 0xFE00 <= cp <= 0xFE0F or cp == 0x200D:  # sélecteurs de variante / ZWJ
            continue
        total += 1 if any(a <= cp <= b for a, b in _LIGHT_RANGES) else 2
    return total


# ---------------------------------------------------------------------------
# Verdict global
# ---------------------------------------------------------------------------
def publish_problem(text: str, *, sources: list[str] | None = None, max_len: int = MAX_WEIGHTED,
                    allow_numbers: int = 1, check_numbers: bool = True) -> str | None:
    """Raison pour laquelle `text` ne doit PAS être proposé, ou None si OK."""
    from .x_api import contains_url

    if not text or not text.strip():
        return "texte vide"
    if looks_like_json_leak(text):
        return "ressemble à du JSON"
    if contains_url(text):
        return "contient un lien ou un nom de domaine (0,20 $ le post via l'API au lieu de 0,015 $)"
    off = off_limits_term(text)
    if off:
        return f"hors ligne éditoriale ({off})"
    jargon = find_jargon(text)
    if jargon:
        return f"jargon ({jargon})"
    if check_numbers and too_many_numbers(text, allow_numbers):
        return "trop de chiffres"
    if weighted_length(text) > max_len:
        return f"trop long ({weighted_length(text)} > {max_len})"
    if sources and is_too_similar(text, sources):
        return "trop proche d'une source"
    return None
