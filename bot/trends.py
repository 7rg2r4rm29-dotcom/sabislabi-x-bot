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

Veille du marché (consigne du propriétaire, 23/11/2026 : « analyse le marché… pour capturer ce
qui marche et ce qui ramène de l'engagement » ; il paie les lectures X avec ses crédits) : une fois
par jour, les derniers posts de quelques gros comptes qui racontent la tech et l'IA à tout le monde
(`market_accounts`, `MARKET_WATCH_ENABLED`), classés par réactions. Lectures X payantes (0,005 $ le
post, 10 par compte) et optionnelles : elles cèdent au budget X.

Les tendances ne sont JAMAIS une source de faits : elles aident seulement la rédaction en
chef à choisir, parmi les actus du jour, celle qui parlera le plus aux gens.
"""
from __future__ import annotations

import html
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
MARKET_EVERY_H = 24              # veille du marché : une lecture par jour (payante)
MARKET_POSTS_PER_ACCOUNT = 10
MARKET_KEEP_HOURS = 48
MARKET_KEEP = 60
MAX_MARKET = 6
MARKET_STOP_KINDS = {"budget", "rate_limited", "credits", "auth", "network", "server", "dry_run"}

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


def engagement(row: dict) -> int:
    """Réactions d'un post : likes, partages (comptés double : ils emmènent le post plus loin)
    et réponses."""
    def n(key: str) -> int:
        try:
            return int(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    return n("likes") + 2 * n("reposts") + n("replies")


def market_row(handle: str, tw: dict) -> dict | None:
    """Un post de la veille du marché, tel qu'on le garde ; None s'il est inutilisable ou hors
    ligne éditoriale."""
    if not isinstance(tw, dict) or not tw.get("id"):
        return None
    text = _clean(_URL_RE.sub("", html.unescape(str(tw.get("text") or ""))))
    if not text or text.startswith("RT @") or not acceptable(text):
        return None
    from .sources import parse_datetime
    created = parse_datetime(tw.get("created_at"))
    pm = tw.get("public_metrics") or {}

    def n(key: str) -> int:
        try:
            return int(pm.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    return {"id": str(tw["id"]), "handle": str(handle).lstrip("@"), "text": text[:600],
            "created_at": iso(created) if created else "", "likes": n("like_count"),
            "reposts": n("retweet_count") + n("quote_count"), "replies": n("reply_count")}


def _short(text: str, n: int) -> str:
    t = _clean(text)
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _num(n: int) -> str:
    return f"{int(n):,}"


class TrendWatch:
    def __init__(self, settings, state, http_get: HttpGet | None = None, x=None) -> None:
        self.settings = settings
        self.state = state
        self._get = http_get
        self.x = x                     # client X : seulement pour la veille du marché (lectures payantes)

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

    # ------------------------------------------------------------------
    # Veille du marché : gros comptes du même sujet, une fois par jour
    # ------------------------------------------------------------------
    def market_enabled(self) -> bool:
        return (bool(getattr(self.settings, "market_watch_enabled", False)) and self.x is not None
                and bool(getattr(self.settings, "market_accounts", None)))

    def _market(self) -> dict:
        data = self._data()
        m = data.get("market")
        if not isinstance(m, dict):
            m = data["market"] = {}
        return m

    def refresh_market(self, force: bool = False) -> bool:
        """Lit les derniers posts des comptes de la veille, une fois par 24 h au plus. True si
        la lecture a eu lieu. Ne lève jamais d'exception."""
        if not self.market_enabled():
            return False
        m = self._market()
        now = now_local()
        last = parse_iso(m.get("checked_at"))
        if not force and last is not None and now - last < timedelta(hours=MARKET_EVERY_H):
            return False
        m["checked_at"] = iso(now)                 # marqué AVANT : jamais deux lectures payantes de suite
        self.state.flush()
        handles = list(self.settings.market_accounts)
        ids = {str(k).lower(): str(v) for k, v in (m.get("ids") or {}).items()} if isinstance(m.get("ids"), dict) \
            else {}
        missing = [h for h in handles if h.lower() not in ids]
        if missing:
            try:
                found = {str(k).lower(): str(v) for k, v in (self.x.user_ids(missing) or {}).items()}
                # Un compte introuvable est noté comme tel (""), pour ne pas le chercher chaque jour.
                ids.update({h.lower(): found.get(h.lower(), "") for h in missing})
            except Exception as e:  # noqa: BLE001
                log.info("Veille : comptes introuvables (%s)", getattr(e, "kind", type(e).__name__))
        rows = {str(r.get("id")): r for r in (m.get("rows") or []) if isinstance(r, dict) and r.get("id")}
        read = 0
        for h in handles:
            uid = ids.get(h.lower())
            if not uid:
                continue
            try:
                tweets, _newest = self.x.user_posts(uid, max_results=MARKET_POSTS_PER_ACCOUNT, optional=True)
            except Exception as e:  # noqa: BLE001
                kind = getattr(e, "kind", type(e).__name__)
                log.info("Veille : @%s illisible (%s)", h, kind)
                if kind in MARKET_STOP_KINDS:
                    break
                continue
            read += 1
            for tw in tweets or []:
                row = market_row(h, tw)
                if row is not None:
                    rows[row["id"]] = row
        limit = now - timedelta(hours=MARKET_KEEP_HOURS)
        kept = [r for r in rows.values() if (parse_iso(r.get("created_at")) or limit) > limit]
        kept.sort(key=engagement, reverse=True)
        m["ids"] = ids
        m["rows"] = kept[:MARKET_KEEP]
        if read:
            m["at"] = iso(now)
        self.state.flush()
        log.info("Veille : %d compte(s) lu(s) sur %d, %d post(s) gardé(s)", read, len(handles), len(m["rows"]))
        return True

    def _market_rows(self, now) -> list[dict]:
        m = self._data().get("market")
        if not isinstance(m, dict):
            return []
        at = parse_iso(m.get("at"))
        if at is None or now - at > timedelta(hours=MARKET_EVERY_H + 12):
            return []
        limit = now - timedelta(hours=MARKET_KEEP_HOURS)
        rows = [r for r in (m.get("rows") or []) if isinstance(r, dict)
                and (parse_iso(r.get("created_at")) or limit) > limit]
        return sorted(rows, key=engagement, reverse=True)

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
            self.refresh_market()
        except Exception as e:  # noqa: BLE001
            log.warning("Veille : lecture impossible (%s)", e)
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
        market = self._market_rows(now)[:MAX_MARKET]
        if market:
            lines.append(f"- What gets people reacting in our niche on X (big accounts telling tech and AI news to "
                         f"everyone, last {MARKET_KEEP_HOURS} h, most reactions first — learn what kind of story "
                         "and what angle works, never copy them): " + "; ".join(
                             f"@{r['handle']} \"{_short(r['text'], 110)}\" ({_num(r.get('likes') or 0)} likes, "
                             f"{_num(r.get('reposts') or 0)} reposts)" for r in market))
        liked = x_most_liked(self.state.data.get("news_x_cache"), now)
        if liked:
            lines.append(f"- Most-liked official posts on X (last {X_LIKED_HOURS} h): " + "; ".join(
                f"@{r['handle']} \"{_short(r['text'], 90)}\" ({_num(r['likes'])} likes)" for r in liked))
        if not lines:
            return ""
        return ("TRENDING TODAY — what the public searches, reads and likes right now. Context to choose a "
                "story, NEVER a source of facts:\n" + "\n".join(lines))
