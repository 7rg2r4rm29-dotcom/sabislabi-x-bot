"""Statistiques de mes posts, notes de performance pour Claude, récap du matin
et bilan de la semaine.

- `refresh_metrics()` : relit mes posts récents (« owned read », 0,001 $ le
  post) et range leurs statistiques publiques dans `state.published[*].metrics`,
  puis recalcule les agrégats dans `state.data["performance"]`.
  `maybe_refresh()` ne le fait qu'une fois toutes les ~3 h.
- `performance_notes()` : 3 à 7 lignes EN ANGLAIS pour Claude (ce qui marche
  sur CE compte), vide tant qu'il n'y a pas assez de posts mesurés.
- `daily_report()` / `weekly_report()` : textes Telegram en français (HTML).

Seuls les posts publiés depuis plus de 24 h comptent dans les agrégats : avant,
les chiffres bougent encore beaucoup (cold start ~2 h, puis diffusion).
"""
from __future__ import annotations

import logging
import re
import statistics
from datetime import timedelta

from .telegram import esc
from .utils import iso, now_local, parse_iso, today_str
from .x_api import XError

log = logging.getLogger("xbot.analytics")

KINDS = ("gm", "tip", "news", "thread", "reactive", "explainer")
KIND_FR = {"gm": "gm", "tip": "futur", "news": "actu", "thread": "thread", "reactive": "à chaud", "explainer": "vidéo",
           "comment_reply": "réponse"}
JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")

# Toutes les ~6 h (3 h avant le 23/11/2026) : relire nos 50 derniers posts coûtait 0,45 $ par jour,
# le plus gros poste chez X ; les chiffres se stabilisent en 24 h. L'économie paie la veille du marché.
REFRESH_EVERY_MIN = 350
SETTLE_HOURS = 24          # un post compte dans les agrégats après 24 h
AGG_DAYS = 30              # fenêtre des agrégats par type / heure / format
BEST_DAYS = 14             # fenêtre du meilleur / pire post
MIN_POSTS_FOR_NOTES = 5
MIN_GROUP = 2              # un groupe (type, heure…) doit avoir ≥ 2 posts pour être comparé
OVER, UNDER = 1.25, 0.8    # seuils « surperforme » / « sous-performe »
IGNORED_TIP_MIN = 5        # nb mini de brouillons clos pour juger
IGNORED_TIP_RATIO = 0.6

METRIC_KEYS = ("impressions", "likes", "replies", "reposts", "quotes", "bookmarks")

# « Ce qui marche » (consigne du propriétaire, 23/11/2026 : insister sur les posts qui
# marchent, dans le même style, tant que ça marche). Chaque post reçoit un sujet (Claude le
# classe une fois, `ensure_topics`) ; sa « portée » compte les vues ET les interactions.
TOPICS: dict[str, tuple[str, str]] = {      # clé : (nom court, description donnée à Claude)
    "games": ("video games & play", "video games, play, toys and fun"),
    "cars": ("cars & getting around", "cars, driving, taxis and getting around (self-driving, electric, flying)"),
    "robots": ("robots", "robots and machines doing things in the real world"),
    "ai-feats": ("AI doing astonishing things",
                 "an AI doing something astonishing: solving, discovering, creating, beating people"),
    "ai-apps": ("new features in AI apps",
                "a new feature in AI apps and assistants people use (ChatGPT, Gemini, Claude, Siri, Alexa…)"),
    "gadgets": ("phones, glasses & devices", "phones, glasses, watches, TVs and other devices"),
    "daily-life": ("home, family, school & money", "home, family, school, work, shopping and money in daily life"),
    "privacy-safety": ("privacy, scams & tech going wrong", "privacy, scams, security, and AI or tech going wrong"),
    "science": ("science, health & space", "science, health, space and the body, when AI is not the point"),
    "business": ("tech business & money raised", "companies, money raised, deals, prices and the tech industry"),
    "insider": ("tools for developers & tech jargon",
                "tools and models for developers or specialists, benchmarks, tech jargon"),
}
# Jamais poussés, même s'ils marchent : la ligne éditoriale vise le grand public, sans jargon
# (le 23/11/2026, les sujets pour développeurs faisaient 1,3x, sans doute l'ancien public tech).
OFF_LINE_TOPICS = ("insider",)
ENGAGEMENT_WEIGHT = 30     # une interaction (like, réponse, partage, citation, signet) vaut 30 vues
LIFT_CAP = 4.0             # un post viral compte, mais pas plus que 4 fois la normale
PRIOR_POSTS = 2.0          # deux posts « habituels » imaginaires par groupe : un seul succès ne fait pas une règle
HALF_LIFE_DAYS = 7.0       # « tant que ça marche » : un post d'il y a 7 jours pèse moitié moins
WORKS, FLOPS = 1.15, 0.85  # au-dessus : ça marche ; en dessous : ça tombe à plat
MIN_WORKS_N = 2            # au moins 2 posts pour dire qu'un sujet marche
MIN_FLOPS_N = 3            # au moins 3 pour dire qu'il ne marche pas
STYLE_EXAMPLES = 3         # nos meilleurs posts, montrés au rédacteur comme modèles de style
STYLE_TEXT_MAX = 320
TOPIC_LABEL_EVERY_MIN = 360
TOPIC_LABEL_BATCH = 120
# Le format suit les résultats (consigne du propriétaire, 24/11/2026 : « visuel ou textuel, peu
# importe : choisis la meilleure option à chaque fois »). Voir Analytics.choose_format.
_FAMOUS_RE = re.compile(r"\b(?:GTA|Grand Theft Auto|ChatGPT|OpenAI|Tesla|Apple|iPhone|iPad|Google|Gemini|Meta|"
                        r"Netflix|Amazon|Alexa|Siri|Nintendo|PlayStation|Xbox|Samsung|Microsoft|Claude|Neuralink|Waymo|"
                        r"Uber|Spotify|YouTube|TikTok|Instagram|WhatsApp|Disney|Sony|NASA|SpaceX|Pok[eé]mon)\b")
_BREAKING_RE = re.compile(r"\b(?:just|now|finally|new|first)\b", re.I)
FORMAT_KINDS = ("explainer", "thread", "tip")   # ce qui peut occuper le grand créneau du jour
SPECIAL_KINDS = ("explainer", "thread")         # ce qui peut aussi remplacer un post d'actu, une fois prouvé
FORMAT_MIN_TRIES = 3       # un format encore peu publié est d'abord essayé 3 fois
FORMAT_MIN_SHARE = 0.10    # chaque format garde 1 passage sur 10 : sinon on ne saurait jamais s'il remarche
FORMAT_SHARPEN = 2.0       # l'écart est accentué : 1,6x contre 1,0x donne 72 % / 28 %
FORMAT_PROVEN_MIN = 4      # posts mesurés avant de dire qu'un format bat nettement un post d'actu
FORMAT_PROVEN_RATIO = 1.3  # … et de combien
PHOTO_MIN_POSTS = 12       # posts mesurés avec photo ET en texte seul avant de couper les photos
PHOTO_FLOP_RATIO = 0.70    # photos coupées quand elles font au plus 70 % du texte seul (un écart net :
#                            le 24/11/2026, 0,85 — rien de clair, et les vieux posts en texte seul venaient
#                            d'une autre ligne éditoriale)…
PHOTO_BACK_RATIO = 0.85    # … et remises dès qu'elles remontent à 85 % : pas de va-et-vient d'un jour à l'autre


