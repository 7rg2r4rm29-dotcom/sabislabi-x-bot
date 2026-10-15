"""Statistiques de mes posts, notes de performance pour Claude, récap du matin
et bilan de la semaine.

- `refresh_metrics()` : relit mes posts récents (« owned read », 0,001 $ le
  post) et range leurs statistiques publiques dans `state.published[*].metrics`,
  puis recalcule les agrégats dans `state.data["performance"]`.
  `maybe_refresh()` ne le fait qu'une fois toutes les ~3 h.
- `performance_notes()` : 3 à 6 lignes EN ANGLAIS pour Claude (ce qui marche
  sur CE compte), vide tant qu'il n'y a pas assez de posts mesurés.
- `daily_report()` / `weekly_report()` : textes Telegram en français (HTML).

Seuls les posts publiés depuis plus de 24 h comptent dans les agrégats : avant,
les chiffres bougent encore beaucoup (cold start ~2 h, puis diffusion).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from .telegram import esc
from .utils import iso, now_local, parse_iso, today_str
from .x_api import XError

log = logging.getLogger("xbot.analytics")

KINDS = ("gm", "tip", "news", "thread", "reactive")
KIND_FR = {"gm": "gm", "tip": "astuce", "news": "actu", "thread": "thread", "reactive": "réactif",
           "comment_reply": "réponse"}
JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")

REFRESH_EVERY_MIN = 170
SETTLE_HOURS = 24          # un post compte dans les agrégats après 24 h
AGG_DAYS = 30              # fenêtre des agrégats par type / heure / format
BEST_DAYS = 14             # fenêtre du meilleur / pire post
MIN_POSTS_FOR_NOTES = 5
MIN_GROUP = 2              # un groupe (type, heure…) doit avoir ≥ 2 posts pour être comparé
OVER, UNDER = 1.25, 0.8    # seuils « surperforme » / « sous-performe »
IGNORED_TIP_MIN = 5        # nb mini de brouillons clos pour juger
IGNORED_TIP_RATIO = 0.6

METRIC_KEYS = ("impressions", "likes", "replies", "reposts", "quotes", "bookmarks")


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
        media = "poll" if it.get("poll") else ("image" if it.get("image_path") else "text")
        p["fmt"] = {
            "media": media,
            "image_kind": it.get("image_kind") or "",
            "genre": str(meta.get("angle") or "") if p.get("kind") == "thread" else "",
            "notes": bool(meta.get("used_notes")),
            "fallback": bool(meta.get("from_fallback")),
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
            "best": self._brief(best),
            "worst": self._brief(worst) if worst is not best else None,
        }

    def recompute(self) -> dict:
        perf = self.compute()
        self.state.data["performance"] = perf
        self.state.flush()
        return perf

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
        """≤ 6 lignes en anglais pour les consignes de Claude ; "" si moins de
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
        names = {"image": "posts with an image", "poll": "polls", "text": "text-only posts"}
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
        return "\n".join(lines[:6])

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
        slots = list(self.settings.post_slots or []) + [
            s for s in (self.settings.thread_slots or []) if s.weekday == now.weekday()]
        slots.sort(key=lambda s: s.at)
        lines.append("")
        lines.append("<b>Aujourd'hui</b> :" if slots else "<b>Aujourd'hui</b> : aucun créneau prévu")
        for slot in slots:
            item_id = self.state.slot_item_id(today_s, slot.key)
            it = self.state.item(item_id) if item_id else None
            lines.append(f"• {slot.at:%H:%M} {esc(KIND_FR.get(slot.kind, slot.kind))} — {self._status_fr(it)}")
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
        lines.append(f"🤖 Claude hier : {llm['usd']:.2f} $ ({llm['calls']} appel(s))")

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
