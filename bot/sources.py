"""Collecte de l'actu IA/tech pour nourrir Claude : flux RSS/Atom, Hacker News
(API Algolia, JSON) et posts des comptes officiels lus via l'API X OFFICIELLE.

Jamais de lecture de x.com par HTTP ou navigateur : les posts X passent
uniquement par `XClient.user_posts` (0,005 $ le post lu, lecture « optionnelle »
bloquée dès que le budget du jour est consommé). Un flux configuré sur x.com /
twitter.com est refusé.

`NewsHub.collect()` renvoie une liste de `NewsItem` :
  - filtrée : trop vieux, hors ligne éditoriale (crypto, politique, drames,
    foi…), sujets bloqués, bruit (giveaways…), mots-clés de la liste noire ;
  - dédoublonnée : même URL, ou titres qui se recouvrent à ≥ 80 % (on garde le
    meilleur score) ;
  - triée par score décroissant :
        score = fraîcheur (demi-vie 10 h) × poids de la source × (1 + log1p(engagement) / 3)

Économies :
  - résultat complet gardé 20 min en mémoire (mêmes arguments) ;
  - chaque flux gardé 20 min en mémoire ; s'il tombe, on réutilise sa dernière
    version valide (≤ 6 h) ;
  - posts X : `since_id` par compte (state.data["news_since"]), cache glissant de
    48 h (state.data["news_x_cache"]), chaque compte relu au plus toutes les
    2 h (state.data["news_x_fetched_at"]), ids des comptes partagés avec le radar
    (state.data["radar"]["user_ids"]).

`top_for(kind, items, n)` choisit ce qu'on montre à Claude selon le type de
contenu (frais pour l'actu et le réactif, mélange plus large pour le reste).
"""
from __future__ import annotations

import codecs
import html
import io
import json
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from . import filters
from .models import NewsItem, stable_id
from .utils import iso, now_local, parse_iso, tz
from .x_api import XError

log = logging.getLogger("xbot.sources")

HttpGet = Callable[[str], tuple[int, str]]

# --- Textes ---
SUMMARY_MAX = 600
TITLE_MAX = 200
X_TITLE_MAX = 120

# --- Score ---
HALF_LIFE_HOURS = 10.0
UNDATED_AGE_HOURS = 24.0        # un article sans date compte comme vieux d'un jour
X_ACCOUNT_WEIGHT = 2.0          # comptes officiels lus via l'API X
DEFAULT_WEIGHT = 1.0
DEFAULT_OFFICIAL_WEIGHT = 2.0   # flux officiel sans poids explicite
DUP_TITLE_THRESHOLD = 0.8
FRESH_HOURS = 24                # « frais » pour l'actu et le réactif

# --- Flux ---
MAX_ENTRIES_PER_FEED = 30
UNDATED_PER_FEED = 5            # sans date, on ne garde que les premiers (les plus récents en général)
FUTURE_TOLERANCE = timedelta(hours=1)
HTTP_TIMEOUT = 15
HTTP_MAX_SECONDS = 40           # durée totale max d'un téléchargement (serveur qui distille)
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_WORKERS = 6
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/129.0.0.0 Safari/537.36")

# --- Caches ---
RESULT_TTL = timedelta(minutes=20)
FEED_TTL = timedelta(minutes=20)
FEED_STALE_OK = timedelta(hours=6)
X_CACHE_HOURS = 48
X_REFRESH_MINUTES = 120
RADAR_SHARE_FRESH_MIN = 90     # compte lu par le radar il y a moins de 90 min : pas relu ici
X_UNRESOLVED_RETRY = timedelta(hours=6)
X_MAX_RESULTS = 5

# Erreurs X qui touchent tous les comptes : inutile d'insister pendant ce passage.
X_STOP_KINDS = {"budget", "rate_limited", "credits", "auth", "network", "server", "dry_run"}

# Hôtes qu'on ne lit JAMAIS en HTTP (règle X : pas de scraping, API officielle uniquement).
_X_HOSTS = ("x.com", "twitter.com", "t.co", "nitter.net")

