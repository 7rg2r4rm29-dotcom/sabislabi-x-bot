"""Tendances du jour, gratuites, pour la rédaction en chef.

Ce que le grand public cherche et lit (recherches qui montent sur Google aux États-Unis et
au Royaume-Uni, pages Wikipédia en anglais les plus lues la veille), ce dont parle le
public tech (première page de Hacker News), et les annonces officielles les plus aimées
sur X : des posts DÉJÀ lus pour l'actu (`state.data["news_x_cache"]`), donc aucune lecture
X de plus et rien de plus à payer.

Relevé au plus une fois par heure (`TRENDS_EVERY_MIN`) et rangé dans
`state.data["trends"]` ; une source en panne garde son dernier relevé pendant 24 h, puis
disparaît du bloc. Sans aucune source, le bloc est vide et la rédaction fait comme avant.
Ne lève jamais d'exception.

Les tendances ne sont JAMAIS une source de faits : elles aident seulement la rédaction en
chef à choisir, parmi les actus du jour, celle qui parlera le plus aux gens.
"""
from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import timedelta, timezone
from typing import Callable

from .filters import blocked_topic, off_limits_term
from .utils import iso, now_local, parse_iso

log = logging.getLogger("xbot.trends")

TRENDS_EVERY_MIN = 60            # un relevé par heure au plus
SOURCE_MAX_AGE_H = 24            # au-delà, une source en panne disparaît du bloc
GOOGLE_GEOS = (("US", "the US"), ("GB", "the UK"))
GOOGLE_URL = "https://trends.google.com/trending/rss?geo={geo}"
GOOGLE_NS = {"ht": "https://trends.google.com/trending/rss"}
WIKI_URL = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia.org/all-access/"
            "{day:%Y}/{day:%m}/{day:%d}")
HN_URL = "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30"
RSS_ACCEPT = "application/rss+xml, application/xml;q=0.9, */*;q=0.5"
JSON_ACCEPT = "application/json"

MAX_GOOGLE = 6
MAX_WIKI = 12
MAX_HN = 6
MAX_X = 5
X_LIKED_HOURS = 48
KEEP_PER_SOURCE = 30             # ce qui est gardé dans l'état (le bloc en montre moins)

# Pages de Wikipédia qui ne sont pas des articles (accueil, recherche, images du jour…).
WIKI_SKIP_PREFIXES = ("Special:", "Wikipedia:", "File:", "Portal:", "Help:", "Category:", "Template:",
                      "User:", "Talk:", "Draft:", "Module:", "MediaWiki:", "Book:", "TimedText:")
WIKI_SKIP_TITLES = {"Main_Page", "Main Page", "-", "Undefined"}
# Les filtres de la ligne éditoriale ne visent pas les pages pour adultes, très lues sur Wikipédia.
_ADULT_RE = re.compile(r"(?:^|[^a-z0-9])(?:xxx|porn\w*|sex\w*|onlyfans|nude\w*|nudity|xhamster|xvideos|"
                       r"hentai|erotic\w*|playboy|stripper\w*|escort\w*)(?![a-z0-9])", re.I)
# Justice, punitions, violences : hors ligne éditoriale, et souvent absents des filtres des posts
# (écrits pour des actus tech). Une tendance écartée à tort ne coûte rien.
_HARSH_RE = re.compile(r"(?<![a-z0-9])(?:flog\w*|lash(?:es|ed|ing)?|punish\w*|authorities|jail\w*|prison\w*|"
                       r"arrest\w*|sentenc\w*|convict\w*|assault\w*|abus\w*|stabb?\w*|shooting|shot dead|"
                       r"murder\w*|kill\w*|hostage\w*|riot\w*|protest\w*|crash\w*|injur\w*|lawsuit\w*|"
                       r"trial|charged)(?![a-z0-9])", re.I)
_URL_RE = re.compile(r"https?://\S+")

HttpGet = Callable[[str, str], tuple[int, str]]


def _default_get(url: str, accept: str) -> tuple[int, str]:
    # Cherché à chaque appel : les tests remplacent sources.default_http_get (aucun réseau).
    from . import sources
    return sources.default_http_get(url, accept=accept)


def _clean(text: str) -> str:
    return " ".join(str(text or "").split())


def acceptable(text: str) -> bool:
    """Une tendance montrable à la rédaction : jamais un sujet hors ligne éditoriale
    (politique, crypto, morts, guerre, police, religion…) ni une page pour adultes."""
    t = _clean(text)
    return (bool(t) and not off_limits_term(t) and not blocked_topic(t) and not _ADULT_RE.search(t)
            and not _HARSH_RE.search(t))


