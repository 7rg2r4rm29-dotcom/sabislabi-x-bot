"""Assemble tous les modules et fait tourner la boucle principale.

Un tour (`tick`) toutes les ~20 s, chaque étape isolée (une panne n'arrête
jamais la boucle) :
  1. Telegram (boutons, modifications, commandes) puis publication des items
     validés dont l'heure est venue, puis expiration des brouillons oubliés ;
  2. hors pause : rédaction des créneaux (un seul par tour, c'est lent),
     post réactif (rare), réponses à mes commentaires, radar ;
  3. statistiques (~toutes les 3 h), récap du matin, bilan de la semaine,
     ménage quotidien de l'état ;
  4. de nouveau Telegram + publication : un ✅ tapé pendant une étape longue
     (rédaction Claude, photo de l'actu) part sans attendre le tour suivant.

Rien n'est publié sans ta validation sur Telegram (sauf APPROVAL_MODE=auto,
verrouillé par la config) ; le radar ne publie jamais rien.
"""
from __future__ import annotations

import dataclasses
import logging
import random
import signal
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from . import utils
from .config import Slot
from .costs import Budget
from .state import StateStore
from .telegram import Telegram, esc
from .utils import at_local, in_window, iso, minutes_between, now_local, parse_iso, today_str

log = logging.getLogger("xbot.scheduler")

TICK_SLEEP_S = 20
CLAUDE_ALERT_EVERY_MIN = 720   # alerte « crédit Claude » au plus toutes les 12 h
THREAD_CLEAR_MIN = 180  # un jour de thread, pas d'autre post (hors gm) à moins de 3 h de lui
EXPLAINER_CLEAR_MIN = 60  # une vidéo explicative prend la place du post prévu à moins d'1 h d'elle
TICK_BUDGET_S = 240     # au-delà, les étapes facultatives attendent le tour suivant
ERROR_SLEEP_S = 60
WATCHDOG_S = 420
ANALYTICS_EVERY_MIN = 180
GC_EVERY_MIN = 24 * 60
REACTIVE_EXPIRES_MIN = 180
REACTIVE_MAX_CHECKS_PER_DAY = 6    # appels à Claude pour l'actu chaude, au plus par jour (défaut du réglage)
STYLE_TEST_SHARE = 0.25            # posts prévus qui essaient autre chose que ce qui marche (1 sur 4)
FORMAT_PLAN_KEEP_DAYS = 12         # plans de formats gardés (le créneau d'hier se relit après minuit)
FORMAT_DECIDE_MARGIN_MIN = 5       # le plan d'un jour se décide au moins 5 min avant sa première rédaction
BREAKING_MIN_ENGAGEMENT = 150      # likes sur X ou points Hacker News pour qu'une actu non officielle compte
WEEKLY_RECAP_WEEKDAYS = (4, 5, 6)  # comme writer.WEEKLY_RECAP_WEEKDAYS
DAILY_REPORT_LATE_MAX_H = 12      # récap du matin envoyé au plus tard 12 h après l'heure prévue
WEEKLY_REPORT_LATE_MAX_H = 24
STARTUP_NOTICE_EVERY_MIN = 360    # pas de rafale de « démarré » si systemd relance en boucle
FUTURE_NEWS_HOURS = 96            # actus lues pour « le futur arrive » et les threads
NEWS_PER_POST = 120               # titres proposés à la rédaction en chef (writer.MAX_ITEMS_PICK)
NEWS_PER_THREAD = 120

# Marqueurs de créneau (state.slots_done) autres qu'un id d'item.
SLOT_PENDING = "pending"          # rédaction en cours (posé AVANT d'appeler Claude : jamais de doublon)
SLOT_SKIPPED = "skipped"          # thread sans candidat solide : créneau sauté
SLOT_FAILED = "failed"            # erreur inattendue pendant la rédaction
SLOT_MARKERS = (SLOT_PENDING, SLOT_SKIPPED, SLOT_FAILED)

POST_KINDS = ("gm", "tip", "news")
KIND_FR = {"gm": "☀️ GM", "tip": "🔮 le futur arrive", "news": "📰 l'histoire du jour", "thread": "🧵 thread",
           "reactive": "⚡ ça vient de tomber", "explainer": "🎬 vidéo explicative", "comment_reply": "💬 réponse",
           "radar": "🎯 radar"}
JOURS_COURTS = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")
JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
ITEM_STATUS_FR = {
    "awaiting": "⏳ attend ta validation",
    "approved": "✅ programmé",
    "publishing": "📤 publication en cours",
    "published": "📤 publié",
    "rejected": "❌ annulé",
    "expired": "⌛ expiré (rien publié)",
    "failed": "⚠️ échec de publication",
    "suggested": "🎯 suggéré",
    "dismissed": "❌ écarté",
}


class NullTelegram:
    """Telegram non configuré : mêmes méthodes que `Telegram`, rien n'est
    envoyé, tout est loggé (utile en APPROVAL_MODE=auto ou pour un essai)."""

    chat_id = ""

    def send(self, text: str, buttons=None, reply_to=None, force_reply=False, silent=False) -> int | None:
        log.info("[Telegram absent] %s", (text or "")[:300].replace("\n", " | "))
        return None

    def send_photo(self, path: str, caption: str = "") -> int | None:
        log.info("[Telegram absent] photo %s", path)
        return None

    def send_video(self, path: str, caption: str = "") -> int | None:
        log.info("[Telegram absent] vidéo %s", path)
        return None

    def edit(self, message_id: int, text: str, buttons=None) -> bool:
        return False

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        return None

    def updates(self, offset) -> list[dict]:
        return []

    def is_owner_chat(self, chat_id) -> bool:
        return False


def _hm(dt: datetime) -> str:
    return dt.astimezone(utils.tz()).strftime("%H:%M")


def _day_label(dt: datetime, now: datetime) -> str:
    d = dt.astimezone(utils.tz()).date()
    if d == now.date():
        return "aujourd'hui"
    if d == (now + timedelta(days=1)).date():
        return "demain"
    if d == (now - timedelta(days=1)).date():
        return "hier"
    return f"{JOURS_COURTS[d.weekday()]} {d.day:02d}/{d.month:02d}"


def _excerpt(text: str, n: int = 60) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