# ---------------------------------------------------------------------------
# Petites aides
# ---------------------------------------------------------------------------
def metrics_of(tweet: dict) -> dict:
    pm = tweet.get("public_metrics") or {}
    return {
        "impressions": int(pm.get("impression_count") or 0),
        "likes": int(pm.get("like_count") or 0),
        "replies": int(pm.get("reply_count") or 0),
        "reposts": int(pm.get("retweet_count") or 0),
        "quotes": int(pm.get("quote_count") or 0),
        "bookmarks": int(pm.get("bookmark_count") or 0),
        "fetched_at": iso(now_local()),
    }


def _m(p: dict, key: str) -> int:
    m = p.get("metrics") if isinstance(p.get("metrics"), dict) else {}
    try:
        return int(m.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def stats(posts: list[dict]) -> dict:
    n = len(posts)
    tot = {k: sum(_m(p, k) for p in posts) for k in METRIC_KEYS}
    eng = tot["likes"] + tot["replies"] + tot["reposts"] + tot["bookmarks"]
    return {
        "count": n,
        "avg_impressions": round(tot["impressions"] / n, 1) if n else 0.0,
        "avg_likes": round(tot["likes"] / n, 2) if n else 0.0,
        "avg_replies": round(tot["replies"] / n, 2) if n else 0.0,
        "avg_reposts": round(tot["reposts"] / n, 2) if n else 0.0,
        "avg_bookmarks": round(tot["bookmarks"] / n, 2) if n else 0.0,
        "engagement_rate": round(eng / tot["impressions"], 4) if tot["impressions"] else 0.0,
    }


def post_score(p: dict) -> float:
    """La portée d'un post : ses vues, plus 30 vues par interaction (like, réponse, partage,
    citation, signet) — X pousse les posts qui font réagir, pas seulement ceux qu'on voit."""
    eng = sum(_m(p, k) for k in ("likes", "replies", "reposts", "quotes", "bookmarks"))
    return float(_m(p, "impressions") + ENGAGEMENT_WEIGHT * eng)


def excerpt(text: str, n: int = 90) -> str:
    t = " / ".join(line.strip() for line in (text or "").splitlines() if line.strip())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def fr_int(n: float | int) -> str:
    return f"{int(round(n)):,}".replace(",", " ")


def en_int(n: float | int) -> str:
    return f"{int(round(n)):,}"


def _ratio(value: float, ref: float) -> float | None:
    if not ref:
        return None
    return value / ref


def _local(p: dict):
    dt = parse_iso(p.get("created_at"))
    return dt.astimezone(now_local().tzinfo) if dt is not None else None


class Analytics:
    def __init__(self, settings, state, x, budget) -> None:
        self.settings = settings
        self.state = state
        self.x = x
        self.budget = budget

    # ==================================================================
    # Relevé des statistiques
    # ==================================================================
    def maybe_refresh(self) -> int:
        """`refresh_metrics()` au plus une fois toutes les ~3 h, et seulement
        s'il y a des posts récents à mesurer. Ne lève jamais d'exception."""
        since = self.state.minutes_since("metrics")
        if since is not None and since < REFRESH_EVERY_MIN:
            return 0
        if not self.state.published_since(24 * (BEST_DAYS + 1)):
            return 0
        return self.refresh_metrics()

    def refresh_metrics(self) -> int:
        """Relit mes posts récents et met à jour leurs stats. Retourne le nombre
        de posts mis à jour (0 en cas d'erreur)."""
        self.state.set_last_run("metrics")
        try:
            tweets = self.x.my_recent_posts(max_results=50) or []
        except XError as e:
            log.warning("Stats : lecture de mes posts impossible (%s) : %s", e.kind, e)
            return 0
        except Exception as e:  # noqa: BLE001
            log.exception("Stats : erreur inattendue : %s", e)
            return 0
        updated = 0
        try:
            by_id = {str(t.get("id")): t for t in tweets if isinstance(t, dict) and t.get("id")}
            for p in self.state.data.get("published") or []:
                tw = by_id.get(str(p.get("tweet_id")))
                if tw is None:
                    continue
                p["metrics"] = metrics_of(tw)
                self._remember_format(p)
                updated += 1
            self.recompute()
        except Exception as e:  # noqa: BLE001
            log.exception("Stats : calcul des agrégats impossible : %s", e)
        self._maybe_record_followers()
        self.state.flush()
        log.info("Stats : %d post(s) relu(s), %d mis à jour", len(tweets), updated)
        return updated

    def _remember_format(self, p: dict) -> None:
        """Note une fois pour toutes le format du post (image, sondage, genre de
        thread, note perso…) : l'item d'origine est supprimé au bout de 10 jours."""
        if p.get("fmt"):
            return
        it = self.state.item(p.get("item_id")) if p.get("item_id") else None
        if not it:
            return
        meta = it.get("meta") or {}
        media = "poll" if it.get("poll") else (("video" if it.get("image_kind") == "video" else "image")
                                               if it.get("image_path") else "text")
        p["fmt"] = {
            "media": media,
            "image_kind": it.get("image_kind") or "",
            "genre": str(meta.get("angle") or "") if p.get("kind") == "thread" else "",
            "hook": str(meta.get("hook") or ""),
            "notes": bool(meta.get("used_notes")),
            "fallback": bool(meta.get("from_fallback")),
            "test": bool(meta.get("style_test")),
            "tweets": len(it.get("texts") or []),
        }

    def _maybe_record_followers(self) -> None:
        """Nombre d'abonnés, une fois par jour (0,01 $ la lecture). `me()` est
        optionnel : absent des doublures de test, il peut aussi échouer."""
        me = getattr(self.x, "me", None)
        if not callable(me):
            return
        hist = self.state.data.setdefault("followers", [])
        today = today_str()
        if hist and isinstance(hist[-1], dict) and hist[-1].get("date") == today:
            return
        try:
            if self.budget is not None and not self.budget.can_spend_optional(0.01):
                return
            data = me() or {}
            count = (data.get("public_metrics") or {}).get("followers_count")
        except Exception as e:  # noqa: BLE001
            log.info("Stats : nombre d'abonnés indisponible (%s)", getattr(e, "kind", type(e).__name__))
            return
        if count is None:
            return
        hist.append({"date": today, "count": int(count)})
        self.state.data["followers"] = hist[-120:]

    # ==================================================================
    # Agrégats
    # ==================================================================
    def _posts(self, days: float, *, settled: bool = True, with_metrics: bool = True) -> list[dict]:
        now = now_local()
        out = []
        for p in self.state.data.get("published") or []:
            if p.get("kind") not in KINDS:
                continue
            if with_metrics and not isinstance(p.get("metrics"), dict):
                continue
            dt = parse_iso(p.get("created_at"))
            if dt is None:
                continue
            age_h = (now - dt).total_seconds() / 3600.0
            if age_h > days * 24 or (settled and age_h < SETTLE_HOURS):
                continue
            out.append(p)
        return out

    @staticmethod
    def _brief(p: dict | None) -> dict | None:
        if not p:
            return None
        return {"tweet_id": p.get("tweet_id"), "kind": p.get("kind"), "text": excerpt(p.get("text") or "", 120),
                "impressions": _m(p, "impressions"), "likes": _m(p, "likes"), "replies": _m(p, "replies"),
                "created_at": p.get("created_at")}

    def compute(self) -> dict:
        posts = self._posts(AGG_DAYS)
        by_kind: dict[str, list] = {}
        by_hour: dict[str, list] = {}
        by_media: dict[str, list] = {}
        by_notes: dict[str, list] = {}
        by_genre: dict[str, list] = {}
        by_hook: dict[str, list] = {}
        for p in posts:
            by_kind.setdefault(p["kind"], []).append(p)
            dt = _local(p)
            if dt is not None:
                by_hour.setdefault(f"{dt.hour:02d}", []).append(p)
            fmt = p.get("fmt") or {}
            if fmt.get("media"):
                by_media.setdefault(fmt["media"], []).append(p)
                by_notes.setdefault("with_notes" if fmt.get("notes") else "without_notes", []).append(p)
            if fmt.get("genre"):
                by_genre.setdefault(fmt["genre"], []).append(p)
            if fmt.get("hook"):
                by_hook.setdefault(fmt["hook"], []).append(p)
        recent_ids = {id(p) for p in self._posts(BEST_DAYS)}
        recent = [p for p in posts if id(p) in recent_ids]
        best = max(recent, key=lambda p: _m(p, "impressions"), default=None)
        worst = min(recent, key=lambda p: _m(p, "impressions"), default=None)
        return {
            "updated_at": iso(now_local()),
            "window_days": AGG_DAYS,
            "settled_posts": len(posts),
            "overall": stats(posts),
            "by_kind": {k: stats(v) for k, v in sorted(by_kind.items())},
            "by_hour": {h: stats(v) for h, v in sorted(by_hour.items())},
            "by_format": {k: stats(v) for k, v in sorted(by_media.items())},
            "by_notes": {k: stats(v) for k, v in sorted(by_notes.items())},
            "by_thread_genre": {k: stats(v) for k, v in sorted(by_genre.items())},
            "by_hook": {k: stats(v) for k, v in sorted(by_hook.items())},
            "best": self._brief(best),
            "worst": self._brief(worst) if worst is not best else None,
        }

    def recompute(self) -> dict:
        perf = self.compute()
        self.state.data["performance"] = perf
        self.state.flush()
        return perf

    # ==================================================================
    # Ce qui marche : sujets, accroches, formats (consigne du 23/11/2026)
    # ==================================================================
    def ensure_topics(self, labeler) -> int:
        """Donne un sujet (TOPICS) aux posts des 30 derniers jours qui n'en ont pas encore :
        un seul appel à Claude (`labeler(textes) -> sujets`, dans le même ordre), toutes les
        6 h au plus. Retourne le nombre de posts classés ; ne lève jamais d'exception."""
        if not callable(labeler):
            return 0
        try:
            since = self.state.minutes_since("topics")
            if since is not None and since < TOPIC_LABEL_EVERY_MIN:
                return 0
            todo = [p for p in self._posts(AGG_DAYS, settled=False, with_metrics=False)
                    if p.get("topic") not in TOPICS and (p.get("text") or "").strip()][-TOPIC_LABEL_BATCH:]
            if not todo:
                return 0
            self.state.set_last_run("topics")
            labels = labeler([str(p.get("text") or "") for p in todo]) or []
            n = 0
            for p, topic in zip(todo, labels):
                if topic in TOPICS:
                    p["topic"] = topic
                    n += 1
            self.state.flush()
            log.info("Stats : %d post(s) classé(s) par sujet sur %d", n, len(todo))
            return n
        except Exception as e:  # noqa: BLE001
            log.warning("Stats : classement des posts par sujet impossible (%s)", e)
            return 0

    def what_works(self) -> dict:
        """Ce qui marche sur CE compte, sur 30 jours : pour chaque sujet, forme d'accroche,
        type de post et format, la portée moyenne comparée à un post habituel (1.0), vues et
        interactions comprises (`post_score`). Les posts récents pèsent plus (moitié moins
        tous les 7 jours : « tant que ça marche ») ; un groupe d'un ou deux posts reste
        proche de 1.0 (deux posts « habituels » imaginaires) ; un post viral compte au plus
        4 fois la normale. `best` : nos meilleurs posts des 14 derniers jours, s'ils ont
        fait mieux que d'habitude. {} tant qu'il y a moins de 5 posts mesurés."""
        posts = self._posts(AGG_DAYS)
        if len(posts) < MIN_POSTS_FOR_NOTES:
            return {}
        scores = [post_score(p) for p in posts]
        base = statistics.median(scores)
        if base <= 0:
            return {}
        now = now_local()

        def weight(p: dict) -> float:
            dt = parse_iso(p.get("created_at"))
            age = max(0.0, (now - dt).total_seconds() / 86400.0) if dt is not None else float(AGG_DAYS)
            return 0.5 ** (age / HALF_LIFE_DAYS)

        weights = [weight(p) for p in posts]

        def fmt_of(p: dict) -> dict:
            f = p.get("fmt")
            return f if isinstance(f, dict) else {}

        def table(key) -> dict:
            acc: dict[str, list] = {}
            for p, s, w in zip(posts, scores, weights):
                k = key(p)
                if not k:
                    continue
                a = acc.setdefault(str(k), [0.0, 0.0, 0])
                a[0] += w * min(LIFT_CAP, s / base)
                a[1] += w
                a[2] += 1
            return {k: {"lift": round((sl + PRIOR_POSTS) / (sw + PRIOR_POSTS), 2), "count": n}
                    for k, (sl, sw, n) in acc.items()}

        recent = [p for p in self._posts(BEST_DAYS) if (p.get("text") or "").strip()
                  and post_score(p) >= base * WORKS and p.get("topic") not in OFF_LINE_TOPICS]
        recent.sort(key=post_score, reverse=True)
        best = [{"text": str(p.get("text") or ""), "kind": p.get("kind"),
                 "impressions": _m(p, "impressions"), "likes": _m(p, "likes"), "replies": _m(p, "replies"),
                 "reposts": _m(p, "reposts") + _m(p, "quotes"), "bookmarks": _m(p, "bookmarks"),
                 "lift": round(post_score(p) / base, 2)} for p in recent[:STYLE_EXAMPLES]]
        return {
            "baseline": base,
            "posts": len(posts),
            "topics": table(lambda p: p.get("topic") if p.get("topic") in TOPICS else None),
            "hooks": table(lambda p: fmt_of(p).get("hook")),
            "kinds": table(lambda p: p.get("kind")),
            "formats": table(lambda p: fmt_of(p).get("media")),
            "best": best,
        }

    # ==================================================================
    # La feuille de chiffres de l'agent d'analyse (bot/strategy.py)
    # ==================================================================
    def figures_sheet(self) -> tuple[str, int]:
        """Pour l'agent d'analyse (anglais) : tout ce que nos chiffres disent sur 30 jours —
        portée par sujet, type, format, accroche, heure, longueur, fin de post, premier mot —
        avec le nombre de posts de chaque groupe, la tendance des 7 derniers jours, nos
        meilleurs et nos moins bons posts. ("", 0) sans assez de chiffres. Ne lève jamais."""
        try:
            return self._figures_sheet()
        except Exception as e:  # noqa: BLE001
            log.exception("Stats : feuille de chiffres impossible : %s", e)
            return "", 0

    def _figures_sheet(self) -> tuple[str, int]:
        posts = self._posts(AGG_DAYS)
        if len(posts) < MIN_POSTS_FOR_NOTES:
            return "", 0
        scores = [post_score(p) for p in posts]
        base = statistics.median(scores)
        if base <= 0:
            return "", 0
        now = now_local()

        def age_days(p: dict) -> float:
            dt = parse_iso(p.get("created_at"))
            return max(0.0, (now - dt).total_seconds() / 86400.0) if dt is not None else float(AGG_DAYS)

        weights = [0.5 ** (age_days(p) / HALF_LIFE_DAYS) for p in posts]

        def table(key) -> str:
            acc: dict[str, list] = {}
            for p, sc, w in zip(posts, scores, weights):
                k = key(p)
                if not k:
                    continue
                a = acc.setdefault(str(k), [0.0, 0.0, 0])
                a[0] += w * min(LIFT_CAP, sc / base)
                a[1] += w
                a[2] += 1
            rows = sorted(((round((sl + PRIOR_POSTS) / (sw + PRIOR_POSTS), 2), k, n) for k, (sl, sw, n) in acc.items()),
                          reverse=True)
            return ", ".join(f"{k} {lift:.2f}x ({n})" for lift, k, n in rows) or "no data"

        def first_line(p: dict) -> str:
            return (str(p.get("text") or "").strip().splitlines() or [""])[0]

        def fmt_of(p: dict) -> dict:
            f = p.get("fmt")
            return f if isinstance(f, dict) else {}

        def hour_bucket(p: dict) -> str:
            dt = _local(p)
            if dt is None:
                return ""
            h = dt.hour
            return ("06-11h" if 6 <= h < 12 else "12-15h" if h < 16 and h >= 12 else "16-18h" if 16 <= h < 19
                    else "19-22h" if 19 <= h < 23 else "23-05h")

        def length_bucket(p: dict) -> str:
            n = len(str(p.get("text") or ""))
            return "under 160 characters" if n < 160 else "160-220" if n <= 220 else "over 220"

        def reactions(p: dict) -> int:
            return sum(_m(p, k) for k in ("likes", "replies", "reposts", "quotes", "bookmarks"))

        def week(lo: float, hi: float) -> str:
            rows = [p for p in posts if lo <= age_days(p) < hi]
            if not rows:
                return "no posts"
            views = statistics.median(_m(p, "impressions") for p in rows)
            return f"{len(rows)} posts, median {views:.0f} views, {sum(reactions(p) for p in rows)} reactions"

        def line(p: dict) -> str:
            dt = _local(p)
            when = dt.strftime("%a %d %b %H:%M") if dt is not None else "?"
            return (f"  {_m(p, 'impressions')} views, {reactions(p)} reactions | {when} | {p.get('kind')} | "
                    f"{p.get('topic') or '?'} | {fmt_of(p).get('media') or '?'} | {excerpt(first_line(p), 120)}")

        ranked = sorted(posts, key=post_score, reverse=True)
        total = {k: sum(_m(p, k) for p in posts) for k in METRIC_KEYS}
        out = [
            f"OUR NUMBERS — the {len(posts)} posts of the last {AGG_DAYS} days that are at least {SETTLE_HOURS} hours "
            "old. \"1.00x\" is our usual post: views plus 30 per like, reply, repost, quote or bookmark; recent posts "
            "weigh more (half as much every 7 days); a group of one or two posts stays close to 1.00x. In "
            "brackets: how many posts the figure rests on.",
            f"- Totals: {total['impressions']} views, {total['likes']} likes, {total['replies']} replies, "
            f"{total['reposts'] + total['quotes']} reposts and quotes, {total['bookmarks']} bookmarks. Usual post: "
            f"{statistics.median(_m(p, 'impressions') for p in posts):.0f} views.",
            f"- Last 7 days: {week(1, 8)}. The 7 days before: {week(8, 15)}.",
            "- By topic: " + table(lambda p: p.get("topic") if p.get("topic") in TOPICS else None) + ".",
            "- By kind of post (tip = \"the future is coming\", news = story of the day, reactive = breaking, "
            "explainer = animated video): " + table(lambda p: p.get("kind")) + ".",
            "- By media: " + table(lambda p: fmt_of(p).get("media")) + ".",
            "- By shape of the first line: " + table(lambda p: fmt_of(p).get("hook")) + ".",
            "- By time of day (Paris time; New York is 6 hours earlier): " + table(hour_bucket) + ".",
            "- By day: " + table(lambda p: ("weekend" if _local(p).weekday() >= 5 else "weekday")
                                 if _local(p) is not None else None) + ".",
            "- By length of the post: " + table(length_bucket) + ".",
            "- By last line: " + table(lambda p: "ends with a question"
                                       if str(p.get("text") or "").rstrip().endswith("?") else "ends with a statement") + ".",
            "- First line has a number: " + table(lambda p: "yes" if re.search(r"\d", first_line(p)) else "no") + ".",
            "- First line names something everybody knows (GTA, ChatGPT, Tesla, iPhone…): "
            + table(lambda p: "yes" if _FAMOUS_RE.search(first_line(p)) else "no") + ".",
            "- First line sounds like breaking news (just, now, finally, new, first): "
            + table(lambda p: "yes" if _BREAKING_RE.search(first_line(p)) else "no") + ".",
            "- Test posts (1 in 4 tries something else) against the rest: "
            + table(lambda p: ("test" if fmt_of(p).get("test") else "follows what works") if "test" in fmt_of(p) else None) + ".",
            "OUR BEST POSTS:",
            *[line(p) for p in ranked[:8]],
            "OUR WEAKEST POSTS:",
            *[line(p) for p in ranked[-6:]],
        ]
        return "\n".join(out), len(posts)

    # ==================================================================
    # Le format suit les résultats
    # ==================================================================
    def format_table(self) -> dict:
        """Pour chaque type de post (vidéo, thread, « le futur arrive », actu…) : sa portée
        comparée à un post habituel (`lift`), le nombre de posts mesurés (`count`) et le nombre
        de posts publiés sur 30 jours, mesurés ou non (`tries`). {} tant qu'il n'y a pas assez
        de chiffres. Ne lève jamais d'exception."""
        try:
            ww = self.what_works()
            if not ww:
                return {}
            tries: dict[str, int] = {}
            for p in self._posts(AGG_DAYS, settled=False, with_metrics=False):
                tries[p["kind"]] = tries.get(p["kind"], 0) + 1
            kinds = ww.get("kinds") or {}
            return {k: {"lift": float((kinds.get(k) or {}).get("lift", 1.0)),
                        "count": int((kinds.get(k) or {}).get("count", 0)), "tries": int(tries.get(k, 0))}
                    for k in set(kinds) | set(tries) | set(FORMAT_KINDS) | {"news"}}
        except Exception as e:  # noqa: BLE001
            log.exception("Stats : tableau des formats impossible : %s", e)
            return {}

    @staticmethod
    def format_shares(table: dict, candidates: list[str]) -> dict[str, float]:
        """La part de chaque format dans le grand créneau du jour : proportionnelle au carré de
        sa portée (ce qui marche passe nettement plus souvent), jamais sous `FORMAT_MIN_SHARE`."""
        cands = [k for k in candidates if k in FORMAT_KINDS]
        if not cands:
            return {}
        weights = {k: max(0.2, float((table.get(k) or {}).get("lift", 1.0))) ** FORMAT_SHARPEN for k in cands}
        floor = min(FORMAT_MIN_SHARE, 1.0 / len(cands))
        low: set[str] = set()
        while True:                                   # les formats sous le plancher y sont remontés
            rest = sum(w for k, w in weights.items() if k not in low)
            room = 1.0 - floor * len(low)
            shares = {k: (floor if k in low else room * weights[k] / rest) for k in cands}
            under = {k for k in cands if k not in low and shares[k] < floor - 1e-9}
            if not under or len(low) + len(under) >= len(cands):
                return shares
            low |= under

    def choose_format(self, candidates: list[str], rng, pending: dict | None = None) -> tuple[str, str] | None:
        """Le format du grand créneau du jour, parmi `candidates` : (type, raison en français).
        Un format encore peu publié (`FORMAT_MIN_TRIES`, en comptant `pending` : ce qui est
        déjà prévu sans être publié) passe d'abord, pour être mesuré ; ensuite le tirage suit
        les résultats (`format_shares`). None sans chiffres : le planning fixe s'applique."""
        table = self.format_table()
        cands = [k for k in candidates if k in FORMAT_KINDS]
        if not table or not cands:
            return None
        tries = {k: table.get(k, {}).get("tries", 0) + int((pending or {}).get(k, 0)) for k in cands}
        fresh = [k for k in cands if tries[k] < FORMAT_MIN_TRIES]
        if fresh:
            k = min(fresh, key=lambda c: (tries[c], cands.index(c)))
            return k, f"format encore peu essayé ({tries[k]} sur {FORMAT_MIN_TRIES} essais)"
        shares = self.format_shares(table, cands)
        x, acc, picked = rng.random(), 0.0, cands[-1]
        for k in cands:
            acc += shares[k]
            if x < acc:
                picked = k
                break
        detail = ", ".join(f"{KIND_FR.get(k, k)} {table.get(k, {}).get('lift', 1.0):.2f}x → {shares[k] * 100:.0f} %"
                           for k in cands)
        return picked, f"tiré selon les résultats ({detail})"

    def second_format(self, first: str, candidates: list[str]) -> tuple[str, str] | None:
        """Un format qui bat NETTEMENT un post d'actu (au moins `FORMAT_PROVEN_RATIO` fois sa
        portée, sur `FORMAT_PROVEN_MIN` posts mesurés) prend aussi la place d'un post d'actu
        ce jour-là, s'il n'occupe pas déjà le grand créneau. (type, raison) ou None."""
        table = self.format_table()
        if not table:
            return None
        base = max(0.5, float((table.get("news") or {}).get("lift", 1.0)))
        proven = [(row["lift"], k) for k in candidates if k in SPECIAL_KINDS and k != first
                  for row in [table.get(k) or {}]
                  if row.get("count", 0) >= FORMAT_PROVEN_MIN and row.get("lift", 0.0) >= FORMAT_PROVEN_RATIO * base]
        if not proven:
            return None
        lift, k = max(proven)
        return k, (f"{KIND_FR.get(k, k)} {lift:.2f}x contre {base:.2f}x pour un post d'actu "
                   f"({table[k]['count']} posts mesurés)")

    def photo_advice(self) -> tuple[bool, str]:
        """(joindre la photo de l'actu ?, raison). Non seulement quand les chiffres sont nets :
        au moins `PHOTO_MIN_POSTS` posts mesurés de chaque sorte, et les posts avec photo à
        `PHOTO_FLOP_RATIO` fois les posts en texte seul, ou moins. Une fois coupées, les photos
        reviennent quand l'écart se referme (`PHOTO_BACK_RATIO`) ou quand il n'y a plus assez de
        posts avec photo pour juger : la décision est notée dans l'état (`photo_policy`), elle
        ne change pas d'un relevé à l'autre. Ne lève jamais d'exception."""
        try:
            policy = self.state.data.get("photo_policy")
            was_off = isinstance(policy, dict) and bool(policy.get("off"))
            formats = (self.what_works() or {}).get("formats") or {}
            img, txt = formats.get("image") or {}, formats.get("text") or {}
            enough = img.get("count", 0) >= PHOTO_MIN_POSTS and txt.get("count", 0) >= PHOTO_MIN_POSTS
            ratio = float(img.get("lift", 1.0)) / max(0.01, float(txt.get("lift", 1.0)))
            off = enough and (ratio <= PHOTO_FLOP_RATIO or (was_off and ratio < PHOTO_BACK_RATIO))
            if off != was_off:
                self.state.data["photo_policy"] = {"off": off, "since": iso(now_local()), "ratio": round(ratio, 2)}
                self.state.flush()
                log.info("Photos de l'actu : %s (avec photo %.2fx, texte seul %.2fx)",
                         "coupées, les chiffres sont nets" if off else "de retour",
                         img.get("lift", 1.0), txt.get("lift", 1.0))
            if off:
                return False, (f"avec photo {img['lift']:.2f}x ({img['count']} posts), texte seul "
                               f"{txt['lift']:.2f}x ({txt['count']} posts)")
        except Exception as e:  # noqa: BLE001
            log.warning("Stats : avis sur les photos impossible (%s)", e)
        return True, ""

    @staticmethod
    def _winners(table: dict, n_min: int = MIN_WORKS_N) -> list[tuple[float, str, int]]:
        return sorted(((v["lift"], k, v["count"]) for k, v in (table or {}).items()
                       if v["lift"] >= WORKS and v["count"] >= n_min), reverse=True)

    @staticmethod
    def _losers(table: dict, n_min: int = MIN_FLOPS_N) -> list[tuple[float, str, int]]:
        return sorted((v["lift"], k, v["count"]) for k, v in (table or {}).items()
                      if v["lift"] <= FLOPS and v["count"] >= n_min)

    def editor_notes(self, explore: bool = False) -> str:
        """Pour la rédaction en chef (anglais) : les sujets qui marchent et ceux qui tombent à
        plat sur ce compte, nos meilleurs posts récents, et la consigne du choix — suivre ce
        qui marche ou, pour un post d'essai (`explore`), essayer autre chose. "" sans chiffres.
        Ne lève jamais d'exception."""
        try:
            return self._editor_notes(explore)
        except Exception as e:  # noqa: BLE001
            log.exception("Stats : notes pour la rédaction en chef impossibles : %s", e)
            return ""

    def _editor_notes(self, explore: bool) -> str:
        ww = self.what_works()
        if not ww:
            return ""
        wins = [w for w in self._winners(ww["topics"]) if w[1] not in OFF_LINE_TOPICS][:4]
        flops = self._losers(ww["topics"])[:4]
        best = ww["best"]
        if not (wins or flops or best):
            return ""
        lines = [f"WHAT WORKS ON THIS ACCOUNT (our own numbers over the last {AGG_DAYS} days: views, plus "
                 "likes, replies, reposts and bookmarks; 1.0x = our usual post):"]
        if wins:
            lines.append("- Stories our readers react to: " + ", ".join(
                f"{TOPICS[t][0]} ({lift:.1f}x, {n} posts)" for lift, t, n in wins) + ".")
        if flops:
            lines.append("- Stories that fall flat with them: " + ", ".join(
                f"{TOPICS[t][0]} ({lift:.1f}x, {n} posts)" for lift, t, n in flops) + ".")
        if best:
            lines.append("- Our best recent posts: " + "; ".join(
                f"\"{excerpt(b['text'], 110)}\" ({en_int(b['impressions'])} views)" for b in best) + ".")
        if explore:
            lines.append("THIS PICK IS A TEST: choose the strongest story whatever its kind (only avoid the kinds "
                         "that fall flat), so the account learns what else works.")
        else:
            lines.append("THIS PICK FOLLOWS WHAT WORKS: when the list has a strong story of a kind our readers "
                         "react to, or one like our best posts, put it first. Never pick a weak story just for "
                         "its kind.")
        return "\n".join(lines)

    def style_notes(self, explore: bool = False) -> str:
        """Pour le rédacteur (anglais) : nos meilleurs posts récents, en entier, comme modèles
        de STYLE (première ligne, longueur, rythme), et les formes d'accroche qui marchent.
        Pour un post d'essai (`explore`) : la liberté d'essayer un autre style. "" sans
        chiffres. Ne lève jamais d'exception."""
        try:
            return self._style_notes(explore)
        except Exception as e:  # noqa: BLE001
            log.exception("Stats : modèles de style impossibles : %s", e)
            return ""

    def _style_notes(self, explore: bool) -> str:
        ww = self.what_works()
        if not ww:
            return ""
        if explore:
            return ("THIS POST IS A TEST: you may try a different style from our usual one (another kind of first "
                    "line, another rhythm), still in plain words and still true, so the account learns what else "
                    "works.")
        lines: list[str] = []
        best = ww["best"]
        if best:
            lines.append("OUR BEST RECENT POSTS — insist on what works: write in the same style (the same kind of "
                         "first line, the same length and rhythm, the same plain words), never copy their words:")
            for b in best:
                extra = "".join(f", {b[k]} {many if b[k] > 1 else one}"
                                for k, one, many in (("likes", "like", "likes"), ("replies", "reply", "replies"),
                                                     ("reposts", "repost", "reposts"),
                                                     ("bookmarks", "bookmark", "bookmarks")) if b.get(k))
                text = " ".join(b["text"].split())
                if len(text) > STYLE_TEXT_MAX:
                    text = text[: STYLE_TEXT_MAX - 1].rstrip() + "…"
                lines.append(f"  • ({en_int(b['impressions'])} views{extra}) {text}")
        hooks = self._winners(ww["hooks"])[:2]
        if hooks:
            lines.append("First-line shapes that work best here: " + ", ".join(
                f"\"{k}\" ({lift:.1f}x, {n} posts)" for lift, k, n in hooks) + ". Use one when it fits the story.")
        return "\n".join(lines)

    # ==================================================================
    # Notes pour Claude (anglais)
    # ==================================================================
    @staticmethod
    def _compare(groups: dict, overall: dict, key: str = "avg_impressions") -> tuple[list, list]:
        ref = overall.get(key) or 0
        over, under = [], []
        for name, st in groups.items():
            if st.get("count", 0) < MIN_GROUP:
                continue
            r = _ratio(st.get(key) or 0, ref)
            if r is None:
                continue
            if r >= OVER:
                over.append((r, name, st))
            elif r <= UNDER:
                under.append((r, name, st))
        over.sort(reverse=True)
        under.sort()
        return over, under

    def performance_notes(self) -> str:
        """≤ 7 lignes en anglais pour les consignes de Claude ; "" si moins de
        5 posts mesurés. Ne lève jamais d'exception."""
        try:
            return self._performance_notes()
        except Exception as e:  # noqa: BLE001
            log.exception("Stats : notes de performance impossibles : %s", e)
            return ""

    def _performance_notes(self) -> str:
        perf = self.compute()
        ov = perf["overall"]
        if perf["settled_posts"] < MIN_POSTS_FOR_NOTES:
            return ""
        lines = [f"Last {AGG_DAYS} days, {perf['settled_posts']} posts: avg {en_int(ov['avg_impressions'])} "
                 f"impressions, {ov['avg_likes']:.1f} likes, {ov['avg_replies']:.1f} replies, "
                 f"engagement {ov['engagement_rate']:.1%}."]

        over, under = self._compare(perf["by_kind"], ov)
        parts = []
        if over:
            parts.append("over-performing: " + ", ".join(
                f"{name} posts ({r:.1f}x avg impressions, n={st['count']})" for r, name, st in over[:2]))
        if under:
            parts.append("under-performing: " + ", ".join(
                f"{name} posts ({r:.1f}x, n={st['count']})" for r, name, st in under[:2]))
        if parts:
            lines.append("By type, " + "; ".join(parts) + ".")

        # Les réponses pèsent bien plus que les likes dans le classement X.
        rep_over, _ = self._compare(perf["by_kind"], ov, "avg_replies")
        if rep_over and ov["avg_replies"] > 0:
            r, name, st = rep_over[0]
            lines.append(f"{name.capitalize()} posts get the most replies ({st['avg_replies']:.1f} per post vs "
                         f"{ov['avg_replies']:.1f} average); replies count far more than likes in ranking.")

        extra = []
        f_over, f_under = self._compare(perf["by_format"], ov)
        names = {"video": "posts with a video", "image": "posts with an image", "poll": "polls",
                 "text": "text-only posts"}
        for r, name, st in f_over[:1]:
            extra.append(f"{names.get(name, name)} do {r:.1f}x avg impressions (n={st['count']})")
        for r, name, st in f_under[:1]:
            extra.append(f"{names.get(name, name)} only {r:.1f}x (n={st['count']})")
        notes_st = perf["by_notes"].get("with_notes")
        if notes_st and notes_st["count"] >= MIN_GROUP and ov["engagement_rate"]:
            r = notes_st["engagement_rate"] / ov["engagement_rate"]
            if r >= OVER:
                extra.append(f"posts built on the owner's real notes get {r:.1f}x the engagement rate")
        g_over, _ = self._compare(perf["by_thread_genre"], perf["by_kind"].get("thread") or {})
        for r, name, st in g_over[:1]:
            extra.append(f"{name} threads do {r:.1f}x the average thread")
        if extra:
            line = "; ".join(extra) + "."
            lines.append(line[0].upper() + line[1:])

        # Quelle forme d'accroche marche sur CE compte (le rédacteur note la sienne à chaque post).
        k_over, k_under = self._compare(perf.get("by_hook") or {}, ov)
        hooks = [f"\"{name}\" hooks do {r:.1f}x avg impressions (n={st['count']})" for r, name, st in k_over[:2]]
        hooks += [f"\"{name}\" only {r:.1f}x (n={st['count']})" for r, name, st in k_under[:1]]
        if hooks:
            lines.append("Hook shapes on this account: " + "; ".join(hooks)
                         + ". Lean toward what works, but keep varying.")

        h_over, h_under = self._compare(perf["by_hour"], ov)
        hours = []
        if h_over:
            r, h, _ = h_over[0]
            hours.append(f"best publishing hour {h}:00 local time ({r:.1f}x avg impressions)")
        if h_under:
            r, h, _ = h_under[0]
            hours.append(f"weakest {h}:00 ({r:.1f}x)")
        if hours:
            line = "; ".join(hours) + "."
            lines.append(line[0].upper() + line[1:])

        best = perf.get("best")
        if best and best.get("impressions"):
            lines.append(f"Top post of the last {BEST_DAYS} days ({best['kind']}, {en_int(best['impressions'])} "
                         f"impressions, {best['likes']} likes): \"{best['text']}\"")
        return "\n".join(lines[:7])

    # ==================================================================
    # Récap du matin (français)
    # ==================================================================
    def _metrics_fr(self, p: dict) -> str:
        if not isinstance(p.get("metrics"), dict):
            return "stats pas encore relevées"
        return (f"{fr_int(_m(p, 'impressions'))} vues · {_m(p, 'likes')} ❤️ · {_m(p, 'replies')} 💬"
                + (f" · {_m(p, 'bookmarks')} 🔖" if _m(p, "bookmarks") else ""))

    @staticmethod
    def _status_fr(it: dict | None) -> str:
        if not it:
            return "📝 pas encore rédigé"
        st = it.get("status")
        if st == "approved":
            dt = parse_iso(it.get("publish_at"))
            return f"🕓 validé, part à {dt.astimezone(now_local().tzinfo):%H:%M}" if dt else "🕓 validé"
        return {
            "awaiting": "⏳ attend ta validation",
            "publishing": "📤 publication en cours",
            "published": "✅ publié",
            "rejected": "❌ annulé",
            "expired": "⌛ expiré, rien publié",
            "failed": "⚠️ échec de publication",
        }.get(st, str(st))

    def _llm_day(self, date: str) -> dict:
        days = (self.state.data.get("llm_usage") or {}).get("days") or {}
        d = days.get(date) or {}
        return {"calls": int(d.get("calls") or 0), "usd": float(d.get("usd") or 0.0)}

    def _claude_month_text(self) -> str:
        """« · 28,10 $ ce mois sur 35 $ » (avec les essais faits à la main), et le rappel du
        plafond s'il est atteint ; rien si aucun plafond n'est réglé."""
        try:
            cap = float(getattr(self.settings, "claude_budget_monthly_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            cap = 0.0
        if cap <= 0:
            return ""
        month = now_local().strftime("%Y-%m")
        days = (self.state.data.get("llm_usage") or {}).get("days") or {}
        spent = sum(float((v or {}).get("usd") or 0.0) for k, v in days.items() if str(k).startswith(month))
        try:
            from .llm import manual_spent_month
            spent += manual_spent_month(month)
        except Exception:  # noqa: BLE001
            pass
        text = f" · {spent:.2f} $ ce mois sur {cap:.0f} $"
        if spent >= cap:
            text += " — plafond atteint : plus rien n'est rédigé jusqu'au 1er"
        return text

    def _llm_last_days(self, n: int) -> float:
        today = now_local().date()
        return sum(self._llm_day((today - timedelta(days=i)).strftime("%Y-%m-%d"))["usd"] for i in range(n))

    def _ignored_tip(self, days: int = 3) -> str:
        """Une ligne de conseil si tu laisses passer la plupart des brouillons de
        réponses (commentaires ou radar)."""
        limit = now_local() - timedelta(days=days)
        com_closed = com_ignored = rad_closed = rad_passed = 0
        for it in self.state.data.get("items", {}).values():
            dt = parse_iso(it.get("created_at"))
            if dt is None or dt < limit:
                continue
            st = it.get("status")
            if it.get("kind") == "comment_reply" and st in ("published", "rejected", "expired", "failed"):
                com_closed += 1
                com_ignored += st in ("rejected", "expired")
            elif it.get("kind") == "radar" and st in ("dismissed", "expired"):
                rad_closed += 1
                rad_passed += st == "dismissed"
        com_r = com_ignored / com_closed if com_closed >= IGNORED_TIP_MIN else 0.0
        rad_r = rad_passed / rad_closed if rad_closed >= IGNORED_TIP_MIN else 0.0
        if com_r >= IGNORED_TIP_RATIO and com_r >= rad_r:
            return (f"💡 {com_ignored}/{com_closed} brouillons de réponse aux commentaires sont restés sans suite "
                    "ces 3 derniers jours : baisse COMMENTS_MAX_DRAFTS_PER_DAY ou espace les passages "
                    "(COMMENTS_CHECK_EVERY_MINUTES) pour ne recevoir que l'essentiel.")
        if rad_r >= IGNORED_TIP_RATIO:
            return (f"💡 Tu as passé {rad_passed}/{rad_closed} suggestions radar ces 3 derniers jours : "
                    "baisse RADAR_MAX_PER_DAY, garde moins de comptes dans radar_accounts, ou coupe le radar "
                    "(RADAR_ENABLED=false).")
        return ""

    def daily_report(self) -> str:
        """Récap Telegram du matin (HTML). Ne lève jamais d'exception."""
        try:
            return self._daily_report()
        except Exception as e:  # noqa: BLE001
            log.exception("Stats : récap du jour impossible : %s", e)
            return "📊 Récap du jour indisponible (erreur interne, voir les logs)."

    def _daily_report(self) -> str:
        now = now_local()
        today = now.date()
        yday = today - timedelta(days=1)
        yday_s = yday.strftime("%Y-%m-%d")
        today_s = today.strftime("%Y-%m-%d")
        lines = [f"📊 <b>Récap du {JOURS[now.weekday()]} {now:%d/%m}</b>"]
        if self.state.data.get("paused"):
            lines.append("⏸️ Bot en pause : rien ne sera rédigé ni publié (/reprendre pour relancer).")

        # --- Hier -------------------------------------------------------
        posts_y = sorted((p for p in self.state.data.get("published") or []
                          if p.get("kind") in KINDS and (_local(p) is not None) and _local(p).date() == yday),
                         key=lambda p: p.get("created_at") or "")
        lines.append("")
        lines.append(f"<b>Hier</b> : {len(posts_y)} post(s) publié(s)" if posts_y
                     else "<b>Hier</b> : aucun post publié")
        for p in posts_y:
            dt = _local(p)
            lines.append(f"• {dt:%H:%M} {esc(KIND_FR.get(p.get('kind'), p.get('kind') or '?'))} — "
                         f"{self._metrics_fr(p)}")
            lines.append(f"   « {esc(excerpt(p.get('text') or '', 80))} »")
        drafts_y = self.state.count("comment_drafts", date=yday_s)
        sent_y = sum(1 for it in self.state.data.get("items", {}).values()
                     if it.get("kind") == "comment_reply" and it.get("status") == "published"
                     and (parse_iso(it.get("approved_at") or it.get("publish_at") or it.get("created_at"))
                          or now).astimezone(now.tzinfo).date() == yday)
        radar_y = self.state.count("radar_suggestions", date=yday_s)
        if drafts_y or sent_y or radar_y:
            lines.append(f"💬 Commentaires : {drafts_y} brouillon(s), {sent_y} réponse(s) envoyée(s) · "
                         f"📡 radar : {radar_y} suggestion(s)")

        # --- Aujourd'hui ---------------------------------------------------
        from .scheduler import SLOT_FAILED, SLOT_PENDING, SLOT_SKIPPED, THREAD_CLEAR_MIN

        threads = [s for s in (self.settings.thread_slots or []) if s.weekday == now.weekday()]

        def _minutes(t) -> int:
            return t.hour * 60 + t.minute

        # Comme le planificateur : un jour de thread, pas d'autre post (hors gm) près de lui.
        slots = [s for s in (self.settings.post_slots or [])
                 if s.kind == "gm" or all(abs(_minutes(s.at) - _minutes(t.at)) >= THREAD_CLEAR_MIN for t in threads)]
        slots += threads
        slots.sort(key=lambda s: s.at)
        markers = {SLOT_PENDING: "✍️ rédaction en cours", SLOT_SKIPPED: "⏭️ sauté (rien d'assez solide)",
                   SLOT_FAILED: "⚠️ échec de la rédaction"}
        lines.append("")
        lines.append("<b>Aujourd'hui</b> :" if slots else "<b>Aujourd'hui</b> : aucun créneau prévu")
        for slot in slots:
            item_id = self.state.slot_item_id(today_s, slot.key)
            it = self.state.item(item_id) if item_id and item_id not in markers else None
            status = markers.get(item_id) if item_id in markers else self._status_fr(it)
            lines.append(f"• {slot.at:%H:%M} {esc(KIND_FR.get(slot.kind, slot.kind))} — {status}")
        awaiting = self.state.items(statuses=("awaiting",))
        radar_open = self.state.items(kinds=("radar",), statuses=("suggested",))
        if awaiting:
            lines.append(f"⏳ {len(awaiting)} brouillon(s) attendent ta validation")
        if radar_open:
            lines.append(f"📡 {len(radar_open)} suggestion(s) radar ouvertes")

        # --- Budget --------------------------------------------------------
        lines.append("")
        if self.budget is not None:
            lines.append(f"💸 {esc(self.budget.summary())}")
        llm = self._llm_day(yday_s)
        lines.append(f"🤖 Claude hier : {llm['usd']:.2f} $ ({llm['calls']} appel(s)){self._claude_month_text()}")

        tip = self._ignored_tip()
        if tip:
            lines.append("")
            lines.append(esc(tip))
        return "\n".join(lines)

    # ==================================================================
    # Bilan de la semaine (français)
    # ==================================================================
    def _followers_line(self) -> str:
        hist = [h for h in self.state.data.get("followers") or []
                if isinstance(h, dict) and h.get("date") and h.get("count") is not None]
        if not hist:
            return ""
        hist.sort(key=lambda h: h["date"])
        last = hist[-1]
        if len(hist) < 2:
            return f"👥 Abonnés : {fr_int(last['count'])}"
        # Référence : le dernier relevé d'il y a 7 jours ou plus, sinon le plus ancien.
        ref_date = (now_local().date() - timedelta(days=7)).strftime("%Y-%m-%d")
        older = [h for h in hist[:-1] if h["date"] <= ref_date]
        ref = older[-1] if older else hist[0]
        delta = int(last["count"]) - int(ref["count"])
        return f"👥 Abonnés : {fr_int(last['count'])} ({delta:+d} depuis le {ref['date'][8:10]}/{ref['date'][5:7]})"

    def weekly_report(self) -> str:
        """Bilan Telegram de la semaine (HTML). Ne lève jamais d'exception."""
        try:
            return self._weekly_report()
        except Exception as e:  # noqa: BLE001
            log.exception("Stats : bilan de la semaine impossible : %s", e)
            return "📈 Bilan de la semaine indisponible (erreur interne, voir les logs)."

    def _weekly_report(self) -> str:
        now = now_local()
        start = (now - timedelta(days=6)).date()
        lines = [f"📈 <b>Bilan de la semaine</b> (du {start:%d/%m} au {now:%d/%m})", ""]

        # Semaine calendaire (les 7 jours affichés dans le titre), pas 7 × 24 h.
        week = [p for p in self._posts(8, settled=False, with_metrics=False)
                if _local(p) is not None and _local(p).date() >= start]
        measured = [p for p in week if isinstance(p.get("metrics"), dict)]
        threads = sum(1 for p in week if p.get("kind") == "thread")
        if not week:
            lines.append("Aucun post publié ces 7 derniers jours.")
        else:
            tot = {k: sum(_m(p, k) for p in measured) for k in ("impressions", "likes", "replies", "bookmarks")}
            lines.append(f"📝 {len(week)} post(s) publié(s)" + (f" dont {threads} thread(s)" if threads else ""))
            if measured:
                lines.append(f"👀 {fr_int(tot['impressions'])} vues · {fr_int(tot['likes'])} ❤️ · "
                             f"{fr_int(tot['replies'])} 💬 · {fr_int(tot['bookmarks'])} 🔖")
            if len(measured) < len(week):
                lines.append(f"   ({len(week) - len(measured)} post(s) pas encore mesuré(s))")

        today = now.date()
        last7 = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
        drafts = sum(self.state.count("comment_drafts", date=d) for d in last7)
        radar = sum(self.state.count("radar_suggestions", date=d) for d in last7)
        if drafts or radar:
            lines.append(f"💬 {drafts} brouillon(s) de réponse aux commentaires · 📡 {radar} suggestion(s) radar")

        best = max(measured, key=lambda p: _m(p, "impressions"), default=None)
        if best is not None and _m(best, "impressions"):
            lines.append("")
            lines.append(f"🏆 <b>Meilleur post</b> ({esc(KIND_FR.get(best.get('kind'), best.get('kind') or '?'))}) : "
                         f"{fr_int(_m(best, 'impressions'))} vues, {_m(best, 'likes')} ❤️, "
                         f"{_m(best, 'replies')} 💬")
            lines.append(f"« {esc(excerpt(best.get('text') or '', 140))} »")

        # Tableau par type (moyennes de la semaine)
        groups: dict[str, list] = {}
        for p in measured:
            groups.setdefault(p["kind"], []).append(p)
        if groups:
            rows = [f"{'type':<9}{'n':>3}{'vues':>8}{'likes':>7}{'rép.':>6}{'sign.':>7}{'eng.':>7}"]
            for kind in KINDS:
                if kind not in groups:
                    continue
                st = stats(groups[kind])
                rows.append(f"{KIND_FR[kind]:<9}{st['count']:>3}{fr_int(st['avg_impressions']):>8}"
                            f"{st['avg_likes']:>7.1f}{st['avg_replies']:>6.1f}{st['avg_bookmarks']:>7.1f}"
                            f"{st['engagement_rate'] * 100:>6.1f}%")
            lines.append("")
            lines.append("<b>Par type</b> (moyennes par post) :")
            lines.append("<pre>" + esc("\n".join(rows)) + "</pre>")

        # Meilleures heures (fenêtre longue : 7 jours, c'est trop peu par heure)
        perf = self.compute()
        ref = perf["overall"]["avg_impressions"]
        hours = [(st["avg_impressions"], h, st) for h, st in perf["by_hour"].items()
                 if st["count"] >= MIN_GROUP and st["avg_impressions"] > ref]
        hours.sort(reverse=True)
        if hours and len(perf["by_hour"]) >= 2:
            top = ", ".join(f"{h}h ({fr_int(avg)} vues en moy., {st['count']} posts)" for avg, h, st in hours[:2])
            lines.append(f"⏰ Meilleures heures ({AGG_DAYS} j, moyenne {fr_int(ref)} vues) : {top}")

        lines.append("")
        x_spent = self.budget.spent_last_days(7) if self.budget is not None else 0.0
        lines.append(f"💸 Dépenses 7 j : API X {x_spent:.2f} $ · Claude {self._llm_last_days(7):.2f} $")
        fl = self._followers_line()
        if fl:
            lines.append(fl)

        lines.append("")
        lines.append("✍️ <b>À toi</b> : envoie-moi 1 ou 2 choses VRAIES que tu as testées ou apprises cette "
                     "semaine avec /note (un outil essayé, un résultat, une erreur). Du vécu précis, c'est ce "
                     "qui rend tes posts uniques et t'évite l'étiquette « contenu IA générique » de X.")
        return "\n".join(lines)