def parse_traffic(raw: str) -> int:
    """« 200000+ » → 200000 ; « 2K+ » → 2000 ; « 1M+ » → 1000000 ; illisible → 0."""
    s = (raw or "").strip().upper().replace(",", "").replace(" ", "").rstrip("+")
    mult = 1
    if s.endswith("K"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    try:
        return max(0, int(float(s) * mult))
    except ValueError:
        return 0


def parse_google_trends(xml_text: str) -> list[dict]:
    """Flux RSS « Daily Search Trends » de Google : [{term, traffic, news}], le plus
    cherché d'abord. `news` : le titre du premier article lié (pour comprendre le mot)."""
    try:
        root = ET.fromstring(xml_text or "")
    except ET.ParseError:
        return []
    out: list[dict] = []
    for it in root.iter("item"):
        term = _clean(it.findtext("title") or "")
        if not term:
            continue
        news = [_clean(n.findtext("ht:news_item_title", default="", namespaces=GOOGLE_NS))
                for n in it.findall("ht:news_item", GOOGLE_NS)]
        out.append({"term": term,
                    "traffic": parse_traffic(it.findtext("ht:approx_traffic", default="", namespaces=GOOGLE_NS)),
                    "news": next((n for n in news if n), "")})
    out.sort(key=lambda d: -d["traffic"])
    return out


def parse_wiki_top(json_text: str) -> list[dict]:
    """Pages de Wikipédia les plus lues sur une journée : [{title, views}], dans l'ordre
    de l'API (la plus lue d'abord), sans les pages qui ne sont pas des articles."""
    try:
        arts = json.loads(json_text or "")["items"][0]["articles"]
    except (ValueError, KeyError, IndexError, TypeError):
        return []
    out: list[dict] = []
    for a in arts if isinstance(arts, list) else []:
        if not isinstance(a, dict):
            continue
        raw = str(a.get("article") or "")
        if not raw or raw in WIKI_SKIP_TITLES or raw.startswith(WIKI_SKIP_PREFIXES):
            continue
        try:
            views = int(a.get("views") or 0)
        except (TypeError, ValueError):
            views = 0
        out.append({"title": raw.replace("_", " "), "views": views})
    return out


def parse_hn(json_text: str) -> list[dict]:
    """Première page de Hacker News (API Algolia) : [{title, points}], la plus votée d'abord."""
    try:
        hits = json.loads(json_text or "").get("hits") or []
    except (ValueError, AttributeError):
        return []
    out: list[dict] = []
    for h in hits if isinstance(hits, list) else []:
        if not isinstance(h, dict):
            continue
        title = _clean(h.get("title") or "")
        if not title:
            continue
        try:
            points = int(h.get("points") or 0)
        except (TypeError, ValueError):
            points = 0
        out.append({"title": title, "points": points})
    out.sort(key=lambda d: -d["points"])
    return out


def x_most_liked(cache, now, hours: int = X_LIKED_HOURS, n: int = MAX_X) -> list[dict]:
    """Les posts officiels les plus aimés des `hours` dernières heures, pris dans le cache
    de l'actu X (déjà payé) : [{handle, text, likes}]."""
    rows: list[dict] = []
    limit = now - timedelta(hours=hours)
    for e in (cache or {}).values() if isinstance(cache, dict) else []:
        if not isinstance(e, dict):
            continue
        created = parse_iso(e.get("created_at"))
        if created is None or created < limit:
            continue
        text = _clean(_URL_RE.sub("", str(e.get("text") or "")))
        try:
            likes = int(e.get("likes") or 0)
        except (TypeError, ValueError):
            likes = 0
        if not text or likes <= 0 or not acceptable(text):
            continue
        rows.append({"handle": str(e.get("handle") or "").lstrip("@"), "text": text, "likes": likes})
    rows.sort(key=lambda r: -r["likes"])
    return rows[:n]


def _short(text: str, n: int) -> str:
    t = _clean(text)
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _num(n: int) -> str:
    return f"{int(n):,}"


class TrendWatch:
    def __init__(self, settings, state, http_get: HttpGet | None = None) -> None:
        self.settings = settings
        self.state = state
        self._get = http_get

    # ------------------------------------------------------------------
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "trends_enabled", True))

    def _data(self) -> dict:
        d = self.state.data.get("trends")
        if not isinstance(d, dict):
            d = self.state.data["trends"] = {}
        return d

    def _fetch(self, url: str, accept: str) -> str | None:
        get = self._get or _default_get
        try:
            status, text = get(url, accept)
        except Exception as e:  # noqa: BLE001 — une source en panne n'arrête rien
            log.info("Tendances : %s illisible (%s)", url.split("/")[2] if "//" in url else url, type(e).__name__)
            return None
        if status != 200 or not text:
            log.info("Tendances : %s → HTTP %s", url.split("/")[2] if "//" in url else url, status)
            return None
        return text

    def _google(self, geo: str) -> list[dict] | None:
        text = self._fetch(GOOGLE_URL.format(geo=geo), RSS_ACCEPT)
        if text is None:
            return None
        rows = [r for r in parse_google_trends(text)
                if acceptable(r["term"]) and (not r["news"] or acceptable(r["news"]))]
        return rows[:KEEP_PER_SOURCE]

    def _wiki(self, now) -> list[dict] | None:
        # Les chiffres d'une journée sont publiés quelques heures après minuit (UTC) : la
        # veille d'abord, l'avant-veille si la veille n'est pas encore prête.
        day = now.astimezone(timezone.utc).date()
        for back in (1, 2):
            text = self._fetch(WIKI_URL.format(day=day - timedelta(days=back)), JSON_ACCEPT)
            if text is None:
                continue
            rows = [r for r in parse_wiki_top(text) if acceptable(r["title"])]
            if rows:
                return rows[:KEEP_PER_SOURCE]
        return None

    def _hn(self) -> list[dict] | None:
        text = self._fetch(HN_URL, JSON_ACCEPT)
        if text is None:
            return None
        return [r for r in parse_hn(text) if acceptable(r["title"])][:KEEP_PER_SOURCE]

    # ------------------------------------------------------------------
    def refresh(self, force: bool = False) -> bool:
        """Relève les tendances si le dernier relevé a plus d'une heure. True s'il a eu lieu."""
        if not self.enabled():
            return False
        data = self._data()
        now = now_local()
        last = parse_iso(data.get("checked_at"))
        if not force and last is not None and now - last < timedelta(minutes=TRENDS_EVERY_MIN):
            return False
        data["checked_at"] = iso(now)
        found: list[str] = []
        sources: list[tuple[str, Callable[[], list[dict] | None]]] = [
            (f"google_{geo.lower()}", (lambda g=geo: self._google(g))) for geo, _name in GOOGLE_GEOS]
        sources += [("wiki", lambda: self._wiki(now)), ("hn", self._hn)]
        for key, fn in sources:
            try:
                rows = fn()
            except Exception as e:  # noqa: BLE001
                log.info("Tendances : %s en panne (%s)", key, type(e).__name__)
                rows = None
            if rows:
                data[key] = {"at": iso(now), "rows": rows}
                found.append(f"{key} {len(rows)}")
        self.state.flush()
        log.info("Tendances : %s", ", ".join(found) if found else "aucune source lisible")
        return True

    def _rows(self, key: str, now) -> list[dict]:
        entry = self._data().get(key)
        if not isinstance(entry, dict):
            return []
        at = parse_iso(entry.get("at"))
        if at is None or now - at > timedelta(hours=SOURCE_MAX_AGE_H):
            return []
        rows = entry.get("rows")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def block(self) -> str:
        """Le bloc (anglais) donné à la rédaction en chef ; "" sans tendance."""
        if not self.enabled():
            return ""
        try:
            self.refresh()
        except Exception as e:  # noqa: BLE001
            log.warning("Tendances : relevé impossible (%s)", e)
        try:
            return self._block()
        except Exception as e:  # noqa: BLE001
            log.warning("Tendances : bloc impossible (%s)", e)
            return ""

    def _block(self) -> str:
        now = now_local()
        lines: list[str] = []
        for geo, name in GOOGLE_GEOS:
            rows = self._rows(f"google_{geo.lower()}", now)[:MAX_GOOGLE]
            if rows:
                lines.append(f"- Rising Google searches in {name}: " + "; ".join(
                    f"{_short(r.get('term', ''), 40)} ({_num(r.get('traffic') or 0)}+ searches"
                    + (f": {_short(r['news'], 50)}" if r.get("news") else "") + ")" for r in rows))
        wiki = self._rows("wiki", now)[:MAX_WIKI]
        if wiki:
            lines.append("- Most-read English Wikipedia pages yesterday: " + "; ".join(
                f"{_short(r.get('title', ''), 50)} ({_num(r.get('views') or 0)} views)" for r in wiki))
        hn = self._rows("hn", now)[:MAX_HN]
        if hn:
            lines.append("- Hacker News front page (the tech crowd): " + "; ".join(
                f"\"{_short(r.get('title', ''), 80)}\" ({_num(r.get('points') or 0)} points)" for r in hn))
        liked = x_most_liked(self.state.data.get("news_x_cache"), now)
        if liked:
            lines.append(f"- Most-liked official posts on X (last {X_LIKED_HOURS} h): " + "; ".join(
                f"@{r['handle']} \"{_short(r['text'], 90)}\" ({_num(r['likes'])} likes)" for r in liked))
        if not lines:
            return ""
        return ("TRENDING TODAY — what the public searches, reads and likes right now. Context to choose a "
                "story, NEVER a source of facts:\n" + "\n".join(lines))
