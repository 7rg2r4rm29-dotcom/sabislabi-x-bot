"""Garde-fous éditoriaux appliqués à TOUT texte avant de te le proposer.

Repris de la v1 (ligne éditoriale d'octobre 2026) : zéro crypto/NFT, zéro
politique/drames, rien de contraire à la foi du propriétaire, pas de jargon,
pas de chiffres empilés, pas de copie d'une source, pas de fuite JSON.
Novembre 2026 : le compte parle au grand public — `find_tech_jargon` refuse le
vocabulaire de spécialiste (LLM, API, benchmark, open-source…) dans les posts.
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
    """Guillemets englobants, hashtags, @mentions, tirets longs, espaces. Garde les
    sauts de ligne."""
    t = (raw or "").strip()
    t = re.sub(r"^(here'?s (the |a |your )?(post|reply|tweet)\s*:?\s*|(post|reply|tweet)\s*:\s*)", "", t,
               flags=re.I).strip()
    # Guillemets qui entourent TOUT le texte (une seule paire), jamais deux citations.
    for q_open, q_close in (('"', '"'), ("'", "'"), ("`", "`"), ("“", "”"), ("‘", "’")):
        inner = t[1:-1]
        if (len(t) > 2 and t.startswith(q_open) and t.endswith(q_close)
                and q_open not in inner and q_close not in inner):
            t = inner.strip()
    t = re.sub(r"(?<![\w&])#(?=\w*[^\W\d_])\w+", "", t)   # #AI oui, « #1 » non (pas un hashtag)
    t = re.sub(r"(?<![\w.])@\w+", "", t)
    t = re.sub(r"(?<=\d) ?[–—] ?(?=\d)", "-", t)        # 2–3, 2 – 3 → 2-3
    t = re.sub(r"(?<![\w.])[–—](?=\d)", "-", t)         # –5°C : un signe moins
    t = re.sub(r"[ \t]*[—–][ \t]*(?=\S)", ", ", t)      # tiret long → virgule
    t = re.sub(r"[ \t]*[—–][ \t]*$", "", t, flags=re.M)  # en fin de ligne : rien
    t = re.sub(r"^, ", "", t, flags=re.M)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t


# « 1/ », « 2/7 », « 3. », « 4) », « 5/7: » en tête de tweet — suivis d'un espace.
_THREAD_NUMBER_RE = re.compile(r"^\s*(\d{1,2})\s*(?:/\s*(\d{1,2})|[/.)])(?=\s|$|\s*[-–:])\s*[-–:]?\s*")


def strip_thread_numbering(t: str, position: int | None = None) -> str:
    """Retire la numérotation d'un tweet de thread (« 1/ », « 2/7 », « 3. », « 4) »),
    jamais un chiffre du texte : « 2.5 million homes », « 24/7, it never sleeps » restent.
    Une fraction (« 2/3 of people ») reste aussi : « n/m » n'est une numérotation que si
    m ≥ n, que la suite n'est pas « of … » et, avec `position` (rang du tweet), que n
    est ce rang."""
    t = t or ""
    m = _THREAD_NUMBER_RE.match(t)
    if m:
        n, total, rest = int(m.group(1)), m.group(2), t[m.end():]
        fraction = total is not None and (int(total) < n or rest[:3].lower() == "of "
                                           or (position is not None and n != position))
        if not fraction:
            t = rest
    return t.strip()


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


# Vocabulaire de spécialiste, incompréhensible pour M. et Mme Tout-le-monde.
# Sigles : casse exacte (« RAG » oui, « rag » non). Mots : casse indifférente.
# Volontairement absents, car ils ont un sens courant : model, agent, chip,
# prompt, developer (la relecture par Claude s'en charge selon le contexte).
_TECH_ACRONYMS_RE = re.compile(
    r"\b(?:LLMs?|APIs?|SDKs?|RAG|GPUs?|CLI|MCP|SOTA|AGI|ASI|NLP|IDE|OSS|SaaS|B2B|KPIs?|ROI)\b")
_TECH_WORDS_RE = re.compile("|".join([
    r"\bbenchmarks?\b", r"\bbenchmarked\b", r"\bopen[- ]source\b", r"\bopen[- ]weights?\b",
    r"\bfine[- ]?tun\w*", r"\bmulti[- ]?modal\w*", r"\bcontext windows?\b", r"\binference\b",
    r"\btokens?\b", r"\bparameters?\b", r"\bdatasets?\b", r"\bagentic\b", r"\bAI agents?\b",
    r"\bprompt engineering\b", r"\bembeddings?\b", r"\blatency\b", r"\bworkflows?\b",
    r"\buse[- ]cases?\b", r"\bleverag\w*", r"\bhallucinat\w*", r"\bneural net\w*",
    r"\bmachine learning\b", r"\bdeep learning\b", r"\blarge language models?\b",
    r"\blanguage models?\b", r"\bfoundation models?\b", r"\bfrontier models?\b",
    r"\breasoning models?\b", r"\bquantiz\w*", r"\bself[- ]host\w*", r"\bcodebases?\b",
    r"\brepositor(?:y|ies)\b", r"\bpull requests?\b", r"\bend[- ]to[- ]end\b", r"\bscalab\w*",
    r"\bsynerg\w*", r"\bstate[- ]of[- ]the[- ]art\b", r"\bgenerative AI\b", r"\bgen ?AI\b",
]), re.I)


def find_tech_jargon(text: str) -> str | None:
    """Premier mot de jargon tech trouvé dans un texte destiné au grand public."""
    m = _TECH_ACRONYMS_RE.search(text or "") or _TECH_WORDS_RE.search(text or "")
    return m.group(0) if m else None


# Formules toutes faites qui sentent la pub ou la machine : un lecteur les repère, et un
# texte qu'on croit écrit par une machine est moins cru et moins partagé.
_HYPE_RE = re.compile("|".join([
    r"\bgame[- ]?chang\w+", r"\bmind[- ]?blow\w+", r"\bjaw[- ]?dropping\b", r"\bground[- ]?breaking\b",
    r"\bcutting[- ]edge\b", r"\brevolutioni[sz]\w+", r"\brevolutionary\b", r"\blet that sink in\b",
    r"\bbuckle up\b", r"\bthe future is here\b", r"\b(?:this )?changes everything\b",
    r"\bthis is (?:insane|huge)\b", r"\bdelv(?:e|es|ed|ing)\b", r"\btapestry\b", r"\ba testament to\b",
    r"\bunleash\w*", r"\bsupercharg\w+", r"\bin today'?s (?:fast[- ]paced )?world\b",
]), re.I)
_HYPE_CAPS_RE = re.compile(r"\b(?:BREAKING|JUST IN|RIP)\b")     # en capitales seulement


def find_hype(text: str) -> str | None:
    """Première formule toute faite trouvée (« game changer », « BREAKING »…)."""
    m = _HYPE_RE.search(text or "") or _HYPE_CAPS_RE.search(text or "")
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
    r"\bwagmi\b", r"\bngmi\b", r"\bhodl\w*", r"\brug ?pulls?\b", r"\b(?:got|get|gets|been|getting) rugged\b", r"\brugged (?:us|me|them|again)\b", r"\bopensea\b",
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
    r"\badults?[- ]only\b", r"\bexplicit content\b", r"\bdating (?:apps?|sites?|profiles?)\b", r"\btinder\b",
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


# Guerres et armement : jamais (actus écartées par sources, et posts refusés).
MILITARY_RE = re.compile(r"\b(?:" + "|".join([
    # Toujours militaire…
    r"militar(?:y|ies|ism|ized|ised)(?![- ]grade)", r"missiles?", r"warships?", r"warplanes?", r"fighter jets?",
    r"air force", r"pentagon", r"troops", r"soldiers?", r"battlefields?", r"munitions?", r"artiller\w+",
    r"warfare", r"warheads?", r"warfighters?", r"war ?zones?", r"torpedo(?:es)?", r"minesweep\w*",
    r"mine hunting", r"lockheed", r"raytheon", r"northrop", r"anduril", r"helsing", r"shield ai",
    r"service ?members?", r"veterans? affairs", r"national guard", r"nato", r"dod", r"darpa", r"space force",
    r"marine corps", r"marines", r"navy seals?", r"(?-i:Navy)(?! [Bb]lue)",
    r"navy (?:ships?|vessels?|sailors?|officers?|destroyers?|submarines?|drones?)", r"bombers?", r"kamikaze",
    r"drone strikes?", r"air ?strikes?", r"f-(?:16|22|35|47)", r"battle ?tanks?", r"rifles?", r"firearms?",
    r"machine guns?", r"gunfire", r"weapons", r"weaponry", r"weaponi[sz](?:e|ed|ing)", r"armed (?:robots?|drones?)",
    r"(?:department|ministry|secretary|minister) of defen[cs]e", r"(?:air|missile) defen[cs]e",
    r"defen[cs]e (?:department|ministry|secretary|minister|contractors?|firms?|tech|startups?|"
    r"compan(?:y|ies)|industry|giants?|contracts?|systems?|forces?|officials?|budgets?|spending)",
    r"hypersonic (?:missiles?|weapons?|glide)",
    # … et ce qui ne l'est que dans ce contexte (« robot army », « combat spam »,
    # « industry veterans », « secret weapon », « Naval Ravikant » restent permis).
    r"(?:u\.?s\.?|british|russian|chinese|ukrainian|israeli|iranian|indian|the) army(?! of)",
    r"army (?:bases?|soldiers?|units?|officers?|chiefs?|generals?|drones?|robots?)",
    r"combat (?:troops|aircraft|drones?|zones?|missions?|vehicles?|operations?|units?|soldiers?|robots?)",
    r"weapons? (?:systems?|makers?|manufacturers?|tests?|programs?|of war)",
    r"(?:nuclear|autonomous|lethal|chemical|biological) weapons?",
    r"naval (?:ships?|bases?|forces?|warfare|vessels?|drones?|officers?)",
]) + r")\b", re.I)


def military_term(text: str) -> str | None:
    m = MILITARY_RE.search(text or "")
    return m.group(0) if m else None


# Mots de HARD_BLOCK qui, sous notre plume, ne trahissent aucun sujet interdit (« Vote below »).
_OWN_TEXT_OK = {"vote"}


def blocked_topic(text: str) -> str | None:
    """Sujet hors ligne éditoriale dans un texte du compte (mort, guerre, armée, police,
    maladie, procès…), même quand l'actu choisie n'en parlait pas dans son titre : le
    texte de l'article, lu en entier, peut en parler au détour d'une phrase."""
    for term, rx in _HARD_BLOCK_RES:
        if term not in _OWN_TEXT_OK and rx.search(text or ""):
            return term
    return military_term(text)


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
    if re.search(r'"\s*(text|reply|post|reason|angle|image_prompt|visual_url|tweets|visual|hook|hook_options|'
                 r'source_ids|used_note_ids|poll_options|picks|claims)\s*"\s*:', t):
        return True
    # Clé sans guillemets : seulement tout au début (« text: "…" »). Au milieu d'une
    # phrase, « then reply: "shorter, please" » est un conseil tout à fait normal.
    if re.match(r"(text|image_prompt|visual_url|reply|tweets|card_type|card_title|card_lines)\s*:\s*\"", t):
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
# Le compte donne la nouvelle LUI-MÊME (novembre 2026)
# ---------------------------------------------------------------------------
# Comme un compte d'alertes info : le post dit le fait, il ne renvoie pas au site,
# au journal ou au blog qui l'a raconté (« TechCrunch reports », « according to The
# Verge »). Qui a fait l'annonce, qui l'a dit, qui a mené l'étude : ça, oui, c'est un
# fait. Le nom d'un média reste permis quand il est le SUJET de l'histoire (« The BBC
# will read the news with an AI voice ») : seule la citation est refusée.
# Les noms sont écrits sans « The » (« the Verge » et « The Verge » sont reconnus) ;
# ils sont lus avec leurs majuscules (« the verge of », « wired headphones » passent).
NEWS_OUTLETS = (
    "TechCrunch", "Verge", "Wired", "WIRED", "Ars Technica", "Engadget", "ZDNET", "ZDNet", "Guardian",
    "BBC", "BBC News", "MIT Technology Review", "Technology Review", "TechRadar", "Tom's Guide",
    "Tom’s Guide", "Tom's Hardware", "IEEE Spectrum", "Hacker News", "CNET", "Reuters", "Bloomberg",
    "Axios", "Mashable", "9to5Mac", "9to5Google", "MacRumors", "VentureBeat", "Business Insider", "Insider",
    "Forbes", "CNBC", "CNN", "Associated Press", "AP", "AFP", "New York Times", "NYT", "Washington Post",
    "Wall Street Journal", "WSJ", "Financial Times", "FT", "Fast Company", "NPR", "PCMag", "PC Magazine",
    "Lifehacker", "How-To Geek", "Android Authority", "Android Police", "Gizmodo", "Futurism", "New Scientist",
    "Popular Science", "PopSci", "Live Science", "Interesting Engineering", "Digital Trends", "Electrek",
    "Semafor", "Platformer", "Nikkei", "Sky News", "NBC News", "NBC", "CBS News", "ABC News", "Fox News",
    "Daily Mail", "Quartz", "Vox", "Rest of World", "404 Media", "New Atlas", "ScienceAlert",
    "Science Alert", "Dexerto", "UNILAD", "UNILAD Tech", "Singularity Hub", "ZME Science", "IGN",
    "GamesRadar", "UploadVR", "Humanoids Daily", "DroneDJ", "Science News", "Kotaku", "Polygon",
    "Eurogamer", "PC Gamer", "Road to VR", "InsideEVs", "TechSpot", "Neowin", "XDA", "Euronews",
    "GameSpot", "VGC", "Video Games Chronicle", "TechXplore", "Tech Xplore", "Robot Report",
    "Independent", "Information", "Times", "Atlantic", "Register", "Debrief", "Decoder",
)
# Ceux-là ne désignent un média qu'avec leur « The » (« the information », « times »…).
_OUTLETS_NEED_THE = frozenset({"Independent", "Information", "Times", "Atlantic", "Register", "Debrief",
                               "Decoder", "Verge", "Guardian"})
# (Pas « tests » : « AI can now pass AP tests » parle d'examens, pas d'un média.)
_OUTLET_VERBS = (r"reports?|reported|reporting|found|finds|revealed|reveals|learned|learns|tested|"
                 r"spotted|spots|confirmed|confirms|writes|wrote|notes|noted|says|said|broke(?: the)? news|"
                 r"got (?:an? )?(?:look|demo|hands-on)|obtained|saw|reviewed|reviews|asked|explains|explained|"
                 r"describes|described|estimates|estimated|first reported|has reported|has learned|has found")
_OUTLET_NOUNS = (r"report|reporting|reporter|reporters|story|article|review|reviewer|test|tests|testing|"
                 r"investigation|analysis|interview|editor|editors|journalist|journalists|team|video|podcast|"
                 r"newsletter|coverage|scoop")
_OUTLET_BEFORE = (r"according to|via|per|told|tells|telling|in an interview with|speaking to|speaking with|"
                  r"spoke to|spoke with|talking to|reported by|first reported by|as reported by|cited by|"
                  r"quoted by|confirmed to|(?:a|an|the) (?:new |recent )?(?:report|story|article|review|test|"
                  r"investigation|interview|piece|video|analysis) (?:by|from|in|on)")


def _outlet_name_re(name: str) -> str:
    the = r"(?i:the)\s+" if name in _OUTLETS_NEED_THE else r"(?:(?i:the)\s+)?"
    return the + re.escape(name).replace("\\ ", r"\s+") + r"(?![\w-])"


_OUTLET_ALT = "|".join(_outlet_name_re(n) for n in sorted(set(NEWS_OUTLETS), key=len, reverse=True))
_OUTLET_CITED_RES = (
    # « according to The Verge », « told Reuters », « a report from Bloomberg »
    re.compile(r"(?<![\w-])(?i:" + _OUTLET_BEFORE + r")\s+(?:" + _OUTLET_ALT + r")"),
    # « TechCrunch reports », « The Verge has learned », « Wired first reported »
    re.compile(r"(?<![\w-])(?:" + _OUTLET_ALT + r")\s+(?i:(?:has |have |first |just |also |now )?(?:"
               + _OUTLET_VERBS + r"))(?![\w-])"),
    # « The Verge's test », « Engadget's review »
    re.compile(r"(?<![\w-])(?:" + _OUTLET_ALT + r")(?:'s|’s)\s+(?i:(?:own |new |latest )?(?:"
               + _OUTLET_NOUNS + r"))(?![\w-])"),
    # Sans nom : « according to a report », « media reports », « reports say »
    re.compile(r"(?<![\w-])(?i:according to (?:a |an |the |one )?(?:new |recent |latest )?(?:media |news |press )?"
               r"(?:report|reports|article|story|stories|outlet|newspaper|website|site|blog)"
               r"|(?:media|press) reports?|news reports? (?:say|says|suggest|suggests|claim|claims)"
               r"|reports (?:say|suggest|claim|indicate))(?![\w-])"),
    # « 211 points on Hacker News » : la preuve vient d'un forum, pas du compte
    re.compile(r"(?<![\w-])(?i:on|from)\s+Hacker\s+News(?![\w-])"),
)


_OUTLET_NAME_RE = re.compile(r"(?<![\w-])(?:" + _OUTLET_ALT + r")")


def names_news_outlet(text: str) -> str | None:
    """Le nom d'un média n'importe où dans un texte court : l'auteur d'une citation à l'écran
    d'une vidéo (« — The Verge » est refusé, comme « The Verge reports » dans un post)."""
    m = _OUTLET_NAME_RE.search(text or "")
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else None