# Hacker News : on ne garde que ce qui touche à l'IA / la tech grand public / les startups.
_HN_RELEVANT_RE = re.compile(r"\b(?:" + "|".join([
    r"A\.?I\.?", r"AGI", r"artificial intelligence", r"machine learning", r"deep learning",
    r"neural (?:net|network)s?", r"LLMs?", r"GPT[\w.-]*", r"ChatGPT", r"Claude", r"Anthropic", r"OpenAI",
    r"Gemini", r"Gemma", r"DeepMind", r"Llama", r"Mistral", r"DeepSeek", r"Qwen", r"Grok", r"xAI",
    r"Copilot", r"Perplexity", r"Hugging ?Face", r"Midjourney", r"Stable Diffusion", r"diffusion models?",
    r"Sora", r"chatbots?", r"transformers?", r"prompts?", r"prompt injection", r"inference", r"fine-?tun\w*",
    r"embeddings?", r"models?", r"agents?", r"agentic", r"MCP", r"vibe[- ]?cod\w*", r"Codex",
    r"Cursor", r"Windsurf", r"Replit", r"Lovable", r"Ollama", r"llama\.cpp", r"ElevenLabs", r"NotebookLM",
    r"Cohere", r"Groq", r"Cerebras",
    r"Nvidia", r"GPUs?", r"TPUs?", r"chips?", r"semiconductors?", r"TSMC", r"Intel", r"AMD", r"Qualcomm",
    r"Samsung", r"startups?", r"founders?", r"Y ?Combinator", r"YC", r"venture capital", r"VCs?", r"IPO",
    r"Apple", r"iPhone", r"iOS", r"macOS", r"Google", r"Microsoft", r"Meta", r"Amazon", r"AWS", r"Tesla",
    r"robots?", r"robotics", r"humanoids?", r"self-driving", r"Waymo", r"SaaS",
]) + r")(?!\w)", re.I)

_BOILERPLATE_RES = (
    # WordPress : « The post X appeared first on Y. »
    re.compile(r"\s*The post .{1,300}? appeared first on .{1,150}?$", re.I | re.S),
    re.compile(r"\s*(?:\[\s*(?:…|\.\.\.)\s*\]|…|\.\.\.)?\s*(?:continue reading|read more|keep reading|"
               r"read the full (?:story|article|post))\b[^.]{0,80}\.?\s*$", re.I),
)


# ---------------------------------------------------------------------------
# Texte
# ---------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    BLOCK = {"p", "br", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th",
             "blockquote", "pre", "section", "article", "figure", "figcaption", "table", "hr", "header",
             "footer"}
    SKIP = {"script", "style", "noscript", "iframe", "svg", "object", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in self.BLOCK:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


_ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9a-f]+|[a-z]{2,8});", re.I)


def strip_html(raw: str | None) -> str:
    """HTML → texte brut sur une ligne (entités décodées, espaces normalisés)."""
    if not raw:
        return ""
    text = str(raw)
    if "<" in text or "&" in text:
        parser = _TextExtractor()
        try:
            parser.feed(text)
            parser.close()
            text = "".join(parser.parts)
        except Exception:  # HTML très cassé : on retombe sur une regex
            text = html.unescape(re.sub(r"<[^>]+>", " ", text))
        # Certains flux échappent deux fois (« &amp;#8217; »)
        if _ENTITY_RE.search(text):
            text = html.unescape(text)
    text = text.replace(" ", " ").replace("​", "").replace("﻿", "")
    return re.sub(r"\s+", " ", text).strip()


def clean_summary(raw: str | None) -> str:
    text = strip_html(raw)
    for rx in _BOILERPLATE_RES:
        text = rx.sub("", text).strip()
    text = text.replace("[…]", "…").replace("[...]", "…")
    return text


def shorten(text: str, limit: int) -> str:
    """Coupe proprement à `limit` caractères (au dernier espace) avec « … »."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:max(1, limit - 1)]
    space = cut.rfind(" ")
    if space >= int(limit * 0.6):
        cut = cut[:space]
    return cut.rstrip(" ,;:-–—.") + "…"


def normalize_url(url: str | None) -> str:
    """Clé de dédoublonnage : https, sans www, sans utm_*, sans fragment, sans / final."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return url.strip().lower()
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in ("ref", "ref_src", "source", "fbclid",
                                                                       "gclid", "mc_cid", "mc_eid")]
    path = parts.path.rstrip("/")
    return urlunsplit(("https", host, path, urlencode(query), ""))


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _is_x_url(url: str) -> bool:
    host = _host(url)
    return any(host == h or host.endswith("." + h) for h in _X_HOSTS)


def _to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(tz())
    now = now_local()
    if dt > now + FUTURE_TOLERANCE:
        # Date dans le futur (fuseau mal déclaré, article programmé) : on la ramène à maintenant
        return now
    return min(dt, now)


def parse_datetime(value: Any) -> datetime | None:
    """ISO 8601 (« …Z » compris) ou RFC 822 → datetime aware (heure locale)."""
    if not value:
        return None
    s = str(value).strip()
    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(s)
        except (TypeError, ValueError, IndexError):
            dt = None
    return _to_local(dt)


def _entry_datetime(entry: Any) -> datetime | None:
    # feedparser donne des struct_time déjà ramenés en UTC
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(key)
        if t:
            try:
                return _to_local(datetime(*t[:6], tzinfo=timezone.utc))
            except (TypeError, ValueError):
                continue
    for key in ("published", "updated", "created"):
        dt = parse_datetime(entry.get(key))
        if dt:
            return dt
    return None


def _entry_link(entry: Any, base_url: str) -> str:
    link = entry.get("link") or ""
    if not link:
        for l in entry.get("links") or []:
            if l.get("href") and l.get("rel", "alternate") == "alternate":
                link = l["href"]
                break
    if not link:
        eid = str(entry.get("id") or "")
        if eid.startswith("http"):
            link = eid
    return urljoin(base_url, link.strip()) if link else ""


