"""Le nombre de posts par jour, réglé par l'agent d'analyse (accord du propriétaire, 24/11/2026 :
« il peut régler même les limites de post si nécessaire, pas de problème […] juste pour l'argent
fais attention »).

Deux limites peuvent bouger, d'un cran par analyse, dans des bornes fixes :
  - le nombre de posts prévus par jour : un de moins ou un de plus que les réglages (`POST_SLOTS`) ;
    un de moins = le dernier post d'actu de la journée saute ; un de plus = un post d'actu à une
    heure libre (9 h), loin des autres ;
  - le nombre maximum de posts « à chaud » par jour : de 1 à 5.
Une décision s'applique à partir du LENDEMAIN (les créneaux d'un jour ne changent jamais en cours de
route) et reste notée dans l'état (`post_limits`).

L'argent : toute hausse passe d'abord par `afford`. Au rythme des 7 derniers jours, plus le coût des
posts ajoutés, la dépense Claude d'un mois entier doit rester sous 80 % du plafond
(`CLAUDE_BUDGET_MONTHLY_USD`), et celle de l'API X sous 90 % du sien. Sinon la hausse est refusée,
quoi qu'en dise l'agent. Une baisse est toujours permise.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .config import Slot, parse_hhmm
from .utils import iso, now_local

log = logging.getLogger("xbot.limits")

REACTIVE_RANGE = (1, 5)          # posts « à chaud » par jour, au plus
SCHEDULED_SPREAD = 1             # posts prévus : un de moins ou un de plus que les réglages
STEP = 1                         # un cran par analyse : sinon on ne saurait pas ce qui a fait quoi
EXTRA_SLOT_TIMES = ("09:00", "11:00", "17:00", "21:00")     # heures possibles pour un post d'actu en plus
MIN_SLOT_GAP_MIN = 180           # … à 3 h au moins de tout autre post prévu
POST_COST_USD = 0.45             # un post de plus par jour, côté Claude (large : 0,25 à 0,38 $ mesurés)
X_POST_COST_USD = 0.03           # … et côté API X (le post et son image)
CLAUDE_SAFE_SHARE = 0.80         # part du plafond Claude à ne pas dépasser sur un mois entier
X_SAFE_SHARE = 0.90
KEEP = 12


def _minute(slot: Slot) -> int:
    return slot.at.hour * 60 + slot.at.minute


class PostLimits:
    def __init__(self, settings, state, llm=None, budget=None) -> None:
        self.settings = settings
        self.state = state
        self.llm = llm
        self.budget = budget

    # ---- Ce qui est en vigueur ----------------------------------------
    def _decisions(self) -> list[dict]:
        rows = self.state.data.get("post_limits")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def decision(self, day: datetime | None = None) -> dict | None:
        """La décision en vigueur ce jour-là (la plus récente déjà applicable), ou None."""
        date = (day or now_local()).strftime("%Y-%m-%d")
        rows = [r for r in self._decisions() if str(r.get("from") or "9999") <= date]
        return max(rows, key=lambda r: (str(r.get("from")), str(r.get("decided_at") or ""))) if rows else None

    def scheduled_base(self) -> int:
        return sum(1 for s in self.settings.post_slots or [] if s.kind != "gm")

    def reactive_base(self) -> int:
        try:
            return max(0, int(self.settings.reactive_max_per_day))
        except (TypeError, ValueError):
            return 0

    def scheduled_range(self) -> tuple[int, int]:
        base = self.scheduled_base()
        return max(1, base - SCHEDULED_SPREAD), base + SCHEDULED_SPREAD

    def scheduled(self, day: datetime | None = None) -> int:
        d = self.decision(day)
        lo, hi = self.scheduled_range()
        try:
            return min(hi, max(lo, int(d["scheduled"]))) if d else self.scheduled_base()
        except (KeyError, TypeError, ValueError):
            return self.scheduled_base()

    def reactive(self, day: datetime | None = None) -> int:
        d = self.decision(day)
        try:
            return min(REACTIVE_RANGE[1], max(REACTIVE_RANGE[0], int(d["reactive"]))) if d else self.reactive_base()
        except (KeyError, TypeError, ValueError):
            return self.reactive_base()

    def target(self) -> tuple[int, int]:
        """(posts prévus, posts à chaud au plus) de la DERNIÈRE décision, même si elle ne s'applique
        que demain : c'est d'elle que part la décision suivante."""
        far = now_local() + timedelta(days=3650)
        return self.scheduled(far), self.reactive(far)

    def post_slots(self, day: datetime | None = None) -> list[Slot]:
        """Les posts prévus d'un jour : ceux des réglages, moins le dernier post d'actu ou plus
        un post d'actu à une heure libre, selon la décision en vigueur ce jour-là."""
        slots = list(self.settings.post_slots or [])
        target = self.scheduled(day)
        core = [s for s in slots if s.kind != "gm"]
        while len(core) > target:
            news = [s for s in core if s.kind == "news"]
            if not news:
                break
            last = max(news, key=_minute)
            core.remove(last)
            slots.remove(last)
        for hhmm in EXTRA_SLOT_TIMES:
            if len(core) >= target:
                break
            extra = Slot("news", parse_hhmm(hhmm))
            if all(abs(_minute(extra) - _minute(s)) >= MIN_SLOT_GAP_MIN for s in core):
                core.append(extra)
                slots.append(extra)
        return sorted(slots, key=_minute)

    # ---- L'argent -----------------------------------------------------
    def _claude_daily(self) -> float:
        """Dépense Claude moyenne par jour sur les 7 derniers jours complets (sinon aujourd'hui)."""
        days = (self.state.data.get("llm_usage") or {}).get("days") or {}
        today = now_local()
        past = [float((days.get((today - timedelta(days=i)).strftime("%Y-%m-%d")) or {}).get("usd", 0.0))
                for i in range(1, 8)]
        past = [v for v in past if v > 0]
        if past:
            return sum(past) / len(past)
        return float((days.get(today.strftime("%Y-%m-%d")) or {}).get("usd", 0.0))

    def _claude_cap(self) -> float:
        try:
            return max(0.0, float(getattr(self.settings, "claude_budget_monthly_usd", 0.0) or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _x_numbers(self) -> tuple[float, float]:
        """(dépense X moyenne par jour sur 7 jours, plafond du mois) ; (0, 0) si inconnu."""
        try:
            return float(self.budget.spent_last_days(7)) / 7.0, float(self.budget.monthly_usd)
        except Exception:  # noqa: BLE001
            return 0.0, 0.0

    def afford(self, extra_per_day: float) -> tuple[bool, str]:
        """Peut-on payer `extra_per_day` posts de plus par jour, tous les jours ? (oui/non, la
        raison en français). Une baisse est toujours permise."""
        if extra_per_day <= 0:
            return True, ""
        cap = self._claude_cap()
        daily = self._claude_daily()
        month = (daily + extra_per_day * POST_COST_USD) * 30
        if cap > 0 and month > cap * CLAUDE_SAFE_SHARE:
            return False, (f"Claude coûterait environ {month:.0f} $ par mois (aujourd'hui {daily * 30:.0f} $), "
                           f"au-dessus de {cap * CLAUDE_SAFE_SHARE:.0f} $, soit 80 % du plafond de {cap:.0f} $")
        x_daily, x_cap = self._x_numbers()
        x_month = (x_daily + extra_per_day * X_POST_COST_USD) * 30
        if x_cap > 0 and x_month > x_cap * X_SAFE_SHARE:
            return False, (f"l'API X coûterait environ {x_month:.0f} $ par mois, au-dessus de "
                           f"{x_cap * X_SAFE_SHARE:.0f} $, soit 90 % du plafond de {x_cap:.0f} $")
        return True, ""

    def sheet(self) -> str:
        """Pour l'agent d'analyse (anglais) : les limites en vigueur, ce qu'il peut choisir, ce
        que coûte un post de plus, où en est l'argent."""
        lo, hi = self.scheduled_range()
        cap, daily = self._claude_cap(), self._claude_daily()
        x_daily, x_cap = self._x_numbers()
        money = (f"the AI that writes the posts costs about ${daily * 30:.0f} a month at the pace of the last 7 "
                 f"days" + (f", for a cap of ${cap:.0f}" if cap > 0 else " (no cap set)"))
        if x_cap > 0:
            money += f"; X's API about ${x_daily * 30:.0f} a month for a cap of ${x_cap:.0f}"
        return "\n".join([
            "POSTING LIMITS — the owner lets you change them when the figures or the algorithm give a reason, "
            "and asks you to be careful with money:",
            f"- Scheduled posts per day: {self.target()[0]} now (you may choose {lo} to {hi}). One fewer drops the "
            "last news post of the day; one more adds a news post in the morning.",
            f"- Breaking-news posts per day, at most: {self.target()[1]} now (you may choose {REACTIVE_RANGE[0]} to "
            f"{REACTIVE_RANGE[1]}).",
            f"- One step at a time (at most {STEP} up or down each), and a change starts tomorrow.",
            f"- Money: one more post a day costs about ${POST_COST_USD * 30:.0f} a month; {money}. A raise the "
            "budget cannot carry is refused whatever you decide.",
        ])

    # ---- Décider ------------------------------------------------------
    def problem(self, scheduled: int, reactive: int) -> str | None:
        """Pourquoi ces nouvelles limites sont refusées par le code (en français), ou None."""
        if isinstance(scheduled, bool) or isinstance(reactive, bool) or not isinstance(scheduled, int) \
                or not isinstance(reactive, int):
            return "valeurs illisibles"
        lo, hi = self.scheduled_range()
        if not (lo <= scheduled <= hi):
            return f"posts prévus hors bornes ({lo} à {hi})"
        if not (REACTIVE_RANGE[0] <= reactive <= REACTIVE_RANGE[1]):
            return f"posts à chaud hors bornes ({REACTIVE_RANGE[0]} à {REACTIVE_RANGE[1]})"
        now_s, now_r = self.target()
        if abs(scheduled - now_s) > STEP or abs(reactive - now_r) > STEP:
            return "plus d'un cran à la fois"
        extra = max(0, scheduled - now_s) + max(0, reactive - now_r)
        ok, why = self.afford(extra)
        if not ok:
            return "le budget ne le permet pas : " + why
        return None

    def changed(self, scheduled: int, reactive: int) -> bool:
        return (scheduled, reactive) != self.target()

    def adopt(self, scheduled: int, reactive: int, why: str = "", fr: str = "") -> dict | None:
        """Note de nouvelles limites, applicables demain. None si rien ne change ou si le code
        les refuse (bornes, cran, budget)."""
        if not self.changed(scheduled, reactive):
            return None
        bad = self.problem(scheduled, reactive)
        if bad:
            log.info("Limites de posts : changement refusé (%s)", bad)
            return None
        now = now_local()
        before_s, before_r = self.target()
        row = {"from": (now + timedelta(days=1)).strftime("%Y-%m-%d"), "scheduled": int(scheduled),
               "reactive": int(reactive), "before": {"scheduled": before_s, "reactive": before_r},
               "why": " ".join(str(why or "").split())[:300], "fr": " ".join(str(fr or "").split())[:300],
               "decided_at": iso(now)}
        rows = [r for r in self._decisions() if r.get("from") != row["from"]] + [row]
        self.state.data["post_limits"] = rows[-KEEP:]
        self.state.flush()
        log.info("Limites de posts à partir du %s : %d post(s) prévu(s) par jour (avant : %d), %d à chaud au plus "
                 "(avant : %d) — %s", row["from"], row["scheduled"], row["before"]["scheduled"], row["reactive"],
                 row["before"]["reactive"], row["why"] or "sans raison notée")
        return row
