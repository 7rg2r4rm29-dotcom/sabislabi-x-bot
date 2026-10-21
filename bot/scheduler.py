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
     (rédaction Claude, capture de page) part sans attendre le tour suivant.

Rien n'est publié sans ta validation sur Telegram (sauf APPROVAL_MODE=auto,
verrouillé par la config) ; le radar ne publie jamais rien.
"""
from __future__ import annotations

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
THREAD_CLEAR_MIN = 180  # un jour de thread, pas d'autre post (hors gm) à moins de 3 h de lui
TICK_BUDGET_S = 240     # au-delà, les étapes facultatives attendent le tour suivant
ERROR_SLEEP_S = 60
WATCHDOG_S = 420
ANALYTICS_EVERY_MIN = 180
GC_EVERY_MIN = 24 * 60
REACTIVE_EXPIRES_MIN = 180
DAILY_REPORT_LATE_MAX_H = 12      # récap du matin envoyé au plus tard 12 h après l'heure prévue
WEEKLY_REPORT_LATE_MAX_H = 24
STARTUP_NOTICE_EVERY_MIN = 360    # pas de rafale de « démarré » si systemd relance en boucle
NEWS_PER_POST = 15
NEWS_PER_THREAD = 20

# Marqueurs de créneau (state.slots_done) autres qu'un id d'item.
SLOT_PENDING = "pending"          # rédaction en cours (posé AVANT d'appeler Claude : jamais de doublon)
SLOT_SKIPPED = "skipped"          # thread sans candidat solide : créneau sauté
SLOT_FAILED = "failed"            # erreur inattendue pendant la rédaction
SLOT_MARKERS = (SLOT_PENDING, SLOT_SKIPPED, SLOT_FAILED)

POST_KINDS = ("gm", "tip", "news")
KIND_FR = {"gm": "☀️ GM", "tip": "💡 astuce", "news": "📰 actu", "thread": "🧵 thread",
           "reactive": "⚡ réactif", "comment_reply": "💬 réponse", "radar": "🎯 radar"}
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
                 rng: random.Random | None = None) -> None:
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
        self._prepare_visual = prepare_visual
        self.rng = rng or random.Random()
        self._stop = False
        self._ticks = 0
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
        if kind not in POST_KINDS + ("thread", "reactive"):
            return None
        news = self._news(kind, use_x=False)
        return self._write(kind, news, self._notes(), self._perf())

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
            for label, key, fn in optional:
                if key == "reactive" and res["plan"]:
                    continue  # une seule rédaction (lente) par tour
                if time.monotonic() - started > TICK_BUDGET_S:
                    log.info("Tour long (%.0f s) : %s reporté au tour suivant", time.monotonic() - started, label)
                    continue
                res[key] = self._step(label, fn)
                res["publish_mid"] += self._between()
        res["metrics"] = self._step("statistiques", self.maybe_analytics)
        res["daily"] = self._step("récap du matin", self.maybe_daily_report)
        res["weekly"] = self._step("bilan de la semaine", self.maybe_weekly_report)
        res["gc"] = self._step("ménage", self.maybe_gc)
        res["poll2"] = self._step("Telegram", self.approvals.poll)
        res["publish2"] = self._step("publication", self.publisher.publish_due)
        self._heartbeat(paused, time.monotonic() - started, res)
        return res

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
            threads = [at_local(day, sl.at) for sl in self.settings.thread_slots or []
                       if sl.weekday == day.weekday()]
            for slot in self.settings.post_slots or []:
                occ = at_local(day, slot.at)
                # Jour de thread : le thread prend la place du post le plus proche (pas
                # plus de 3 posts par jour, et jamais deux posts à moins de 3 h).
                if slot.kind != "gm" and any(abs(minutes_between(occ, t)) < THREAD_CLEAR_MIN for t in threads):
                    continue
                out.append((occ, slot))
            for slot in self.settings.thread_slots or []:
                if slot.weekday == day.weekday():
                    out.append((at_local(day, slot.at), slot))
        out.sort(key=lambda p: p[0])
        return out

    def plan_slots(self) -> int:
        """Rédige le prochain créneau dont l'heure de rédaction est venue.
        Au plus UNE rédaction par tour. Retourne 1 si une rédaction a eu lieu."""
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
            perf = self._perf()
            draft = None
            for attempt in (1, 2):
                draft = self._write(kind, news, notes, perf)
                if draft is not None and getattr(draft, "texts", None):
                    break
                draft = None
                log.warning("Créneau %s : pas de brouillon (essai %d/2)", slot.key, attempt)
            if draft is None:
                if kind == "thread":
                    self.state.mark_slot(date, slot.key, SLOT_SKIPPED)
                    self.approvals.notify(
                        f"🧵 Thread de {JOURS[occ.weekday()]} {_hm(occ)} : Claude n'a rien trouvé d'assez "
                        "solide, créneau sauté. Mieux vaut pas de thread qu'un thread faible (les « pas "
                        "intéressé » coûtent cher dans l'algorithme).")
                    return None
                if kind == "news":
                    # Pas de texte bouche-trou sur le créneau « actu » : une maxime de
                    # développement perso sur un compte d'actu IA fait fuir.
                    self.state.mark_slot(date, slot.key, SLOT_SKIPPED)
                    self.approvals.notify(
                        f"📰 Actu de {JOURS[occ.weekday()]} {_hm(occ)} : rien d'assez solide ou de vérifiable "
                        "aujourd'hui, créneau sauté. Mieux vaut pas d'actu qu'une actu creuse ou fausse.",
                        silent=True)
                    return None
                draft = self.writer.fallback_post(kind)
            image_path, image_kind = self._visual(draft, f"{kind}_{date}")
            jitter_s = max(0, int(self.settings.publish_jitter_minutes)) * 60
            publish_at = occ + timedelta(seconds=round(self.rng.uniform(0, jitter_s)))
            expires_at = occ + timedelta(minutes=self.settings.approval_grace_minutes)
            if publish_at >= expires_at:
                publish_at = occ
            item = self.approvals.create_post_item(
                draft, slot_key=slot.key, slot_date=date, publish_at=publish_at, expires_at=expires_at,
                image_path=image_path, image_kind=image_kind)
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
        try:
            items = self.news.collect(use_x=use_x) or []
        except Exception as e:  # noqa: BLE001
            log.exception("Actu : collecte impossible : %s", e)
            items = []
        picker = getattr(self.news, "top_for", None)
        if callable(picker) and items:
            try:
                items = picker(kind, items, NEWS_PER_THREAD if kind == "thread" else NEWS_PER_POST)
            except Exception as e:  # noqa: BLE001
                log.warning("Actu : sélection pour %s impossible (%s), liste complète", kind, e)
        return list(items)

    def _notes(self) -> list[str]:
        try:
            return [str(n.get("text")) for n in self.state.active_notes() if n.get("text")]
        except Exception as e:  # noqa: BLE001
            log.warning("Notes perso illisibles (%s)", e)
            return []

    def _perf(self) -> str:
        try:
            return self.analytics.performance_notes() or ""
        except Exception as e:  # noqa: BLE001
            log.warning("Notes de performance indisponibles (%s)", e)
            return ""

    def _write(self, kind: str, news: list, notes: list[str], perf: str):
        if kind == "thread":
            return self.writer.draft_thread(news, notes, perf)
        if kind == "reactive":
            return self.writer.draft_reactive(news, notes, perf)
        return self.writer.draft_post(kind, news, notes, perf)

    def _visual(self, draft, tag: str) -> tuple[str | None, str | None]:
        if getattr(draft, "poll", None) or getattr(draft, "visual", None) is None:
            return None, None
        fn = self._prepare_visual
        if fn is None:
            from . import visuals
            fn = visuals.prepare
        try:
            res = fn(self.settings, draft, self.writer, tag)
            path, kind = res if isinstance(res, tuple) and len(res) == 2 else (None, None)
        except Exception as e:  # noqa: BLE001
            log.exception("Visuel : erreur (%s), post en texte seul", e)
            return None, None
        return (path, kind) if path else (None, None)

    # ==================================================================
    # Post réactif (rare : la porte de Claude dit « non » la plupart du temps)
    # ==================================================================
    def _near_slot(self, now: datetime, gap_min: float) -> bool:
        for occ, _slot in self._occurrences((0,)):
            if abs(minutes_between(occ, now)) < gap_min:
                return True
        return False

    def _recent_reactive(self, now: datetime, gap_min: float) -> bool:
        for it in self.state.items(kinds=("reactive",)):
            created = parse_iso(it.get("created_at"))
            if created is not None and 0 <= minutes_between(created, now) < gap_min:
                return True
        return False

    def maybe_reactive(self, force: bool = False) -> int:
        """`force` (commande --once reactive) ignore la fenêtre, l'intervalle et
        l'écart avec les créneaux, jamais le plafond du jour."""
        s = self.settings
        if not s.reactive_enabled and not force:
            return 0
        if self.state.count("reactive") >= s.reactive_max_per_day:
            return 0
        now = now_local()
        if not force:
            if not in_window(now.time(), s.reactive_window):
                return 0
            since = self.state.minutes_since("reactive")
            if since is not None and since < s.reactive_check_every_min:
                return 0
            if self._near_slot(now, s.reactive_min_gap_min):
                return 0
            if self._recent_reactive(now, s.reactive_min_gap_min):
                return 0
        self.state.set_last_run("reactive")
        news = self._news("reactive")
        if not news:
            log.info("Réactif : pas d'actu fraîche")
            return 0
        draft = self.writer.draft_reactive(news, self._notes(), self._perf())
        if draft is None or not getattr(draft, "texts", None):
            log.info("Réactif : rien d'assez fort pour l'instant")
            return 0
        image_path, image_kind = self._visual(draft, f"reactive_{today_str()}")
        now = now_local()
        item = self.approvals.create_post_item(
            draft, slot_key=None, slot_date=None, publish_at=now,
            expires_at=now + timedelta(minutes=REACTIVE_EXPIRES_MIN),
            image_path=image_path, image_kind=image_kind)
        self.state.incr("reactive")
        log.info("Réactif : item %s proposé", item.get("id"))
        return 1

    # ==================================================================
    # Statistiques, rapports, ménage
    # ==================================================================
    def maybe_analytics(self) -> int:
        fn = getattr(self.analytics, "maybe_refresh", None)
        if callable(fn):
            return int(fn() or 0)
        since = self.state.minutes_since("analytics")
        if since is not None and since < ANALYTICS_EVERY_MIN:
            return 0
        self.state.set_last_run("analytics")
        return int(self.analytics.refresh_metrics() or 0)

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
        return float(d.get("usd", 0.0)), int(d.get("calls", 0)), month_usd

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
                     f"{self.state.count('reactive')}/{s.reactive_max_per_day} · 💬 "
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
        lines.append(f"🧠 Claude : {usd:.2f} $ aujourd'hui ({calls} appel(s)), {month:.2f} $ ce mois")
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
        s = self.settings
        reactive = [it for it in self.state.items(kinds=("reactive",), statuses=("awaiting", "approved"))]
        line = f"⚡ Réactif : {self.state.count('reactive')}/{s.reactive_max_per_day} aujourd'hui"
        if not s.reactive_enabled:
            line = "⚡ Réactif : désactivé"
        elif reactive:
            line += f", {len(reactive)} en cours"
        lines.append(line)
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
                 f"🧠 Claude : {usd:.2f} $ aujourd'hui ({calls} appel(s)), {month:.2f} $ ce mois"]
        return "\n".join(lines)