def news_outlet_cited(text: str) -> str | None:
    """Le passage où le post renvoie à un média (« TechCrunch reports », « according to a
    report »), ou None. Le compte donne la nouvelle lui-même."""
    for rx in _OUTLET_CITED_RES:
        m = rx.search(text or "")
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip()
    return None


# ---------------------------------------------------------------------------
# Verdict global
# ---------------------------------------------------------------------------
def publish_problem(text: str, *, sources: list[str] | None = None, max_len: int = MAX_WEIGHTED,
                    allow_numbers: int = 1, check_numbers: bool = True, plain: bool = False) -> str | None:
    """Raison pour laquelle `text` ne doit PAS être proposé, ou None si OK.
    `plain` : texte pour le grand public (posts, threads), sans jargon tech."""
    from .x_api import contains_url

    if not text or not text.strip():
        return "texte vide"
    if looks_like_json_leak(text):
        return "ressemble à du JSON"
    if contains_url(text):
        return "contient un lien ou un nom de domaine (0,20 $ le post via l'API au lieu de 0,015 $)"
    off = off_limits_term(text) or (blocked_topic(text) if plain else None)
    if off:
        return f"hors ligne éditoriale ({off})"
    jargon = find_jargon(text)
    if jargon:
        return f"jargon ({jargon})"
    if plain:
        tech = find_tech_jargon(text)
        if tech:
            return f"trop technique ({tech})"
        hype = find_hype(text)
        if hype:
            return f"formule toute faite ({hype})"
        outlet = news_outlet_cited(text)
        if outlet:
            return f"cite un média ({outlet})"
    if check_numbers and too_many_numbers(text, allow_numbers):
        return "trop de chiffres"
    if weighted_length(text) > max_len:
        return f"trop long ({weighted_length(text)} > {max_len})"
    if sources and is_too_similar(text, sources):
        return "trop proche d'une source"
    return None