class Bot:
    def __init__(self, settings, *, llm=None, x=None, telegram=None, news=None, state=None,
                 writer=None, approvals=None, publisher=None, comments=None, radar=None,
                 analytics=None, budget=None, prepare_visual: Callable | None = None,
                 rng: random.Random | None = None, trends=None,
                 prepare_explainer: Callable | None = None, strategist=None) -> None:
        # Les modules lourds sont importés à la demande : un test (ou --busy-check)
        # qui fournit ses doublures n'a pas besoin de les charger.
        self.settings = settings
        self.state = state if state is not None else StateStore()
        self.budget = budget if budget is not None else Budget(
            self.state, settings.x_budget_daily_usd, settings.x_budget_monthly_usd)
        if llm is None:
            from .llm import LLM
            llm = LLM(settings, self.state)
        self.llm = llm
        if x is None:
            from .x_api import XClient
            x = XClient(settings, self.budget)
        self.x = x
        if telegram is None:
            if settings.telegram_ok():
                telegram = Telegram(settings.telegram_token, settings.telegram_chat_id)
            else:
                log.warning("Telegram non configuré : les cartes et messages ne seront que loggés")
                telegram = NullTelegram()
        self.telegram = telegram
        if news is None:
            from .sources import NewsHub
            news = NewsHub(settings, self.state, self.x)
        self.news = news
        if writer is None:
            from .writer import Writer
            writer = Writer(settings, self.llm, self.state)
        self.writer = writer
        if approvals is None:
            from .approvals import Approvals
            approvals = Approvals(settings, self.state, self.telegram, hooks=self.hooks())
        self.approvals = approvals
        if publisher is None:
            from .publisher import Publisher
            publisher = Publisher(settings, self.state, self.x, self.approvals)
        self.publisher = publisher
        if comments is None:
            from .comments import CommentDesk
            comments = CommentDesk(settings, self.state, self.x, self.writer, self.approvals)
        self.comments = comments
        if radar is None:
            from .radar import Radar
            radar = Radar(settings, self.state, self.x, self.writer, self.approvals)
        self.radar = radar
        if analytics is None:
            from .analytics import Analytics
            analytics = Analytics(settings, self.state, self.x, self.budget)
        self.analytics = analytics
        if trends is None and getattr(settings, "trends_enabled", True):
            from .trends import TrendWatch
            trends = TrendWatch(settings, self.state, x=self.x)      # x : la veille du marché
        self.trends = trends
        # Le nombre de posts par jour (bot/limits.py) : ceux des réglages tant que l'agent d'analyse
        # n'en a pas décidé autrement, dans ses bornes et dans le budget.
        from .limits import PostLimits
        self.limits = PostLimits(settings, self.state, self.llm, self.budget)
        # L'agent d'analyse (bot/strategy.py) : chiffres, tendances et algorithme de X → consignes.
        if strategist is None and getattr(settings, "strategy_enabled", False):
            from .algo import AlgoWatch
            from .strategy import Strategist
            strategist = Strategist(settings, self.state, self.analytics, self.writer, trends=self.trends,
                                    algo=AlgoWatch(settings, self.state), limits=self.limits)
        self.strategist = strategist
        self._prepare_visual = prepare_visual
        self._prepare_explainer = prepare_explainer
        self.rng = rng or random.Random()
        # À part : tirer « post d'essai ou non » ne décale pas les autres tirages (self.rng).
        self.style_rng = random.Random()
        self._stop = False
        self._ticks = 0
        self._optional_pause_logged = False
        self._recover_pending_slots()

    # ==================================================================
    # Crochets donnés à Approvals (🔄 Autre version, /statut, /plan, /budget)
    # ==================================================================
    def hooks(self) -> dict[str, Callable]:
        return {
            "regenerate": self.regenerate,
            "regen_comment": self.regen_comment,
            "regen_radar": self.regen_radar,
            "status": self.status_text,
            "plan": self.plan_text,
            "budget": self.budget_text,
        }

    def regenerate(self, item: dict):
        """Nouvelle version d'un post / thread / réactif, avec l'actu du moment
        (sans nouvelle lecture payante de l'API X : posts X en cache seulement)."""
        kind = str(item.get("kind") or "")
        if kind not in POST_KINDS + ("thread", "reactive", "explainer"):
            return None
        news = self._news(kind, use_x=False)
        if kind == "reactive":       # comme maybe_reactive : jamais les annonces pour initiés
            news = [it for it in news if self._appeal(it) >= 1.0]
        # Le Writer ne doit pas refuser la nouvelle version parce qu'elle ressemble…
        # à la version qu'elle remplace.
        self.writer.regen_item_id = item.get("id")
        try:
            perf, _test = self._context(kind, test_ok=False)
            draft = self._write(kind, news, self._notes(), perf)
        finally:
            self.writer.regen_item_id = None
        if draft is not None and getattr(draft, "texts", None):
            self._mark_trend(draft)
            # L'image va avec le texte : nouvelle version, nouvelle photo.
            path, image_kind, second = self._visual(draft,
                                                    f"{kind}_{today_str()}_v{int(item.get('regen_count') or 0)}")
            try:
                draft.image_path, draft.image_kind, draft.image_path2 = path, image_kind, second
            except AttributeError:
                pass
        return draft

    def regen_comment(self, item: dict) -> str | None:
        meta = item.get("meta") or {}
        return self.writer.draft_comment_reply(str(meta.get("my_post_text") or ""),
                                               str(meta.get("commenter") or ""),
                                               str(meta.get("comment_text") or ""))

    def regen_radar(self, item: dict) -> tuple[str, str] | None:
        meta = item.get("meta") or {}
        return self.writer.draft_radar_reply(str(meta.get("author") or ""), str(meta.get("post_text") or ""),
                                             meta.get("age_min"), meta.get("likes"))

    # ==================================================================
    # Boucle
    # ==================================================================
    def _step(self, name: str, fn: Callable, *args: Any) -> Any:
        if self._stop:
            return None
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001 — une étape en panne n'arrête jamais la boucle
            log.exception("Étape « %s » : erreur inattendue : %s", name, e)
            return None

    def tick(self) -> dict[str, Any]:
        """Un tour de boucle. Retourne le résultat de chaque étape (pour les logs et les tests)."""
        started = time.monotonic()
        self._ticks += 1
        res: dict[str, Any] = {}
        # Expiration d'abord : un ✅ en attente depuis un arrêt du bot ne doit pas
        # faire partir un post dont le délai est passé.
        res["expire"] = self._step("expiration", self.approvals.expire_due)
        res["claude_alert"] = self._step("alerte Claude", self.maybe_alert_claude)
        res["claude_budget"] = self._step("budget Claude", self.maybe_warn_claude_budget)
        res["poll"] = self._step("Telegram", self.approvals.poll)
        res["publish"] = self._step("publication", self.publisher.publish_due)
        paused = bool(self.state.data.get("paused"))
        if not paused:
            res["plan"] = self._step("créneaux", self.plan_slots)
            # Entre deux étapes lentes (Claude), on traite les boutons et ce qui doit
            # partir : un ✅ ou un post à l'heure n'attend jamais la fin du tour.
            res["publish_mid"] = self._between()
            optional = (("post réactif", "reactive", self.maybe_reactive),
                        ("commentaires", "comments", self.comments.check),
                        ("radar", "radar", self.radar.check))
            # Budget Claude du mois : ce qui est optionnel s'arrête en premier, pour garder de
            # quoi écrire les posts planifiés jusqu'à la fin du mois.
            optional_ok = self._claude_optional_ok()
            for label, key, fn in optional:
                if not optional_ok:
                    continue
                if key == "reactive" and res["plan"]:
                    continue  # une seule rédaction (lente) par tour
                if time.monotonic() - started > TICK_BUDGET_S:
                    log.info("Tour long (%.0f s) : %s reporté au tour suivant", time.monotonic() - started, label)
                    continue
                res[key] = self._step(label, fn)
                res["publish_mid"] += self._between()
            # L'analyse (tous les trois jours) passe en dernier, jamais dans un tour déjà long.
            if optional_ok and not res["plan"] and time.monotonic() - started <= TICK_BUDGET_S:
                res["strategy"] = self._step("analyse", self.maybe_strategy)
        res["metrics"] = self._step("statistiques", self.maybe_analytics)
        res["daily"] = self._step("récap du matin", self.maybe_daily_report)
        res["weekly"] = self._step("bilan de la semaine", self.maybe_weekly_report)
        res["gc"] = self._step("ménage", self.maybe_gc)
        res["poll2"] = self._step("Telegram", self.approvals.poll)
        res["publish2"] = self._step("publication", self.publisher.publish_due)
        self._heartbeat(paused, time.monotonic() - started, res)
        return res

    def _claude_budget_problem(self) -> str | None:
        fn = getattr(self.llm, "budget_problem", None)
        try:
            return (fn() if callable(fn) else None) or None
        except Exception:  # noqa: BLE001
            return None

    def _claude_optional_ok(self) -> bool:
        """Reste-t-il assez de budget Claude ce mois-ci pour le radar, les posts « à chaud »
        et les réponses aux commentaires, sans entamer la part des posts planifiés ?"""
        fn = getattr(self.llm, "optional_ok", None)
        try:
            ok = bool(fn()) if callable(fn) else True
        except Exception:  # noqa: BLE001
            ok = True
        if not ok and not self._optional_pause_logged:
            log.info("Budget Claude : radar, posts à chaud et réponses aux commentaires en pause "
                     "(la suite du mois est réservée aux posts planifiés)")
        self._optional_pause_logged = not ok
        return ok

    def _budget_mark(self, key: str) -> bool:
        """True la première fois du mois pour `key` (et le note)."""
        marks = self.state.data.setdefault("claude_budget", {})
        month = now_local().strftime("%Y-%m")
        if marks.get(key) == month:
            return False
        marks[key] = month
        self.state.flush()
        return True

    def maybe_alert_claude(self) -> bool:
        """Crédit Claude épuisé ou clé refusée : un message Telegram (au plus toutes
        les 12 h). En attendant, aucun créneau n'est sauté : ils seront rédigés dès que
        Claude répondra de nouveau (s'il n'est pas trop tard).
        Plafond du mois atteint : un seul message dans le mois (le récap du matin le rappelle)."""
        capped = self._claude_budget_problem()
        if capped:
            if not self._budget_mark("reached"):
                return False
            self.approvals.notify(f"💳 Claude ne peut plus écrire : {esc(capped)}.\n"
                                  "Plus rien n'est rédigé ni publié d'ici là. Pour continuer avant, il faut "
                                  "augmenter le plafond.")
            return True
        problem = getattr(self.llm, "account_problem", None)
        if not problem:
            return False
        since = self.state.minutes_since("claude_alert")
        if since is not None and since < CLAUDE_ALERT_EVERY_MIN:
            return False
        self.state.set_last_run("claude_alert")
        self.approvals.notify(f"💳 Claude ne peut plus écrire : {esc(problem)}.\n"
                              "Rien n'est publié en attendant ; tout reprend tout seul dès que c'est réglé.")
        return True

    def maybe_warn_claude_budget(self) -> bool:
        """À 80 % du plafond Claude du mois : un message Telegram, une seule fois dans le mois."""
        due = getattr(self.llm, "budget_warning_due", None)
        try:
            if not (callable(due) and due()) or self._claude_budget_problem():
                return False
            spent, cap = float(self.llm.spent_month()), float(self.llm.monthly_budget)
        except Exception:  # noqa: BLE001
            return False
        if not self._budget_mark("warned"):
            return False
        self.approvals.notify(
            f"⚠️ Claude : {spent:.2f} $ dépensés sur {cap:.0f} $ ce mois-ci. Le radar, les posts « à chaud » et "
            "les réponses aux commentaires se mettent en pause en premier ; les posts prévus continuent "
            "jusqu'au plafond, puis tout reprend le 1er du mois.", silent=True)
        return True

    def _between(self) -> int:
        self._step("Telegram", self.approvals.poll)
        return int(self._step("publication", self.publisher.publish_due) or 0)

    def _heartbeat(self, paused: bool, duration: float, res: dict) -> None:
        try:
            awaiting = len(self.state.items(statuses=("awaiting",)))
            approved = len(self.state.items(statuses=("approved",)))
            done = [k for k in ("plan", "reactive", "comments", "radar") if res.get(k)]
            published = (int(res.get("publish") or 0) + int(res.get("publish2") or 0)
                         + int(res.get("publish_mid") or 0))
            log.info("Tour %d (%.0f s)%s : %d à valider, %d programmé(s)%s%s", self._ticks, duration,
                     " EN PAUSE" if paused else "", awaiting, approved,
                     f", {published} publié(s)" if published else "",
                     f", nouveau : {', '.join(done)}" if done else "")
        except Exception:  # noqa: BLE001
            log.info("Tour %d (%.0f s)", self._ticks, duration)

    def run_forever(self) -> None:
        utils.start_watchdog(WATCHDOG_S)
        self._install_sigterm()
        s = self.settings
        log.info("Bot v2 démarré : @%s, %s, %s, %d créneau(x)/jour + %d thread(s)/semaine",
                 s.handle, "validation Telegram" if s.approval_mode == "telegram" else "MODE AUTOMATIQUE",
                 "SIMULATION (DRY_RUN)" if s.dry_run else "publication réelle",
                 len(s.post_slots), len(s.thread_slots))
        self._step("message de démarrage", self._startup_notice)
        while not self._stop:
            try:
                try:
                    self.tick()
                    utils.touch_watchdog()
                    pause = TICK_SLEEP_S
                except Exception as e:  # noqa: BLE001
                    log.exception("Erreur inattendue dans la boucle : %s", e)
                    pause = ERROR_SLEEP_S
                self._sleep(pause)
            except KeyboardInterrupt:
                log.info("Arrêt demandé (Ctrl+C)")
                break
        log.info("Bot arrêté proprement")

    def stop(self) -> None:
        self._stop = True

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._stop:
            left = end - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(1.0, left))

    def _install_sigterm(self) -> None:
        def handler(signum, frame):  # noqa: ARG001
            log.info("Arrêt demandé par le système (SIGTERM) : fin de l'étape en cours puis arrêt")
            self._stop = True
        try:
            signal.signal(signal.SIGTERM, handler)
        except (ValueError, OSError):  # pas dans le thread principal
            pass

    def _startup_notice(self) -> None:
        since = self.state.minutes_since("startup_notice")
        if since is not None and since < STARTUP_NOTICE_EVERY_MIN:
            return
        self.state.set_last_run("startup_notice")
        s = self.settings
        bits = ["🤖 Bot v2 démarré"]
        if s.dry_run:
            bits.append("🧪 simulation (DRY_RUN=true) : rien ne sera vraiment publié")
        if self.state.data.get("paused"):
            bits.append("⏸️ en pause : /reprendre pour relancer")
        for w in (getattr(s, "warnings", None) or [])[:8]:
            bits.append(f"⚠️ Réglage ignoré : {esc(w)}")
        bits.append("/aide pour les commandes")
        self.approvals.notify("\n".join(bits), silent=True)

    # ==================================================================
    # Créneaux
    # ==================================================================
    def _occurrences(self, day_offsets: tuple[int, ...] = (-1, 0, 1)) -> list[tuple[datetime, Slot]]:
        """Occurrences des créneaux (posts tous les jours, threads le bon jour)
        hier, aujourd'hui, demain — hier pour un créneau tardif dont le délai
        de validation passe minuit."""
        now = now_local()
        out: list[tuple[datetime, Slot]] = []
        for off in day_offsets:
            day = now + timedelta(days=off)
            plan = self._format_plan(day)
            if plan:                                  # FORMAT_AUTO : le format du jour suit les résultats
                out += self._auto_day(day, plan)
                continue
            threads = [at_local(day, sl.at) for sl in self.settings.thread_slots or []
                       if sl.weekday == day.weekday()]
            # Vidéo explicative : elle prend la place du post prévu le plus proche (même nombre de
            # posts par jour) ; un jour de thread, le thread passe avant elle.
            explainers = [(at_local(day, sl.at), sl) for sl in getattr(self.settings, "explainer_slots", None) or []
                          if sl.weekday == day.weekday()]
            explainers = [(occ, sl) for occ, sl in explainers
                          if not any(abs(minutes_between(occ, t)) < THREAD_CLEAR_MIN for t in threads)]
            for slot in self.limits.post_slots(day):
                occ = at_local(day, slot.at)
                # Jour de thread : le thread prend la place du post le plus proche (pas
                # plus de 3 posts par jour, et jamais deux posts à moins de 3 h).
                if slot.kind != "gm" and any(abs(minutes_between(occ, t)) < THREAD_CLEAR_MIN for t in threads):
                    continue
                if slot.kind != "gm" and any(abs(minutes_between(occ, e)) < EXPLAINER_CLEAR_MIN
                                             for e, _sl in explainers):
                    continue
                out.append((occ, slot))
            for slot in self.settings.thread_slots or []:
                if slot.weekday == day.weekday():
                    out.append((at_local(day, slot.at), slot))
            out += explainers
        out.sort(key=lambda p: p[0])
        return out

    # ---- Le format suit les résultats (FORMAT_AUTO) ---------------------
    def _feature_slot(self) -> Slot | None:
        """Le grand créneau du jour : celui du post « le futur arrive »."""
        return next((sl for sl in self.settings.post_slots or [] if sl.kind == "tip"), None)

    def _second_slot(self, feature: Slot, day: datetime | None = None) -> Slot | None:
        """Le post d'actu qu'un format prouvé peut remplacer : le premier après le grand créneau,
        assez loin de tous les autres posts pour qu'un thread à sa place n'en annule aucun."""
        def minute(sl: Slot) -> int:
            return sl.at.hour * 60 + sl.at.minute

        posts = [sl for sl in self.limits.post_slots(day) if sl.kind != "gm"]
        for sl in sorted(posts, key=minute):
            if (sl.kind == "news" and minute(sl) > minute(feature)
                    and all(abs(minute(sl) - minute(o)) >= THREAD_CLEAR_MIN for o in posts if o is not sl)):
                return sl
        return None

    def _format_candidates(self) -> list[str]:
        """Les formats permis par les réglages : ceux qui ont au moins un créneau fixe."""
        out: list[str] = []
        if getattr(self.settings, "explainer_slots", None):
            out.append("explainer")
        if self.settings.thread_slots:
            out.append("thread")
        if self._feature_slot() is not None:
            out.append("tip")
        return out

    def _format_plan(self, day: datetime) -> dict | None:
        """Le plan des formats d'un jour quand FORMAT_AUTO est allumé (consigne du propriétaire,
        24/11/2026 : « choisis la meilleure option à chaque fois ») : {"feature": le format du
        grand créneau, "second": celui qui remplace un post d'actu ou None, les raisons}.
        Décidé une seule fois par jour et noté dans l'état : jamais deux formats pour un même
        créneau, même après un redémarrage ou un nouveau relevé des chiffres. None : le planning
        fixe s'applique (réglage coupé, pas assez de chiffres, jour passé sans plan, jour trop
        lointain, ou décision trop tardive pour la journée en cours)."""
        if not getattr(self.settings, "format_auto", False):
            return None
        feature = self._feature_slot()
        if feature is None or not callable(getattr(self.analytics, "choose_format", None)):
            return None
        date = day.strftime("%Y-%m-%d")
        plans = self.state.data.get("format_plan")
        if not isinstance(plans, dict):
            plans = self.state.data["format_plan"] = {}
        plan = plans.get(date)
        if isinstance(plan, dict):
            return plan if plan.get("feature") else None
        now = now_local()
        today = now.strftime("%Y-%m-%d")
        if date < today or date > (now + timedelta(days=1)).strftime("%Y-%m-%d"):
            return None
        try:
            plan = self._decide_formats(day, date, feature, plans, now, today)
        except Exception as e:  # noqa: BLE001
            log.exception("Formats du %s : choix impossible (%s), planning fixe", date, e)
            plan = {"static": True, "why": "erreur pendant le choix"}
        plans[date] = plan
        cutoff = (now - timedelta(days=FORMAT_PLAN_KEEP_DAYS)).strftime("%Y-%m-%d")
        for old in [d for d in plans if d < cutoff]:
            del plans[old]
        self.state.flush()
        return plan if plan.get("feature") else None

    def _decide_formats(self, day: datetime, date: str, feature: Slot, plans: dict, now: datetime,
                        today: str) -> dict:
        cands = self._format_candidates()
        if date == today:
            # Trop tard pour changer la journée en cours : la rédaction du grand créneau a pu commencer.
            lead = max((self.settings.lead_for(k) for k in cands), default=0)
            if now >= at_local(day, feature.at) - timedelta(minutes=lead + FORMAT_DECIDE_MARGIN_MIN):
                return {"static": True, "why": "décidé trop tard pour ce jour"}
        pick = self.analytics.choose_format(cands, random.Random(f"format:{date}"),
                                            self._pending_formats(plans, today, date))
        if not pick:
            return {"static": True, "why": "pas assez de chiffres"}
        kind, why = pick
        plan = {"feature": kind, "why": str(why), "second": None, "why2": "", "decided_at": iso(now)}
        second = getattr(self.analytics, "second_format", None)
        if callable(second) and self._second_slot(feature, day) is not None:
            extra = second(kind, [k for k in cands if k != "tip"])
            if extra:
                plan["second"], plan["why2"] = extra[0], str(extra[1])
        log.info("Formats du %s : %s au grand créneau (%s)%s", date, KIND_FR.get(kind, kind), why,
                 f" ; {KIND_FR.get(plan['second'], plan['second'])} à la place d'un post d'actu ({plan['why2']})"
                 if plan["second"] else "")
        return plan

    def _pending_formats(self, plans: dict, today: str, skip: str) -> dict[str, int]:
        """Les formats déjà prévus (aujourd'hui ou après) et pas encore publiés : ils comptent
        comme des essais, pour qu'un format nouveau ne soit pas programmé cinq jours de suite
        avant même sa première publication."""
        published = {(str(p.get("created_at") or "")[:10], p.get("kind"))
                     for p in self.state.data.get("published") or []}
        pending: dict[str, int] = {}
        for d, plan in plans.items():
            if d < today or d == skip or not isinstance(plan, dict):
                continue
            for k in (plan.get("feature"), plan.get("second")):
                if k and (d, k) not in published:
                    pending[k] = pending.get(k, 0) + 1
        return pending

    def _auto_day(self, day: datetime, plan: dict) -> list[tuple[datetime, Slot]]:
        """Les créneaux d'un jour dont le plan des formats est décidé : le même nombre de posts
        qu'avec le planning fixe, seul le format de deux d'entre eux au plus change."""
        feature = self._feature_slot()
        second = self._second_slot(feature, day) if feature is not None and plan.get("second") else None
        chosen: list[tuple[datetime, Slot]] = []
        posts: list[tuple[datetime, Slot]] = []
        for slot in self.limits.post_slots(day):
            occ = at_local(day, slot.at)
            if slot == feature and plan.get("feature") != "tip":
                chosen.append((occ, Slot(plan["feature"], slot.at, day.weekday())))
            elif second is not None and slot == second:
                chosen.append((occ, Slot(plan["second"], slot.at, day.weekday())))
            else:
                posts.append((occ, slot))

        def clear(occ: datetime, slot: Slot) -> bool:
            # Comme avec le planning fixe : aucun autre post à moins de 3 h d'un thread, ni à moins
            # d'1 h d'une vidéo explicative.
            return slot.kind == "gm" or not any(
                abs(minutes_between(occ, o)) < (THREAD_CLEAR_MIN if s.kind == "thread" else EXPLAINER_CLEAR_MIN)
                for o, s in chosen)

        return [p for p in posts if clear(*p)] + chosen

    def _fallback_kind(self, slot: Slot, occ: datetime) -> str | None:
        """FORMAT_AUTO : le post qui prend la place d'une vidéo ou d'un thread quand rien n'est
        assez solide pour ce format (« essaye de toujours rester dans le top » : un créneau
        n'est plus perdu pour une question de format). None avec le planning fixe."""
        if slot.kind not in ("thread", "explainer") or not self._format_plan(occ):
            return None
        feature = self._feature_slot()
        return "tip" if feature is not None and slot.at == feature.at else "news"

    def _media_settings(self, draft):
        """FORMAT_AUTO : quand les chiffres disent nettement que la photo de l'actu n'apporte rien,
        le post part sans elle (l'extrait vidéo de l'actu reste). Les posts d'essai gardent la
        photo, pour que la comparaison continue."""
        s = self.settings
        advice = getattr(self.analytics, "photo_advice", None)
        if (not getattr(s, "format_auto", False) or getattr(draft, "style_test", False)
                or not callable(advice) or not dataclasses.is_dataclass(s)):
            return s
        try:
            keep, why = advice()
        except Exception as e:  # noqa: BLE001
            log.warning("Avis sur les photos indisponible (%s)", e)
            return s
        if keep:
            return s
        log.info("Photo non jointe (les chiffres : %s) ; l'extrait vidéo de l'actu reste possible", why)
        return dataclasses.replace(s, photos_enabled=False, second_image_enabled=False)

    def _claude_down(self) -> bool:
        """Claude ne répond pas en ce moment (coupe-circuit ouvert, crédit ou clé refusés)."""
        try:
            paused = getattr(self.llm, "paused", None)
            return bool((callable(paused) and paused()) or getattr(self.llm, "account_problem", None))
        except Exception:  # noqa: BLE001
            return False

    def plan_slots(self) -> int:
        """Rédige le prochain créneau dont l'heure de rédaction est venue.
        Au plus UNE rédaction par tour. Retourne 1 si une rédaction a eu lieu."""
        paused = getattr(self.llm, "paused", None)
        if callable(paused) and paused():
            # Claude en panne depuis peu : on réessaiera au tour suivant plutôt que de
            # sauter (thread, actu) ou de remplir (texte de secours) un créneau.
            return 0
        now = now_local()
        grace = timedelta(minutes=self.settings.approval_grace_minutes)
        for occ, slot in self._occurrences():
            lead = timedelta(minutes=self.settings.lead_for(slot.kind))
            if not (occ - lead <= now < occ + grace):
                continue
            date = occ.strftime("%Y-%m-%d")
            if self.state.slot_item_id(date, slot.key):
                continue
            self._draft_slot(slot, occ, date)
            return 1
        return 0

    def _slot_label(self, slot: Slot, occ: datetime) -> str:
        kind = KIND_FR.get(slot.kind, slot.kind)
        return f"{kind} de {JOURS[occ.weekday()]} {_hm(occ)}"

    def _draft_slot(self, slot: Slot, occ: datetime, date: str) -> dict | None:
        kind = slot.kind
        # Marqué AVANT la rédaction : si le bot est tué pendant l'appel à Claude,
        # le créneau n'est jamais rédigé deux fois (voir _recover_pending_slots).
        self.state.mark_slot(date, slot.key, SLOT_PENDING)
        log.info("Créneau %s (%s %s) : rédaction", slot.key, date, _hm(occ))
        try:
            news = self._news(kind)
            notes = self._notes()
            perf, style_test = self._context(kind)
            draft = None
            for attempt in (1, 2):
                draft = self._write(kind, news, notes, perf)
                if draft is not None and getattr(draft, "texts", None):
                    self._mark_style_test(draft, style_test)
                    break
                draft = None
                log.warning("Créneau %s : pas de brouillon (essai %d/2)", slot.key, attempt)
            if draft is None and self._claude_down():
                # Claude est tombé PENDANT la rédaction (crédit épuisé, panne) : rien à
                # voir avec l'actu. Le créneau n'est pas sauté : il sera rédigé dès que
                # Claude répond, tant que sa fenêtre est ouverte.
                self.state.unmark_slot(date, slot.key)
                log.warning("Créneau %s : Claude indisponible pendant la rédaction, on réessaiera", slot.key)
                return None
            fallback = self._fallback_kind(slot, occ) if draft is None else None
            if fallback:
                # FORMAT_AUTO : pas d'histoire assez solide pour une vidéo ou un thread aujourd'hui.
                # Un post prend la place : le créneau n'est pas perdu pour une question de format.
                log.info("Créneau %s : rien d'assez solide pour %s, un post « %s » prend sa place",
                         slot.key, KIND_FR.get(kind, kind), KIND_FR.get(fallback, fallback))
                perf, _test = self._context(fallback, test_ok=False)
                draft = self._write(fallback, self._news(fallback), notes, perf)
                if draft is not None and getattr(draft, "texts", None):
                    self.approvals.notify(
                        f"🔁 {esc(self._slot_label(slot, occ))} : rien d'assez solide pour ce format, "
                        f"remplacé par un post {esc(KIND_FR.get(fallback, fallback))}.", silent=True)
                    kind = fallback
                else:
                    draft = None
                    if self._claude_down():
                        self.state.unmark_slot(date, slot.key)
                        log.warning("Créneau %s : Claude indisponible pendant la rédaction, on réessaiera",
                                    slot.key)
                        return None
            if draft is None:
                if kind == "thread":
                    self.state.mark_slot(date, slot.key, SLOT_SKIPPED)
                    self.approvals.notify(
                        f"🧵 Thread de {JOURS[occ.weekday()]} {_hm(occ)} : Claude n'a rien trouvé d'assez "
                        "solide, créneau sauté. Mieux vaut pas de thread qu'un thread faible (les « pas "
                        "intéressé » coûtent cher dans l'algorithme).")
                    return None
                if kind != "gm" or self.settings.approval_mode == "auto":
                    # Pas de texte bouche-trou : sans histoire assez forte et vérifiable, le
                    # créneau est sauté (et en mode automatique, personne ne relirait un texte
                    # « de secours »). Seul l'ancien « gm » garde le sien, en mode Telegram.
                    self.state.mark_slot(date, slot.key, SLOT_SKIPPED)
                    self.approvals.notify(
                        f"📰 {esc(self._slot_label(slot, occ))} : rien d'assez fort ni de vérifiable dans "
                        "l'actu, créneau sauté. Mieux vaut pas de post qu'un post tiède ou faux.",
                        silent=True)
                    return None
                draft = self.writer.fallback_post(kind)
            self._mark_trend(draft)
            image_path, image_kind, second = self._visual(draft, f"{kind}_{date}")
            jitter_s = max(0, int(self.settings.publish_jitter_minutes)) * 60
            publish_at = occ + timedelta(seconds=round(self.rng.uniform(0, jitter_s)))
            expires_at = occ + timedelta(minutes=self.settings.approval_grace_minutes)
            if publish_at >= expires_at:
                publish_at = occ
            item = self.approvals.create_post_item(
                draft, slot_key=slot.key, slot_date=date, publish_at=publish_at, expires_at=expires_at,
                image_path=image_path, image_kind=image_kind, image_path2=second)
            self.state.mark_slot(date, slot.key, item["id"])
            log.info("Créneau %s : item %s prêt (publication prévue %s%s)", slot.key, item["id"],
                     iso(publish_at), ", texte de secours" if getattr(draft, "from_fallback", False) else "")
            return item
        except Exception as e:  # noqa: BLE001
            log.exception("Créneau %s : rédaction impossible : %s", slot.key, e)
            self.state.mark_slot(date, slot.key, SLOT_FAILED)
            self.approvals.notify(f"⚠️ {esc(self._slot_label(slot, occ))} : rédaction impossible "
                                  f"({esc(type(e).__name__)}). Regarde les logs.")
            return None

    def _recover_pending_slots(self) -> None:
        """Au démarrage : un créneau resté « pending » vient d'un bot arrêté en
        pleine rédaction. Si l'item existe, on le rattache ; sinon on réessaie
        UNE fois (puis « failed », pour ne jamais boucler sur un plantage)."""
        try:
            slots_done = self.state.data.get("slots_done") or {}
            retries = self.state.data.setdefault("slot_retries", {})
            changed = False
            for date, slots in slots_done.items():
                for key, val in list((slots or {}).items()):
                    if val != SLOT_PENDING:
                        continue
                    found = [it for it in self.state.items()
                             if it.get("slot_date") == date and it.get("slot_key") == key]
                    rk = f"{date}|{key}"
                    if found:
                        slots[key] = found[-1]["id"]
                    elif retries.get(rk):
                        slots[key] = SLOT_FAILED
                        log.warning("Créneau %s du %s : rédaction interrompue deux fois, abandonné", key, date)
                    else:
                        retries[rk] = iso(now_local())
                        del slots[key]
                        log.info("Créneau %s du %s : rédaction interrompue, nouvel essai", key, date)
                    changed = True
            cutoff = (now_local() - timedelta(days=10)).strftime("%Y-%m-%d")
            for rk in [k for k in retries if k[:10] < cutoff]:
                del retries[rk]
                changed = True
            if changed:
                self.state.flush()
        except Exception as e:  # noqa: BLE001
            log.exception("Reprise des créneaux interrompus impossible : %s", e)

    # ---- Ingrédients de la rédaction -----------------------------------
    def _news(self, kind: str, use_x: bool = True) -> list:
        # Fin de semaine, le thread est le récap de la semaine : 7 jours d'actu. Les
        # autres threads et le post « le futur arrive » puisent dans les 4 derniers
        # jours (une belle histoire ne périme pas en 36 h) ; l'actu, dans le frais.
        weekly = kind == "thread" and now_local().weekday() in WEEKLY_RECAP_WEEKDAYS
        hours = 168 if weekly else (FUTURE_NEWS_HOURS if kind in ("tip", "thread", "explainer") else None)
        try:
            items = (self.news.collect(max_age_hours=hours, use_x=use_x) if hours
                     else self.news.collect(use_x=use_x)) or []
        except Exception as e:  # noqa: BLE001
            log.exception("Actu : collecte impossible : %s", e)
            items = []
        picker = getattr(self.news, "top_for", None)
        if callable(picker) and items:
            try:
                # La semaine en revue se classe par intérêt seul ; les autres threads et « le
                # futur arrive », avec une fraîcheur lente (voir sources.top_for).
                items = picker("weekly" if weekly else kind, items,
                               NEWS_PER_THREAD if kind == "thread" else NEWS_PER_POST)
            except Exception as e:  # noqa: BLE001
                log.warning("Actu : sélection pour %s impossible (%s), liste complète", kind, e)
        return list(items)

    def _notes(self) -> list[str]:
        try:
            return [str(n.get("text")) for n in self.state.active_notes() if n.get("text")]
        except Exception as e:  # noqa: BLE001
            log.warning("Notes perso illisibles (%s)", e)
            return []

    def _perf(self, test: bool = False) -> str:
        """Pour le rédacteur : les notes de performance, puis nos meilleurs posts comme
        modèles de style (ou, pour un post d'essai, la liberté d'essayer autre chose)."""
        parts: list[str] = []
        try:
            parts.append(self.analytics.performance_notes() or "")
        except Exception as e:  # noqa: BLE001
            log.warning("Notes de performance indisponibles (%s)", e)
        fn = getattr(self.analytics, "style_notes", None)
        if callable(fn):
            try:
                parts.append(fn(test) or "")
            except Exception as e:  # noqa: BLE001
                log.warning("Modèles de style indisponibles (%s)", e)
        if self.strategist is not None:
            # Les consignes d'écriture de l'agent d'analyse ; pour un post d'essai, son idée d'essai.
            parts.append((self.strategist.test_block() if test else self.strategist.writer_block()) or "")
        return "\n".join(p.strip() for p in parts if p and p.strip())

    def _context(self, kind: str, *, test_ok: bool = True) -> tuple[str, bool]:
        """Avant chaque rédaction (consigne du propriétaire, 23/11/2026 : insister sur ce qui
        marche, et regarder les tendances à chaque fois) : la rédaction en chef reçoit ce qui
        marche sur le compte et les tendances du jour ; le rédacteur, nos meilleurs posts
        comme modèles de style. Trois posts prévus sur quatre suivent ce qui marche ; le
        quatrième essaie autre chose (`STYLE_TEST_SHARE`), sinon le compte ne découvrirait
        jamais ce qui pourrait marcher mieux. Les posts « à chaud » et les 🔄 nouvelles
        versions suivent toujours ce qui marche. Retourne (notes du rédacteur, essai ?)."""
        test = bool(test_ok and self.style_rng.random() < STYLE_TEST_SHARE)
        blocks: list[str] = []
        fn = getattr(self.analytics, "editor_notes", None)
        if callable(fn):
            try:
                blocks.append(fn(test) or "")
            except Exception as e:  # noqa: BLE001
                log.warning("Ce qui marche : notes indisponibles (%s)", e)
        if self.trends is not None:
            try:
                blocks.append(self.trends.block() or "")
            except Exception as e:  # noqa: BLE001
                log.warning("Tendances indisponibles (%s)", e)
        if self.strategist is not None and not test:
            blocks.append(self.strategist.editor_block() or "")      # les consignes de l'agent d'analyse
        setter = getattr(self.writer, "set_editor_context", None)
        if callable(setter):
            setter("\n\n".join(b.strip() for b in blocks if b and b.strip()))
        log.info("Rédaction %s : %s", kind,
                 "post d'essai (autre chose que ce qui marche)" if test else "suit ce qui marche")
        return self._perf(test), test

    def _mark_trend(self, draft) -> None:
        """Note sur le brouillon la tendance du jour à laquelle il colle (recherche Google, page
        Wikipédia), d'après son texte et les titres de ses sources : les statistiques diront
        ensuite si suivre les tendances rapporte (`fmt.trend`)."""
        matcher = getattr(self.trends, "match", None)
        if not callable(matcher) or draft is None:
            return
        try:
            texts = [str((getattr(draft, "texts", None) or [""])[0])]
            texts += [str(getattr(s, "title", "") or "") for s in getattr(draft, "sources", None) or []]
            draft.trend = str(matcher(texts) or "")
            if draft.trend:
                log.info("Tendance du jour reprise par ce post : %s", draft.trend)
        except Exception as e:  # noqa: BLE001
            log.warning("Tendance du post non notée (%s)", e)

    @staticmethod
    def _mark_style_test(draft, test: bool) -> None:
        try:
            draft.style_test = bool(test)
        except AttributeError:
            pass

    def _write(self, kind: str, news: list, notes: list[str], perf: str):
        if kind == "thread":
            return self.writer.draft_thread(news, notes, perf)
        if kind == "reactive":
            return self.writer.draft_reactive(news, notes, perf)
        if kind == "explainer":
            return self.writer.draft_explainer(news, notes, perf)
        return self.writer.draft_post(kind, news, notes, perf)

    def _visual(self, draft, tag: str) -> tuple[str | None, str | None, str | None]:
        """Image du post, comme un compte d'alertes info : la vidéo ou la photo de l'actu, et
        parfois une 2e image (drapeau, logo) ; sinon texte seul. Un sondage n'a pas d'image ;
        un post sans source non plus. (chemin, type, 2e image)."""
        if getattr(draft, "kind", "") == "explainer" and getattr(draft, "video_plan", None):
            return self._explainer_video(draft, tag)
        if getattr(draft, "poll", None) or getattr(draft, "visual", None) is None:
            return None, None, None
        fn = self._prepare_visual
        if fn is None:
            from . import visuals
            fn = visuals.prepare_media
        try:
            res = fn(self._media_settings(draft), draft, self.writer, tag)
            if isinstance(res, tuple) and len(res) == 2:
                res = (*res, None)
            path, kind, second = res if isinstance(res, tuple) and len(res) == 3 else (None, None, None)
        except Exception as e:  # noqa: BLE001
            log.exception("Visuel : erreur (%s), post en texte seul", e)
            return None, None, None
        if not path:
            return None, None, None
        return path, kind, (second if kind == "photo" and isinstance(second, str) and second else None)

    def _explainer_video(self, draft, tag: str) -> tuple[str | None, str | None, str | None]:
        """La vidéo explicative animée (bot/explainer.py), avec la photo de l'actu en fond quand
        il y en a une. Vidéo impossible : la photo seule, sinon texte seul."""
        fn = self._prepare_explainer
        if fn is None:
            from . import explainer
            fn = explainer.prepare
        try:
            res = fn(self.settings, draft, self.writer, tag)
            path, kind = (tuple(res) + (None, None))[:2] if isinstance(res, tuple) else (None, None)
        except Exception as e:  # noqa: BLE001
            log.exception("Vidéo explicative : erreur (%s), post en texte seul", e)
            return None, None, None
        return (path, kind, None) if path else (None, None, None)

    # ==================================================================
    # Post réactif (rare : la porte de Claude dit « non » la plupart du temps)
    # ==================================================================
    def _near_slot(self, now: datetime, gap_min: float) -> bool:
        for occ, _slot in self._occurrences((0,)):
            if abs(minutes_between(occ, now)) < gap_min:
                return True
        return False

    def _slot_gap(self) -> float:
        """Écart mini, en minutes, entre un post « à chaud » et un post prévu
        (REACTIVE_SLOT_GAP_MINUTES). Sans ce réglage : le même qu'entre deux posts « à chaud »
        (REACTIVE_MIN_GAP_MINUTES). Pendant cet écart, l'actu chaude n'est pas regardée du tout :
        trop large, une annonce tombée juste après un post prévu attend une heure."""
        base = self.settings.reactive_min_gap_min
        gap = getattr(self.settings, "reactive_slot_gap_min", None)
        if gap is None:
            return base
        try:
            return max(0.0, float(gap))
        except (TypeError, ValueError):
            return base

    def _recent_reactive(self, now: datetime, gap_min: float) -> bool:
        for it in self.state.items(kinds=("reactive",)):
            created = parse_iso(it.get("created_at"))
            if created is not None and 0 <= minutes_between(created, now) < gap_min:
                return True
        return False

    def _new_breaking_candidates(self, news: list, now) -> list[str]:
        """Ids des actus fraîches (< 12 h) qui peuvent valoir un post immédiat — annonce
        officielle, actu qui décolle (likes sur X, points sur Hacker News) ou histoire
        de nature à intriguer tout le monde — et que la rédaction n'a pas encore
        examinées. Ce n'est qu'un déclencheur : c'est Claude qui décide ensuite."""
        seen = set(self.state.data.get("breaking_seen") or [])
        out: list[str] = []
        for it in news:
            iid = str(getattr(it, "id", "") or "")
            if not iid or iid in seen:
                continue
            published = getattr(it, "published", None)
            if published is None or (now - published) > timedelta(hours=12):
                continue
            appeal = self._appeal(it)
            hot = (bool(getattr(it, "official", False))
                   or int(getattr(it, "engagement", 0) or 0) >= BREAKING_MIN_ENGAGEMENT
                   or appeal > 1.0)
            if hot and appeal >= 1.0:
                out.append(iid)
        return out

    @staticmethod
    def _appeal(it) -> float:
        """> 1 : de quoi intriguer tout le monde (un robot, une première, un nom connu) ;
        < 1 : annonce pour développeurs, entreprises ou investisseurs — inutile de
        demander à Claude si elle mérite un post grand public."""
        try:
            from .sources import audience_factor
            return float(audience_factor(it))
        except Exception:  # noqa: BLE001
            return 1.0

    def _remember_breaking_candidates(self, ids: list[str]) -> None:
        seen = [i for i in (self.state.data.get("breaking_seen") or []) if isinstance(i, str)]
        self.state.data["breaking_seen"] = (seen + [i for i in ids if i not in seen])[-400:]
        self.state.flush()

    def _reactive_max_checks(self) -> int:
        """Fois par jour, au plus, où Claude est interrogé sur l'actu chaude
        (REACTIVE_MAX_CHECKS_PER_DAY). Passé ce nombre, le bot ne voit plus rien jusqu'au
        lendemain : trop bas, il rate les annonces du soir (la journée aux États-Unis)."""
        try:
            return max(1, int(getattr(self.settings, "reactive_max_checks_per_day",
                                      REACTIVE_MAX_CHECKS_PER_DAY)))
        except (TypeError, ValueError):
            return REACTIVE_MAX_CHECKS_PER_DAY

    def maybe_reactive(self, force: bool = False) -> int:
        """`force` (commande --once reactive) ignore la fenêtre, l'intervalle et
        l'écart avec les créneaux, jamais le plafond du jour."""
        s = self.settings
        if not s.reactive_enabled and not force:
            return 0
        if self.state.count("reactive") >= self.limits.reactive():
            return 0
        now = now_local()
        if not force:
            if not in_window(now.time(), s.reactive_window):
                return 0
            since = self.state.minutes_since("reactive")
            if since is not None and since < s.reactive_check_every_min:
                return 0
            if self._near_slot(now, self._slot_gap()):
                return 0
            if self._recent_reactive(now, s.reactive_min_gap_min):
                return 0
        if self._claude_down():
            return 0                 # ni vérification comptée, ni actu marquée « déjà vue »
        self.state.set_last_run("reactive")
        news = self._news("reactive")
        if not news:
            log.info("Réactif : pas d'actu fraîche")
            return 0
        # Claude n'est appelé que s'il y a du NOUVEAU qui pourrait mériter un post
        # (annonce officielle ou actu qui décolle), et au plus N fois par jour.
        fresh = self._new_breaking_candidates(news, now)
        if not fresh and not force:
            return 0
        max_checks = self._reactive_max_checks()
        if self.state.count("reactive_checks") >= max_checks and not force:
            log.info("Réactif : %d vérifications aujourd'hui, on s'arrête là", max_checks)
            return 0
        self.state.incr("reactive_checks")
        self._remember_breaking_candidates(fresh)
        # La rédaction en chef ne lit que ce qui peut parler à tout le monde : les annonces
        # pour développeurs, entreprises ou investisseurs ne valent jamais un post immédiat.
        news = [it for it in news if self._appeal(it) >= 1.0]
        perf, _test = self._context("reactive", test_ok=False)
        draft = self.writer.draft_reactive(news, self._notes(), perf)
        if draft is None or not getattr(draft, "texts", None):
            log.info("Réactif : rien d'assez fort pour l'instant")
            return 0
        self._mark_trend(draft)
        image_path, image_kind, second = self._visual(draft, f"reactive_{today_str()}")
        now = now_local()
        item = self.approvals.create_post_item(
            draft, slot_key=None, slot_date=None, publish_at=now,
            expires_at=now + timedelta(minutes=REACTIVE_EXPIRES_MIN),
            image_path=image_path, image_kind=image_kind, image_path2=second)
        self.state.incr("reactive")
        log.info("Réactif : item %s proposé", item.get("id"))
        return 1

    # ==================================================================
    # Statistiques, rapports, ménage
    # ==================================================================
    def maybe_analytics(self) -> int:
        updated = self._refresh_stats()
        self._label_topics()
        return updated

    def _refresh_stats(self) -> int:
        fn = getattr(self.analytics, "maybe_refresh", None)
        if callable(fn):
            return int(fn() or 0)
        since = self.state.minutes_since("analytics")
        if since is not None and since < ANALYTICS_EVERY_MIN:
            return 0
        self.state.set_last_run("analytics")
        return int(self.analytics.refresh_metrics() or 0)

    def _label_topics(self) -> int:
        """Classe par sujet les posts qui n'en ont pas encore (un appel court à Claude, toutes
        les 6 h au plus) : c'est ce qui dit quels sujets marchent. Optionnel : cède au budget."""
        fn = getattr(self.analytics, "ensure_topics", None)
        labeler = getattr(self.writer, "label_topics", None)
        if not callable(fn) or not callable(labeler):
            return 0
        if self._claude_down() or not self._claude_optional_ok():
            return 0
        return int(fn(labeler) or 0)

    def maybe_strategy(self) -> int:
        """L'agent d'analyse, tous les trois jours (consigne du propriétaire, 24/11/2026 : « analyse
        avec les agents et prends les bonnes décisions ») : nos chiffres, les tendances et
        l'algorithme de X deviennent des consignes pour la rédaction en chef et le rédacteur,
        relues par un second agent, puis résumées au propriétaire. Optionnel : cède au budget
        Claude, et attend si Claude est en panne."""
        st = self.strategist
        if st is None:
            return 0
        watch = getattr(st, "watch", None)
        if callable(watch):
            watch()                # chaque jour, gratuit : ce que X a changé dans son algorithme
        if not st.due():
            return 0
        if self._claude_down():
            return 0
        memo = st.review()
        if not memo:
            return 0
        text = st.report_fr(memo)
        if text:
            self.approvals.notify(text, silent=True)
        return 1

    def _reports(self) -> dict:
        rep = self.state.data.get("reports")
        if not isinstance(rep, dict):
            rep = self.state.data["reports"] = {}
        return rep

    def maybe_daily_report(self) -> bool:
        now = now_local()
        today = now.strftime("%Y-%m-%d")
        reports = self._reports()
        if reports.get("daily") == today:
            return False
        at = at_local(now, self.settings.health_report_at)
        if now < at:
            return False
        reports["daily"] = today  # marqué AVANT : jamais deux récaps le même jour
        self.state.flush()
        if now - at > timedelta(hours=DAILY_REPORT_LATE_MAX_H):
            log.info("Récap du matin sauté (trop tard dans la journée)")
            return False
        text = self.analytics.daily_report()
        if not text:
            return False
        if self.state.data.get("paused"):
            text = "⏸️ <b>Bot en pause</b> — /reprendre pour relancer\n\n" + text
        self.approvals.notify(text)
        log.info("Récap du matin envoyé")
        return True

    def maybe_weekly_report(self) -> bool:
        slot = self.settings.weekly_report
        if slot is None or slot.weekday is None:
            return False
        now = now_local()
        days_back = (now.weekday() - slot.weekday) % 7
        occ = at_local(now - timedelta(days=days_back), slot.at)
        if occ > now:
            occ = at_local(now - timedelta(days=days_back + 7), slot.at)
        key = occ.strftime("%Y-%m-%d")
        reports = self._reports()
        if reports.get("weekly") == key:
            return False
        if now - occ > timedelta(hours=WEEKLY_REPORT_LATE_MAX_H):
            return False  # bot arrêté au mauvais moment : on attend la semaine suivante
        reports["weekly"] = key
        self.state.flush()
        text = self.analytics.weekly_report()
        if not text:
            return False
        self.approvals.notify(text)
        log.info("Bilan de la semaine envoyé")
        return True

    def maybe_gc(self) -> int:
        since = self.state.minutes_since("gc")
        if since is not None and since < GC_EVERY_MIN:
            return 0
        self.state.set_last_run("gc")
        n = self.state.gc()
        if n:
            log.info("Ménage : %d ancien(s) item(s) supprimé(s) de l'état", n)
        return n

    # ==================================================================
    # Textes des commandes /statut, /plan, /budget (HTML Telegram)
    # ==================================================================
    def _llm_spent(self) -> tuple[float, int, float]:
        days = (self.state.data.get("llm_usage") or {}).get("days") or {}
        today = today_str()
        month = today[:7]
        d = days.get(today) or {}
        month_usd = sum(float((v or {}).get("usd", 0.0)) for k, v in days.items() if k.startswith(month))
        try:   # avec les essais faits à la main (aperçus, vérifications), comptés dans le même plafond
            from .llm import manual_spent_month
            month_usd += manual_spent_month(month)
        except Exception:  # noqa: BLE001
            pass
        return float(d.get("usd", 0.0)), int(d.get("calls", 0)), month_usd

    def _claude_cap_text(self) -> str:
        """« sur 35 $ » quand un plafond du mois est réglé, sinon rien."""
        try:
            cap = float(getattr(self.settings, "claude_budget_monthly_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            cap = 0.0
        return f" sur {cap:.0f} $ (plafond du mois)" if cap > 0 else ""

    def status_text(self) -> str:
        s = self.settings
        now = now_local()
        today = today_str()
        paused = bool(self.state.data.get("paused"))
        lines = [f"🤖 <b>Bot {'⏸️ en pause' if paused else '▶️ actif'}</b>"
                 + (" — /reprendre pour relancer" if paused else "")]
        mode = "validation sur Telegram" if s.approval_mode == "telegram" else "⚠️ AUTOMATIQUE (sans validation)"
        lines.append(f"Mode : {mode}" + (" · 🧪 simulation (DRY_RUN)" if s.dry_run else ""))
        published = [p for p in self.state.data.get("published") or []
                     if str(p.get("created_at") or "").startswith(today) and p.get("kind") != "comment_reply"]
        lines.append(f"📤 Aujourd'hui : {len(published)} post(s) publié(s) · ⚡ réactif "
                     f"{self.state.count('reactive')}/{self.limits.reactive()} · 💬 "
                     f"{self.state.count('comment_drafts')} brouillon(s) de réponse · 🎯 radar "
                     f"{self.state.count('radar_suggestions')}/{s.radar_max_per_day}")
        groups = (("awaiting", "⏳ À valider"), ("approved", "🗓️ Programmé"),
                  ("publishing", "📤 En cours de publication"))
        for status, title in groups:
            its = [it for it in self.state.items(statuses=(status,)) if it.get("kind") != "radar"]
            if not its:
                continue
            lines.append(f"{title} ({len(its)}) :")
            for it in its[:8]:
                when = parse_iso(it.get("expires_at") if status == "awaiting" else it.get("publish_at"))
                when_s = ""
                if when is not None:
                    when_s = (f" — expire {_day_label(when, now)} {_hm(when)}" if status == "awaiting"
                              else f" — {_day_label(when, now)} {_hm(when)}")
                text = (it.get("texts") or [""])[0]
                lines.append(f"• {KIND_FR.get(it.get('kind'), esc(str(it.get('kind'))))} "
                             f"« {esc(_excerpt(text))} »{when_s}")
            if len(its) > 8:
                lines.append(f"• … et {len(its) - 8} autre(s)")
        radar_open = self.state.items(kinds=("radar",), statuses=("suggested",))
        if radar_open:
            lines.append(f"🎯 {len(radar_open)} suggestion(s) radar ouvertes (à publier toi-même)")
        failed = [it for it in self.state.items(statuses=("failed",))
                  if (parse_iso(it.get("created_at")) or now) >= now - timedelta(hours=24)]
        if failed:
            lines.append(f"⚠️ {len(failed)} échec(s) de publication ces dernières 24 h")
        if not paused and not any(self.state.items(statuses=("awaiting", "approved", "publishing"))):
            lines.append("Rien en attente pour l'instant. /plan pour les prochains créneaux.")
        lines.append("")
        lines.append(f"💸 {esc(self.budget.summary())}")
        usd, calls, month = self._llm_spent()
        lines.append(f"🧠 Claude : {usd:.2f} $ aujourd'hui ({calls} appel(s)), {month:.2f} $ ce mois"
                     f"{self._claude_cap_text()}")
        return "\n".join(lines)

    def _slot_status(self, slot: Slot, occ: datetime, now: datetime) -> str:
        date = occ.strftime("%Y-%m-%d")
        val = self.state.slot_item_id(date, slot.key)
        if not val:
            draft_at = occ - timedelta(minutes=self.settings.lead_for(slot.kind))
            if draft_at > now:
                return f"rédaction {_day_label(draft_at, now)} vers {_hm(draft_at)}"
            return "rédaction imminente"
        if val == SLOT_PENDING:
            return "✍️ rédaction en cours"
        if val == SLOT_SKIPPED:
            return "⏭️ sauté (rien d'assez solide)"
        if val == SLOT_FAILED:
            return "⚠️ échec de la rédaction"
        it = self.state.item(val)
        if it is None:
            return "?"
        st = it.get("status")
        label = ITEM_STATUS_FR.get(st, st or "?")
        if st == "approved":
            pa = parse_iso(it.get("publish_at"))
            if pa is not None:
                label += f" à {_hm(pa)}"
        if st == "awaiting":
            exp = parse_iso(it.get("expires_at"))
            if exp is not None:
                label += f" (jusqu'à {_hm(exp)})"
        return label

    def plan_text(self) -> str:
        now = now_local()
        horizon = now + timedelta(hours=24)
        grace = timedelta(minutes=self.settings.approval_grace_minutes)
        lines = ["🗓️ <b>Prochaines 24 h</b>"]
        if self.state.data.get("paused"):
            lines.append("⏸️ En pause : rien n'est rédigé ni publié avant /reprendre")
        occs = [(o, sl) for o, sl in self._occurrences((-1, 0, 1, 2)) if o + grace > now and o <= horizon]
        if not occs:
            lines.append("Aucun créneau prévu.")
        for occ, slot in occs:
            lines.append(f"• {_day_label(occ, now)} {_hm(occ)} {KIND_FR.get(slot.kind, slot.kind)} — "
                         f"{self._slot_status(slot, occ, now)}")
        for day in (now, now + timedelta(days=1)):
            plan = self._format_plan(day)
            if plan:                                  # FORMAT_AUTO : pourquoi ce format ce jour-là
                extra = (f" ; {KIND_FR.get(plan['second'], plan['second'])} à la place d'un post d'actu : "
                         f"{plan.get('why2') or ''}") if plan.get("second") else ""
                lines.append(f"🎯 Formats {_day_label(day, now)} : {KIND_FR.get(plan['feature'], plan['feature'])} "
                             f"— {esc(str(plan.get('why') or ''))}{esc(extra)}")
        s = self.settings
        reactive = [it for it in self.state.items(kinds=("reactive",), statuses=("awaiting", "approved"))]
        line = f"⚡ Réactif : {self.state.count('reactive')}/{self.limits.reactive()} aujourd'hui"
        if not s.reactive_enabled:
            line = "⚡ Réactif : désactivé"
        elif reactive:
            line += f", {len(reactive)} en cours"
        lines.append(line)
        if s.approval_mode == "auto":
            lines.append("Mode automatique : chaque post part à son heure, sans validation.")
        else:
            lines.append(f"Brouillons proposés à l'avance (gm la veille au soir) ; sans ta réponse, rien "
                         f"n'est publié ({s.approval_grace_minutes} min de délai après l'heure prévue).")
        return "\n".join(lines)

    def budget_text(self) -> str:
        s = self.settings
        usd, calls, month = self._llm_spent()
        lines = ["💰 <b>Dépenses</b>",
                 f"💸 {esc(self.budget.summary())}",
                 f"   lectures optionnelles (radar, actu) bloquées au-delà de "
                 f"{s.x_budget_daily_usd * 0.8:.2f} $/jour ; tout s'arrête à {s.x_budget_monthly_usd:.0f} $/mois",
                 f"🧠 Claude : {usd:.2f} $ aujourd'hui ({calls} appel(s)), {month:.2f} $ ce mois"
                 f"{self._claude_cap_text()}"]
        if self._claude_cap_text():
            lines.append("   le radar, les posts « à chaud » et les réponses aux commentaires s'arrêtent en "
                         "premier ; tout s'arrête au plafond, jusqu'au 1er du mois")
        return "\n".join(lines)