def _entry_summary(entry: Any) -> str:
    summary = clean_summary(entry.get("summary") or entry.get("description") or "")
    contents = entry.get("content") or []
    content = ""
    for c in contents:
        value = c.get("value") if isinstance(c, dict) else None
        if value:
            content = clean_summary(value)
            if content:
                break
    # Un résumé trop court (« Read more… ») : le contenu complet dit mieux de quoi ça parle.
    if content and len(summary) < 120 and len(content) > len(summary):
        summary = content
    return shorten(summary, SUMMARY_MAX)


# ---------------------------------------------------------------------------
# Ligne éditoriale
# ---------------------------------------------------------------------------
def _hard_block_regex(term: str) -> re.Pattern:
    """Même mot-clé que filters.HARD_BLOCK, mais qui doit COMMENCER un mot
    (« died » ne doit pas toucher « studied », ni « killed » « skilled », ni
    « rip » « trip »). Un espace final dans le mot-clé = mot entier."""
    core = term.strip()
    right = r"(?![a-z0-9])" if term.endswith(" ") else ""
    return re.compile(r"(?<![a-z0-9])" + re.escape(core) + right, re.I)


_HARD_BLOCK_RES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (str(t).strip(), _hard_block_regex(str(t))) for t in getattr(filters, "HARD_BLOCK", ()) if str(t).strip()
)


def hard_block_hit(text: str) -> str | None:
    """`filters.hard_blocked` sans ses faux positifs à l'intérieur d'un mot.
    Ne bloque jamais plus que `filters.hard_blocked` : on ne fait que confirmer
    ce qu'il a trouvé (les résumés d'actu font jusqu'à 600 caractères, « studied »,
    « skilled », « trip » ou « strip » y sont fréquents)."""
    if not filters.hard_blocked(text):
        return None
    if not _HARD_BLOCK_RES:
        return filters.hard_blocked(text)
    for term, rx in _HARD_BLOCK_RES:
        if rx.search(text or ""):
            return term
    return None


# ---------------------------------------------------------------------------
# Lecture des flux
# ---------------------------------------------------------------------------
def _declared_encoding(head: bytes) -> str | None:
    m = re.search(rb"""<\?xml[^>]*encoding=["']([A-Za-z0-9._-]+)["']""", head or b"")
    if not m:
        return None
    name = m.group(1).decode("ascii", "ignore")
    try:
        codecs.lookup(name)
    except LookupError:
        return None
    return name


