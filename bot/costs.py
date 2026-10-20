"""Suivi des dépenses API X (pay-per-use) et garde-fous de budget.

Tarifs officiels (docs.x.com/x-api/getting-started/pricing, oct. 2026) :
  post créé 0,015 $ ; post contenant un lien 0,20 $ ; réponse « summoned » 0,01 $
  lecture d'un post 0,005 $ ; lecture d'un compte 0,010 $ ; lecture de ses propres
  données (« owned read ») 0,001 $ ; métadonnées média 0,005 $.
X déduplique les lectures d'une même ressource sur une journée UTC : notre
estimation est donc un plafond prudent (on compte chaque lecture).

Deux niveaux :
  - essentiel (publier ce que tu as validé, lire tes mentions et tes posts) :
    bloqué seulement par le plafond MENSUEL ;
  - optionnel (radar, lecture des comptes d'actu) : au plus 75 % du plafond
    mensuel, réparti jour par jour (et jamais plus de 80 % du plafond du jour).
    Le dernier quart du mois reste ainsi TOUJOURS disponible pour publier.
"""
from __future__ import annotations

import calendar
import logging

from .utils import now_local

log = logging.getLogger("xbot.costs")

PRICES = {
    "post": 0.015,
    "post_url": 0.20,
    "reply_summoned": 0.010,
    "post_read": 0.005,
    "user_read": 0.010,
    "owned_read": 0.001,
    "media": 0.005,
}

OPTIONAL_DAILY_SHARE = 0.8
OPTIONAL_MONTHLY_SHARE = 0.75


class Budget:
    def __init__(self, state, daily_usd: float, monthly_usd: float) -> None:
        self.state = state
        self.daily_usd = daily_usd
        self.monthly_usd = monthly_usd
        self.state.data.setdefault("costs", {"days": {}})

    # ------------------------------------------------------------------
    def _day_key(self) -> str:
        return now_local().strftime("%Y-%m-%d")

    def record(self, kind: str, units: int = 1) -> float:
        if units <= 0:
            return 0.0
        price = PRICES[kind]
        amount = price * units
        days = self.state.data["costs"].setdefault("days", {})
        day = days.setdefault(self._day_key(), {"total": 0.0, "by_kind": {}})
        day["total"] = round(day["total"] + amount, 6)
        day["by_kind"][kind] = day["by_kind"].get(kind, 0) + units
        # On ne garde que 70 jours
        for d in sorted(days)[:-70]:
            del days[d]
        self.state.flush()
        return amount

    def spent_today(self) -> float:
        day = self.state.data["costs"].get("days", {}).get(self._day_key())
        return float(day["total"]) if day else 0.0

    def spent_month(self) -> float:
        prefix = now_local().strftime("%Y-%m")
        return float(sum(v.get("total", 0.0) for d, v in self.state.data["costs"].get("days", {}).items()
                         if d.startswith(prefix)))

    def spent_last_days(self, n: int) -> float:
        days = sorted(self.state.data["costs"].get("days", {}).items())[-n:]
        return float(sum(v.get("total", 0.0) for _, v in days))

    # ------------------------------------------------------------------
    def can_spend_essential(self, estimate: float = 0.0) -> bool:
        ok = self.spent_month() + estimate <= self.monthly_usd
        if not ok:
            log.warning("Budget MENSUEL API X atteint (%.2f $ / %.2f $)", self.spent_month(), self.monthly_usd)
        return ok

    def optional_daily_limit(self) -> float:
        now = now_local()
        days = calendar.monthrange(now.year, now.month)[1]
        return min(self.daily_usd * OPTIONAL_DAILY_SHARE, self.monthly_usd * OPTIONAL_MONTHLY_SHARE / days)

    def can_spend_optional(self, estimate: float = 0.0) -> bool:
        if not self.can_spend_essential(estimate):
            return False
        month_limit = self.monthly_usd * OPTIONAL_MONTHLY_SHARE
        if self.spent_month() + estimate > month_limit:
            log.info("Lectures optionnelles en pause jusqu'au mois prochain (%.2f $ / %.2f $) : "
                     "le reste est réservé aux publications", self.spent_month(), month_limit)
            return False
        limit = self.optional_daily_limit()
        ok = self.spent_today() + estimate <= limit
        if not ok:
            log.info("Budget du jour pour les lectures optionnelles atteint (%.2f $ / %.2f $)",
                     self.spent_today(), limit)
        return ok

    def summary(self) -> str:
        return (f"API X : {self.spent_today():.2f} $ aujourd'hui, {self.spent_month():.2f} $ ce mois "
                f"(plafond {self.monthly_usd:.0f} $)")