def default_http_get(url: str) -> tuple[int, str]:
    """GET simple (timeout 15 s, User-Agent de navigateur, 5 Mo et 40 s maximum).
    Sans charset dans l'en-tête, on lit la déclaration XML, sinon UTF-8 (requests
    supposerait ISO-8859-1 et abîmerait les accents)."""
    import time

    import requests

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
                  "application/json;q=0.9, text/xml;q=0.8, */*;q=0.5",
    }
    with requests.get(url, timeout=HTTP_TIMEOUT, headers=headers, stream=True) as resp:
        status = resp.status_code
        chunks: list[bytes] = []
        size = 0
        deadline = time.monotonic() + HTTP_MAX_SECONDS
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            if time.monotonic() > deadline:
                log.warning("Actu : %s trop lent (> %d s), lecture tronquée", _host(url), HTTP_MAX_SECONDS)
                break
            size += len(chunk)
            if size > MAX_FEED_BYTES:
                log.warning("Actu : %s dépasse %d Mo, lecture tronquée", _host(url), MAX_FEED_BYTES // 2**20)
                break
            chunks.append(chunk)
        ctype = (resp.headers.get("Content-Type") or "").lower()
        header_encoding = resp.encoding if "charset=" in ctype else None
    raw = b"".join(chunks)
    encoding = header_encoding or _declared_encoding(raw[:300]) or "utf-8"
    try:
        text = raw.decode(encoding, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    return status, text


_XML_DECL_ENCODING_RE = re.compile(r"""^(\s*<\?xml[^>]*?\bencoding\s*=\s*["'])[^"']*(["'])""", re.I)


def _feed_stream(text: str) -> io.BytesIO:
    """Texte déjà décodé → flux d'octets UTF-8 pour feedparser.

    - On réécrit la déclaration XML en utf-8 : sinon feedparser re-décode
      selon l'ancienne (« ISO-8859-1 ») et abîme les accents.
    - On passe un flux, jamais une chaîne : feedparser irait télécharger une
      chaîne qui ressemble à une URL, ou ouvrir un fichier local."""
    t = (text or "").lstrip("﻿")
    t = _XML_DECL_ENCODING_RE.sub(r"\g<1>utf-8\g<2>", t, count=1)
    return io.BytesIO(t.encode("utf-8", errors="replace"))


def _feed_name(feed: dict) -> str:
    return str(feed.get("name") or _host(str(feed.get("url") or "")) or "flux")


def _is_hn(feed: dict) -> bool:
    return str(feed.get("type") or "").lower() == "hn" or "hn.algolia.com" in str(feed.get("url") or "")


def _feed_weight(feed: dict) -> float:
    default = DEFAULT_OFFICIAL_WEIGHT if feed.get("official") else DEFAULT_WEIGHT
    try:
        w = float(feed.get("weight", default))
    except (TypeError, ValueError):
        w = default
    if not math.isfinite(w):
        w = default
    return max(0.1, w)


def parse_rss(text: str, feed: dict) -> list[NewsItem]:
    """RSS 2.0 / Atom → NewsItem (sans score). Lève une exception si illisible."""
    import feedparser  # dépendance déclarée ; import local pour ne jamais casser l'import du module

    parsed = feedparser.parse(_feed_stream(text))
    entries = list(parsed.get("entries") or [])
    if not entries and parsed.get("bozo"):
        raise ValueError(f"flux illisible ({parsed.get('bozo_exception')})")
    name = _feed_name(feed)
    base = str(feed.get("url") or "")
    official = bool(feed.get("official"))
    items: list[NewsItem] = []
    undated = 0
    for entry in entries[:MAX_ENTRIES_PER_FEED]:
        title = shorten(strip_html(entry.get("title")), TITLE_MAX)
        url = _entry_link(entry, base)
        if not title or not url or not url.lower().startswith(("http://", "https://")):
            continue
        published = _entry_datetime(entry)
        if published is None:
            undated += 1
            if undated > UNDATED_PER_FEED:
                continue
        items.append(NewsItem(
            id=stable_id(normalize_url(url) or url),
            title=title,
            summary=_entry_summary(entry),
            url=url,
            source=name,
            published=published,
            official=official,
            kind="rss",
        ))
    return items


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def parse_hn(text: str, feed: dict) -> list[NewsItem]:
    """Réponse JSON de l'API Algolia de Hacker News → NewsItem IA/tech seulement."""
    data = json.loads(text)
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list):
        raise ValueError("réponse Hacker News sans « hits »")
    name = _feed_name(feed)
    items: list[NewsItem] = []
    for h in hits:
        if not isinstance(h, dict):
            continue
        title = shorten(strip_html(h.get("title") or h.get("story_title") or ""), TITLE_MAX)
        oid = str(h.get("objectID") or h.get("story_id") or "").strip()
        url = str(h.get("url") or h.get("story_url") or "").strip()
        if not url and oid:
            url = f"https://news.ycombinator.com/item?id={oid}"
        if not title or not url:
            continue
        # Le nom de domaine compte (openai.com, nvidia.com…), mais pas son
        # extension : « .ai » n'en fait pas un sujet IA (typesafe.ai…).
        host = _host(url)
        host_core = host.rsplit(".", 1)[0] if "." in host else host
        if not _HN_RELEVANT_RE.search(f"{title} {host_core}"):
            continue
        points = _int(h.get("points"))
        comments = _int(h.get("num_comments"))
        published = None
        ts = h.get("created_at_i")
        if ts:
            try:
                published = _to_local(datetime.fromtimestamp(int(ts), tz=timezone.utc))
            except (TypeError, ValueError, OverflowError, OSError):
                published = None
        if published is None:
            published = parse_datetime(h.get("created_at"))
        story = clean_summary(h.get("story_text") or "")
        summary = story or f"Trending on Hacker News ({points} points, {comments} comments)."
        items.append(NewsItem(
            id=stable_id(normalize_url(url) or url),
            title=title,
            summary=shorten(summary, SUMMARY_MAX),
            url=url,
            source=name,
            published=published,
            official=bool(feed.get("official")),
            kind="hn",
            engagement=points,
        ))
    return items


# ---------------------------------------------------------------------------
# Score, dédoublonnage
# ---------------------------------------------------------------------------
def score_item(item: NewsItem, weight: float, now: datetime | None = None) -> float:
    now = now or now_local()
    if item.published is not None:
        age_h = max(0.0, (now - item.published).total_seconds() / 3600.0)
    else:
        age_h = UNDATED_AGE_HOURS
    freshness = 0.5 ** (age_h / HALF_LIFE_HOURS)
    engagement = 1.0 + math.log1p(max(0, int(item.engagement or 0))) / 3.0
    return round(freshness * max(0.1, float(weight)) * engagement, 6)


def _words(text: str) -> set[str]:
    # Même découpage que filters.is_too_similar
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def titles_similar(a: str, b: str) -> bool:
    """Recouvrement de mots ≥ 80 % (dans un sens ou dans l'autre)."""
    thr = DUP_TITLE_THRESHOLD - 1e-9  # is_too_similar compare avec « > »
    return filters.is_too_similar(a, [b], thr) or filters.is_too_similar(b, [a], thr)


def dedupe(items: list[NewsItem]) -> list[NewsItem]:
    """Garde le meilleur score par URL / par titre quasi identique (tri par score)."""
    ordered = sorted(items, key=lambda i: i.score, reverse=True)
    out: list[tuple[NewsItem, set[str]]] = []
    seen: set[str] = set()
    for it in ordered:
        key = normalize_url(it.url)
        if key and key in seen:
            continue
        words = _words(it.title)
        # Pré-filtre : sous 3 mots communs, is_too_similar ne peut pas conclure
        # au doublon (il exige ≥ 3 mots et ≥ 80 % de recouvrement).
        if any(len(words & w) >= 3 and titles_similar(it.title, k.title) for k, w in out):
            continue
        out.append((it, words))
        if key:
            seen.add(key)
    return [it for it, _ in out]


# ---------------------------------------------------------------------------
# Sélection pour un type de post
# ---------------------------------------------------------------------------
def _diverse(items: list[NewsItem], n: int, per_source: int) -> list[NewsItem]:
    """Meilleurs scores en limitant chaque source à `per_source` (puis complète
    si ça ne suffit pas). Résultat trié par score décroissant."""
    picked: list[NewsItem] = []
    counts: dict[str, int] = {}
    for it in items:
        if len(picked) >= n:
            break
        src = (it.source or "").lower()
        if counts.get(src, 0) >= per_source:
            continue
        counts[src] = counts.get(src, 0) + 1
        picked.append(it)
    for it in items:
        if len(picked) >= n:
            break
        if not any(it is p for p in picked):
            picked.append(it)
    picked.sort(key=lambda i: i.score, reverse=True)
    return picked


def top_for(kind: str, items: list[NewsItem], n: int = 15) -> list[NewsItem]:
    """Sélection d'actus pour un type de contenu.

    - news : d'abord le frais (< 24 h) au meilleur score, complété par le reste ;
    - reactive : uniquement le frais (< 24 h) — on ne réagit pas à du réchauffé ;
    - tip / gm / thread (et autres) : un mélange plus large, 2 par source d'abord,
      avec au moins un tiers d'annonces officielles quand il y en a (ce sont
      elles qui donnent de vrais « comment utiliser l'outil X »).
    """
    if n <= 0 or not items:
        return []
    ordered = sorted(items, key=lambda i: i.score, reverse=True)
    limit = now_local() - timedelta(hours=FRESH_HOURS)
    fresh = [i for i in ordered if i.published is not None and i.published >= limit]
    if kind == "reactive":
        return _diverse(fresh, n, per_source=3)
    if kind == "news":
        picked = _diverse(fresh, n, per_source=3)
        if len(picked) < n:
            rest = [i for i in ordered if not any(i is p for p in picked)]
            picked += _diverse(rest, n - len(picked), per_source=3)
        return picked[:n]
    picked = _diverse(ordered, n, per_source=2)
    want_official = n // 3
    have = sum(1 for p in picked if p.official)
    if have < want_official:
        extra = [i for i in ordered if i.official and not any(i is p for p in picked)]
        for off in extra[:want_official - have]:
            # remplace l'élément non officiel le moins bien noté
            for idx in range(len(picked) - 1, -1, -1):
                if not picked[idx].official:
                    picked[idx] = off
                    break
            else:
                break
        picked.sort(key=lambda i: i.score, reverse=True)
    return picked


# ---------------------------------------------------------------------------
# NewsHub
# ---------------------------------------------------------------------------
class NewsHub:
    def __init__(self, settings, state, x=None, http_get: HttpGet | None = None) -> None:
        self.settings = settings
        self.state = state
        self.x = x
        self.http_get: HttpGet = http_get or default_http_get
        self._result_cache: dict[tuple[float, bool], tuple[datetime, list[NewsItem]]] = {}
        # url -> {"at": datetime, "items": [NewsItem]} (dernière lecture réussie)
        self._feed_cache: dict[str, dict] = {}
        self._unresolved: dict[str, datetime] = {}
        self.last_stats: dict[str, Any] = {}

    # Accès pratique : NewsHub.top_for(kind, items, n)
    top_for = staticmethod(top_for)

    # ------------------------------------------------------------------
    def collect(self, max_age_hours: float = 36, use_x: bool = True) -> list[NewsItem]:
        """Actu triée par score décroissant. Ne lève jamais d'exception.
        `use_x=False` : n'interroge pas l'API X, mais réutilise les posts
        officiels déjà en cache (déjà payés)."""
        now = now_local()
        try:
            key = (float(max_age_hours), bool(use_x))
        except (TypeError, ValueError):
            key = (36.0, bool(use_x))
        cached = self._result_cache.get(key)
        if cached and timedelta(0) <= now - cached[0] < RESULT_TTL:
            return [replace(i) for i in cached[1]]

        stats: dict[str, Any] = {"rss": 0, "hn": 0, "x": 0, "old": 0, "blocked": 0, "dupes": 0,
                                 "failed_feeds": []}
        raw: list[tuple[NewsItem, float]] = []
        try:
            raw += self._collect_feeds(now, stats)
        except Exception as e:  # ne doit jamais arriver, mais la boucle ne doit pas tomber
            log.exception("Actu : erreur inattendue sur les flux : %s", e)
        try:
            raw += self._collect_x(now, use_x)
        except Exception as e:
            log.exception("Actu : erreur inattendue sur les comptes X : %s", e)

        for it, _ in raw:
            if it.kind in stats:
                stats[it.kind] += 1
        try:
            items = self._finalize(raw, now, key[0], stats)
        except Exception as e:
            log.exception("Actu : erreur inattendue au tri : %s", e)
            items = []
        stats["kept"] = len(items)
        self.last_stats = stats
        log.info("Actu : %d éléments retenus (RSS %d, HN %d, X %d ; %d trop vieux, %d hors ligne, "
                 "%d doublons%s)", len(items), stats["rss"], stats["hn"], stats["x"], stats["old"],
                 stats["blocked"], stats["dupes"],
                 f" ; flux en échec : {', '.join(stats['failed_feeds'])}" if stats["failed_feeds"] else "")
        self._result_cache[key] = (now, items)
        return [replace(i) for i in items]

    def clear_cache(self) -> None:
        """Oublie les caches mémoire (pas le cache des posts X, déjà payés)."""
        self._result_cache.clear()
        self._feed_cache.clear()

    # ------------------------------------------------------------------
    def _finalize(self, raw: list[tuple[NewsItem, float]], now: datetime, max_age_hours: float,
                  stats: dict) -> list[NewsItem]:
        cutoff = now - timedelta(hours=max_age_hours)
        kept: list[NewsItem] = []
        for it, weight in raw:
            if not (it.title or "").strip():
                continue
            if it.published is not None and it.published < cutoff:
                stats["old"] += 1
                continue
            reason = self._blocked_reason(it)
            if reason:
                stats["blocked"] += 1
                log.debug("Actu écartée (%s) : %s — %s", reason, it.source, it.title[:80])
                continue
            it.score = score_item(it, weight, now)
            kept.append(it)
        out = dedupe(kept)
        stats["dupes"] = len(kept) - len(out)
        return out

    def _blocked_reason(self, it: NewsItem) -> str | None:
        text = f"{it.title}\n{it.summary}"
        term = filters.off_limits_term(text)
        if term:
            return f"hors ligne : {term}"
        hb = hard_block_hit(text)
        if hb:
            return f"sujet bloqué : {hb}"
        if filters.is_noise(text):
            return "bruit"
        low = text.lower()
        for kw in self.settings.blacklist_keywords or []:
            if kw and str(kw).lower() in low:
                return f"liste noire : {kw}"
        return None

    # ------------------------------------------------------------------
    # Flux RSS / Atom / Hacker News
    # ------------------------------------------------------------------
    def _collect_feeds(self, now: datetime, stats: dict) -> list[tuple[NewsItem, float]]:
        feeds = [f for f in (self.settings.news_feeds or []) if isinstance(f, dict) and f.get("url")]
        if not feeds:
            return []
        ready: list[tuple[dict, list[NewsItem]]] = []
        todo: list[dict] = []
        for f in feeds:
            c = self._feed_cache.get(str(f["url"]))
            if c and timedelta(0) <= now - c["at"] < FEED_TTL:
                ready.append((f, c["items"]))
            else:
                todo.append(f)
        if todo:
            if len(todo) == 1:
                fetched = [self._fetch_feed(todo[0])]
            else:
                with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(todo)),
                                        thread_name_prefix="news") as pool:
                    fetched = list(pool.map(self._fetch_feed, todo))
            for f, items in zip(todo, fetched, strict=True):
                url = str(f["url"])
                if items is None:
                    stats["failed_feeds"].append(_feed_name(f))
                    c = self._feed_cache.get(url)
                    if c and timedelta(0) <= now - c["at"] < FEED_STALE_OK:
                        log.info("Actu : %s indisponible, je réutilise sa version d'il y a %d min",
                                 _feed_name(f), int((now - c["at"]).total_seconds() // 60))
                        ready.append((f, c["items"]))
                    continue
                self._feed_cache[url] = {"at": now, "items": items}
                ready.append((f, items))
        out: list[tuple[NewsItem, float]] = []
        for f, items in ready:
            w = _feed_weight(f)
            out.extend((replace(it), w) for it in items)
        return out

    def _fetch_feed(self, feed: dict) -> list[NewsItem] | None:
        """Lit un flux. None = échec (déjà loggé), jamais d'exception."""
        url = str(feed.get("url") or "")
        name = _feed_name(feed)
        if _is_x_url(url):
            log.error("Actu : flux %s ignoré — x.com ne se lit QUE via l'API officielle", name)
            return None
        try:
            status, text = self.http_get(url)
        except Exception as e:
            log.warning("Actu : flux %s injoignable (%s)", name, str(e) or type(e).__name__)
            return None
        try:
            code = int(status or 0)
        except (TypeError, ValueError):
            code = 0
        if isinstance(text, (bytes, bytearray)):
            text = bytes(text).decode("utf-8", errors="replace")
        if not (200 <= code < 300) or not text or not str(text).strip():
            log.warning("Actu : flux %s → HTTP %s%s", name, status, "" if text else " (vide)")
            return None
        try:
            items = parse_hn(text, feed) if _is_hn(feed) else parse_rss(text, feed)
        except Exception as e:
            log.warning("Actu : flux %s illisible (%s)", name, str(e)[:200] or type(e).__name__)
            return None
        log.info("Actu : %s → %d articles", name, len(items))
        return items

    # ------------------------------------------------------------------
    # Comptes officiels sur X (API uniquement)
    # ------------------------------------------------------------------
    def _x_handles(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for h in self.settings.x_news_accounts or []:
            h = str(h or "").strip().lstrip("@")
            if h and h.lower() not in seen:
                seen.add(h.lower())
                out.append(h)
        return out

    def _collect_x(self, now: datetime, use_x: bool) -> list[tuple[NewsItem, float]]:
        if not self.settings.x_news_accounts_enabled:
            return []
        handles = self._x_handles()
        if not handles:
            return []
        # Les comptes que le radar lit déjà ne sont pas relus ici (chaque post lu est
        # payé) : le radar dépose ses lectures dans le même cache (share_radar_posts).
        radar = {h.lower() for h in (self.settings.radar_accounts or [])} \
            if getattr(self.settings, "radar_enabled", False) else set()
        _, _, fetched_at = self._x_store()

        def radar_fresh(h: str) -> bool:
            # Le radar s'arrête parfois pour la journée (plafond, compte déjà servi) :
            # au-delà de 90 min sans lecture, l'actu relit le compte elle-même.
            last = parse_iso(fetched_at.get(h.lower()))
            return last is not None and timedelta(0) <= now - last < timedelta(minutes=RADAR_SHARE_FRESH_MIN)

        to_read = [h for h in handles if not (h.lower() in radar and radar_fresh(h))]
        if to_read and use_x and self.x is not None and getattr(self.x, "configured", False):
            try:
                self._refresh_x(to_read, now)
            except Exception as e:
                log.warning("Actu X : lecture interrompue (%s), je garde le cache", e)
        else:
            _, cache, _ = self._x_store()
            if self._prune_x_cache(cache, now):
                self.state.flush()
        return self._x_items_from_cache(handles, now)

    def _x_store(self) -> tuple[dict, dict, dict]:
        data = self.state.data
        for key in ("news_since", "news_x_cache", "news_x_fetched_at"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        return data["news_since"], data["news_x_cache"], data["news_x_fetched_at"]

    def _resolve_ids(self, handles: list[str], now: datetime) -> dict[str, str]:
        """{handle_minuscule: id}, via le cache partagé avec le radar ; ne
        résout (0,010 $ par compte) que les comptes manquants."""
        radar = self.state.data.get("radar")
        if not isinstance(radar, dict):
            radar = self.state.data["radar"] = {"user_ids": {}, "since_ids": {}, "rotation": 0}
        ids = radar.get("user_ids")
        if not isinstance(ids, dict):
            ids = radar["user_ids"] = {}
        known = {str(k).lower(): str(v) for k, v in ids.items() if v}
        missing = []
        for h in handles:
            low = h.lower()
            if low in known:
                continue
            retry = self._unresolved.get(low)
            if retry and now < retry:
                continue
            missing.append(h)
        if missing:
            got: dict[str, str] = {}
            try:
                got = {str(k).lower(): str(v) for k, v in (self.x.user_ids(missing) or {}).items() if v}
            except XError as e:
                log.warning("Actu X : ids des comptes non résolus (%s : %s)", e.kind, e)
            except Exception as e:
                log.warning("Actu X : ids des comptes non résolus (%s)", e)
            for low, uid in got.items():
                ids[low] = uid
                known[low] = uid
            for h in missing:
                if h.lower() not in got:
                    self._unresolved[h.lower()] = now + X_UNRESOLVED_RETRY
            if got:
                self.state.flush()
                log.info("Actu X : %d compte(s) résolu(s)", len(got))
        return {h.lower(): known[h.lower()] for h in handles if h.lower() in known}

    def _refresh_x(self, handles: list[str], now: datetime) -> None:
        since, cache, fetched_at = self._x_store()
        ids = self._resolve_ids(handles, now)

        def last_fetch(h: str) -> datetime | None:
            return parse_iso(fetched_at.get(h.lower()))

        due = []
        for h in handles:
            if h.lower() not in ids:
                continue
            last = last_fetch(h)
            if last is None or last > now or (now - last) >= timedelta(minutes=X_REFRESH_MINUTES):
                due.append(h)
        # Les comptes lus il y a le plus longtemps d'abord : si le budget coupe,
        # ce ne sont pas toujours les mêmes qui passent à la trappe.
        due.sort(key=lambda h: last_fetch(h) or datetime.min.replace(tzinfo=timezone.utc))

        read_accounts = 0
        new_posts = 0
        for h in due:
            low = h.lower()
            try:
                tweets, newest = self.x.user_posts(ids[low], since_id=since.get(low),
                                                   max_results=X_MAX_RESULTS, optional=True)
            except XError as e:
                if e.kind in X_STOP_KINDS:
                    log.info("Actu X : arrêt des lectures (%s : %s), je garde le cache", e.kind, e)
                    break
                log.warning("Actu X : @%s illisible (%s : %s)", h, e.kind, e)
                fetched_at[low] = iso(now)
                continue
            except Exception as e:
                log.warning("Actu X : @%s illisible (%s)", h, e)
                continue
            read_accounts += 1
            fetched_at[low] = iso(now)
            if newest:
                since[low] = str(newest)
            for tw in tweets or []:
                entry = self._cache_entry(h, tw, now)
                if entry is None:
                    continue
                tid = str(tw.get("id"))
                if tid not in cache:
                    new_posts += 1
                cache[tid] = entry
        removed = self._prune_x_cache(cache, now)
        if due or removed:
            self.state.flush()
        if due:
            log.info("Actu X : %d compte(s) lu(s), %d nouveau(x) post(s), %d en cache",
                     read_accounts, new_posts, len(cache))

    @staticmethod
    def _cache_entry(handle: str, tw: dict, now: datetime) -> dict | None:
        if not isinstance(tw, dict) or not tw.get("id"):
            return None
        text = html.unescape(str(tw.get("text") or "")).strip()
        if not text or text.startswith("RT @"):
            return None
        created = parse_datetime(tw.get("created_at")) or now
        likes = _int((tw.get("public_metrics") or {}).get("like_count"))
        return {"handle": handle, "text": text[:4000], "created_at": iso(created), "likes": likes,
                "fetched_at": iso(now)}

    @staticmethod
    def _prune_x_cache(cache: dict, now: datetime) -> int:
        limit = now - timedelta(hours=X_CACHE_HOURS)
        removed = 0
        for tid in list(cache):
            e = cache[tid]
            created = parse_iso(e.get("created_at")) if isinstance(e, dict) else None
            if created is None or created < limit:
                del cache[tid]
                removed += 1
        return removed

    def _x_items_from_cache(self, handles: list[str], now: datetime) -> list[tuple[NewsItem, float]]:
        cache = self.state.data.get("news_x_cache")
        if not isinstance(cache, dict) or not cache:
            return []
        allowed = {h.lower() for h in handles}
        limit = now - timedelta(hours=X_CACHE_HOURS)
        out: list[tuple[NewsItem, float]] = []
        for tid, e in cache.items():
            if not isinstance(e, dict):
                continue
            handle = str(e.get("handle") or "").lstrip("@")
            if handle.lower() not in allowed:
                continue
            created = parse_iso(e.get("created_at"))
            if created is None or created < limit:
                continue
            item = x_post_item(handle, str(tid), str(e.get("text") or ""), created, _int(e.get("likes")))
            if item is not None:
                out.append((item, X_ACCOUNT_WEIGHT))
        return out


_TCO_RE = re.compile(r"https?://t\.co/\S+", re.I)


def x_post_item(handle: str, tweet_id: str, text: str, created: datetime | None, likes: int = 0) -> NewsItem | None:
    """Post officiel X → NewsItem (titre = 1re ligne ≤ 120 car., résumé = texte complet ≤ 600)."""
    body = _TCO_RE.sub("", html.unescape(text or ""))
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in body.splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return None
    title = shorten(lines[0], X_TITLE_MAX)
    summary = shorten(" ".join(lines), SUMMARY_MAX)
    return NewsItem(
        id=stable_id("x", tweet_id),
        title=title,
        summary=summary,
        url=f"https://x.com/{handle}/status/{tweet_id}",
        source=f"@{handle}",
        published=created,
        official=True,
        kind="x",
        engagement=_int(likes),
    )


def share_radar_posts(state, handle: str, tweets: list[dict], newest: str | None) -> None:
    """Le radar vient de lire (et de payer) les posts d'un compte d'actu : on les
    range aussi dans le cache d'actu, pour ne jamais acheter deux fois le même post."""
    data = state.data
    for key in ("news_since", "news_x_cache", "news_x_fetched_at"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    now = now_local()
    low = handle.lower()
    for tw in tweets or []:
        entry = NewsHub._cache_entry(handle, tw, now)
        if entry is not None:
            data["news_x_cache"][str(tw.get("id"))] = entry
    if newest:
        data["news_since"][low] = str(newest)
    data["news_x_fetched_at"][low] = iso(now)
