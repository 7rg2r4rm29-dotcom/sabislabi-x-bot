"""Appels à Claude (SDK officiel `anthropic`).

- Sorties structurées (`output_config.format`) : la réponse est un JSON valide
  conforme au schéma — fini le JSON réparé à la regex qui avait publié
  '"text": ...' en septembre.
- Cache de prompt d'1 h sur les longues consignes système (≈ 90 % moins cher
  à la relecture).
- Bascule serveur en cas de refus (`fallbacks: "default"`) sur les modèles qui
  la proposent ; un refus final renvoie None, jamais un texte vide publié.
- Délais bornés : 150 s par essai, 2 nouvelles tentatives au plus.
- Plafond du mois (CLAUDE_BUDGET_MONTHLY_USD, 35 $ par défaut) : le coût de chaque appel
  est compté ; au plafond, plus aucun appel jusqu'au 1er du mois. Avant, ce qui est
  optionnel (radar, posts « à chaud », réponses aux commentaires) s'arrête dès qu'il ne
  reste plus de quoi écrire les posts planifiés jusqu'à la fin du mois.
"""
from __future__ import annotations

import calendar
import json
import logging
from pathlib import Path
from typing import Any

from . import utils
from .utils import heartbeat, now_local

log = logging.getLogger("xbot.llm")

FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Prix en $ par million de tokens : (entrée, sortie, lecture cache, écriture cache 1 h)
MODEL_PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 8.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 10.0),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 4.0),
    "claude-sonnet-5": (2.0, 10.0, 0.20, 4.0),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.20),
}


class LLMError(RuntimeError):
    pass


# Coupe-circuit : après 2 pannes réseau / délais dépassés / surcharges de suite,
# on n'appelle plus Claude pendant 10 min (sinon chaque appel bloque la boucle
# plusieurs minutes et les publications validées attendent).
BREAKER_FAILURES = 2
BREAKER_PAUSE_S = 600
ACCOUNT_PAUSE_S = 900    # plus de crédit / clé refusée : on réessaie toutes les 15 min

# Plafond du mois : la part réservée aux posts planifiés (2 par jour, 2 threads par semaine,
# leurs relectures et leurs images), au prorata des jours qui restent. Le radar, les posts
# « à chaud » et les réponses aux commentaires ne tournent que s'il reste plus que cette réserve.
ESSENTIAL_SHARE = 0.75
BUDGET_WARN_SHARE = 0.8  # alerte Telegram, une fois par mois, à 80 % du plafond
# Ce que coûtent les essais faits à la main pendant que le bot tourne (--preview, --check,
# scripts de vérification) : ils n'écrivent jamais dans l'état du bot en service, mais ils
# comptent dans le même plafond. Le service lit ce fichier, il ne l'écrit jamais.
MANUAL_LEDGER = "llm_manual_usage.json"


def manual_ledger_path() -> Path:
    return utils.STATE_DIR / MANUAL_LEDGER


def _month_sum(days: Any, month: str) -> float:
    if not isinstance(days, dict):
        return 0.0
    total = 0.0
    for day, entry in days.items():
        if str(day).startswith(month) and isinstance(entry, dict):
            try:
                total += float(entry.get("usd") or 0.0)
            except (TypeError, ValueError):
                continue
    return total


def manual_spent_month(month: str | None = None) -> float:
    """Coût des essais faits à la main ce mois-ci (0 si le fichier n'existe pas)."""
    try:
        data = json.loads(manual_ledger_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    return _month_sum((data or {}).get("days") if isinstance(data, dict) else None,
                      month or now_local().strftime("%Y-%m"))


class LLM:
    def __init__(self, settings, state=None, client=None, manual: bool = False) -> None:
        self.settings = settings
        self.state = state
        self._client = client
        # `manual` : appels faits à la main (aperçu, vérification) sur un état jetable ; leur
        # coût va dans le fichier des essais, compté dans le plafond du mois.
        self.manual = bool(manual)
        self._fallback_ok = settings.use_server_fallback
        self._failures = 0
        self._paused_until = 0.0
        # Problème de compte Claude (crédit épuisé, clé refusée) : le bot le signale
        # sur Telegram et attend au lieu de sauter ou de remplir des créneaux.
        self._account_problem: str | None = None

    # ------------------------------------------------------------------
    # Plafond du mois
    # ------------------------------------------------------------------
    @property
    def monthly_budget(self) -> float:
        """Plafond Claude du mois en dollars ; 0 = pas de plafond."""
        try:
            return max(0.0, float(getattr(self.settings, "claude_budget_monthly_usd", 0.0) or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def spent_month(self) -> float:
        """Ce que Claude a coûté ce mois-ci : le bot, plus les essais faits à la main."""
        month = now_local().strftime("%Y-%m")
        own = 0.0
        if self.state is not None:
            # (Pour un essai à la main, l'état est une copie jetable de celui du service : ce
            # que le bot avait dépensé au moment de la copie.)
            own = _month_sum((self.state.data.get("llm_usage") or {}).get("days"), month)
        return own + manual_spent_month(month)

    def over_budget(self) -> bool:
        cap = self.monthly_budget
        return cap > 0 and self.spent_month() >= cap

    def budget_problem(self) -> str | None:
        """Le message du plafond atteint, ou None. Se lève tout seul le 1er du mois."""
        if not self.over_budget():
            return None
        return (f"plafond Claude du mois atteint ({self.spent_month():.2f} $ sur {self.monthly_budget:.0f} $) : "
                "reprise le 1er du mois (réglage CLAUDE_BUDGET_MONTHLY_USD)")

    def optional_ok(self) -> bool:
        """Radar, posts « à chaud », réponses aux commentaires : seulement s'il reste plus
        que la réserve des posts planifiés jusqu'à la fin du mois."""
        cap = self.monthly_budget
        if cap <= 0:
            return True
        now = now_local()
        days = calendar.monthrange(now.year, now.month)[1]
        left = days - now.day + 1                      # aujourd'hui compris
        return self.spent_month() + cap * ESSENTIAL_SHARE * left / days <= cap

    def budget_warning_due(self) -> bool:
        cap = self.monthly_budget
        return cap > 0 and self.spent_month() >= cap * BUDGET_WARN_SHARE

    @property
    def account_problem(self) -> str | None:
        """Ce qui empêche Claude d'écrire : le compte (crédit épuisé, clé refusée) ou le
        plafond du mois. None si tout va bien."""
        return self._account_problem or self.budget_problem()

    # ------------------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            import anthropic
            # Réflexion + 16 000 jetons sans streaming : une réponse peut prendre
            # plusieurs minutes (l'API compte jusqu'à ~450 s). 150 s coupait des
            # réponses valides, refacturées à chaque nouvel essai.
            self._client = anthropic.Anthropic(
                api_key=self.settings.anthropic_api_key,
                timeout=anthropic.Timeout(420.0, connect=10.0),
                max_retries=1,
            )
        return self._client

    def _request(self, kwargs: dict) -> Any:
        if self._fallback_ok:
            return self.client.beta.messages.create(
                betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        return self.client.messages.create(**kwargs)

    def _call(self, label: str, kwargs: dict) -> Any | None:
        import time

        import anthropic
        if self.over_budget():
            log.info("Claude [%s] : plafond du mois atteint (%.2f $ sur %.0f $), appel sauté", label,
                     self.spent_month(), self.monthly_budget)
            return None
        if time.monotonic() < self._paused_until:
            log.info("Claude [%s] : en pause après des pannes répétées, appel sauté", label)
            return None
        resp = self._call_once(label, kwargs, anthropic)
        if resp is None and self._last_was_outage:
            self._failures += 1
            if self._failures >= BREAKER_FAILURES:
                self._paused_until = time.monotonic() + BREAKER_PAUSE_S
                self._failures = 0
                log.error("Claude : %d pannes de suite, plus d'appel pendant %d min", BREAKER_FAILURES,
                          BREAKER_PAUSE_S // 60)
        elif resp is not None:
            self._failures = 0
            self._account_problem = None
        if resp is None and self._account_problem:
            self._paused_until = time.monotonic() + ACCOUNT_PAUSE_S
        return resp

    _last_was_outage = False

    def paused(self) -> bool:
        """Claude n'est pas appelé en ce moment : coupe-circuit ouvert, ou plafond du mois atteint."""
        import time
        return time.monotonic() < self._paused_until or self.over_budget()

    def _call_once(self, label: str, kwargs: dict, anthropic) -> Any | None:
        self._last_was_outage = False
        with heartbeat(f"claude {label}", every_s=60.0, max_total_s=900.0):
            try:
                return self._request(kwargs)
            except anthropic.BadRequestError as e:
                msg = str(getattr(e, "message", e))
                if self._fallback_ok and ("fallback" in msg.lower() or FALLBACK_BETA in msg):
                    log.warning("Claude [%s] : bascule serveur refusée (%s), on continue sans", label, msg[:160])
                    self._fallback_ok = False
                    try:
                        return self._request(kwargs)
                    except Exception as e2:  # noqa: BLE001
                        log.error("Claude [%s] : échec sans bascule : %s", label, e2)
                        return None
                if "credit balance" in msg.lower():
                    self._account_problem = "plus de crédit sur ton compte Claude (console.anthropic.com → Billing)"
                log.error("Claude [%s] : requête invalide : %s", label, msg[:300])
                return None
            except anthropic.AuthenticationError:
                self._account_problem = "clé ANTHROPIC_API_KEY refusée (console.anthropic.com → API Keys)"
                log.error("Claude [%s] : clé ANTHROPIC_API_KEY refusée", label)
                return None
            except anthropic.RateLimitError:
                log.warning("Claude [%s] : limite de débit, on réessaiera plus tard", label)
                return None
            except anthropic.APIStatusError as e:
                self._last_was_outage = int(getattr(e, "status_code", 0) or 0) >= 500
                log.error("Claude [%s] : erreur API %s : %s", label, e.status_code, str(e)[:200])
                return None
            except anthropic.APIConnectionError as e:  # dont APITimeoutError
                self._last_was_outage = True
                log.error("Claude [%s] : réseau indisponible ou délai dépassé : %s", label, type(e).__name__)
                return None

    # ------------------------------------------------------------------
    def _record_usage(self, label: str, resp: Any) -> None:
        if self.state is None and not self.manual:
            return
        usage = getattr(resp, "usage", None)
        if usage is None:
            return
        model = getattr(resp, "model", None) or self.settings.llm_model
        inp = int(getattr(usage, "input_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or 0)
        cread = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cwrite = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        p_in, p_out, p_cr, p_cw = MODEL_PRICES.get(str(model), MODEL_PRICES["claude-opus-5-5"])
        cost = (inp * p_in + out * p_out + cread * p_cr + cwrite * p_cw) / 1_000_000
        today = now_local().strftime("%Y-%m-%d")
        if self.manual:
            self._record_manual(today, cost)
        else:
            days = self.state.data.setdefault("llm_usage", {}).setdefault("days", {})
            day = days.setdefault(today, {"calls": 0, "usd": 0.0})
            day["calls"] += 1
            day["usd"] = round(day["usd"] + cost, 5)
            for d in sorted(days)[:-70]:
                del days[d]
            self.state.flush()
        log.info("Claude [%s] %s : %d in (+%d cache) / %d out ≈ %.4f $", label, model, inp, cread, out, cost)

    @staticmethod
    def _record_manual(today: str, cost: float) -> None:
        """Ajoute le coût d'un essai fait à la main au fichier lu par le service (jamais
        son état : il le réécrirait par-dessus). Une écriture ratée n'arrête rien."""
        path = manual_ledger_path()
        try:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict) or not isinstance(data.get("days"), dict):
                data = {"days": {}}
            days = data["days"]
            day = days.get(today)
            if not isinstance(day, dict):
                day = days[today] = {"calls": 0, "usd": 0.0}
            day["calls"] = int(day.get("calls") or 0) + 1
            day["usd"] = round(float(day.get("usd") or 0.0) + cost, 5)
            for d in sorted(days)[:-70]:
                del days[d]
            utils.save_json(path, data)
        except Exception as e:  # noqa: BLE001
            log.warning("Essais à la main : coût non noté (%s)", type(e).__name__)

    @staticmethod
    def _first_text(resp: Any) -> str:
        for block in getattr(resp, "content", None) or []:
            if getattr(block, "type", None) == "text":
                return getattr(block, "text", "") or ""
        return ""

    def _check_stop(self, label: str, resp: Any) -> bool:
        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            details = getattr(resp, "stop_details", None)
            cat = getattr(details, "category", None) if details else None
            log.warning("Claude [%s] : refus (catégorie %s), rien ne sera publié", label, cat)
            return False
        if stop == "max_tokens":
            log.warning("Claude [%s] : réponse tronquée (max_tokens), ignorée", label)
            return False
        return True

    # ------------------------------------------------------------------
    def json(self, label: str, system: str, user: str, schema: dict,
             effort: str | None = None, max_tokens: int = 16000,
             images: list | None = None) -> dict | None:
        """Retourne l'objet JSON produit par Claude, conforme à `schema`, ou None.
        `images` : chemins d'images (PNG/JPEG) que Claude doit regarder."""
        content: Any = user
        if images:
            content = [_image_block(p) for p in images] + [{"type": "text", "text": user}]
            if any(b is None for b in content):
                log.warning("Claude [%s] : image illisible", label)
                return None
        kwargs = {
            "model": self.settings.llm_model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            "messages": [{"role": "user", "content": content}],
            "output_config": {"effort": effort or self.settings.llm_effort,
                              "format": {"type": "json_schema", "schema": schema}},
        }
        resp = self._call(label, kwargs)
        if resp is None:
            return None
        self._record_usage(label, resp)
        if not self._check_stop(label, resp):
            return None
        raw = self._first_text(resp)
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            from .filters import parse_json_object
            obj = parse_json_object(raw)
            if obj is None:
                log.warning("Claude [%s] : JSON illisible : %r", label, (raw or "")[:160])
                return None
        if not isinstance(obj, dict):
            return None
        return obj


def _image_block(path) -> dict | None:
    """Image envoyée à Claude : JPEG ≤ 1600 px de large (léger, bien lisible)."""
    import base64
    import io
    try:
        from PIL import Image
        img = Image.open(path).convert("RGB")
        if img.width > 1600:
            img = img.resize((1600, int(img.height * 1600 / img.width)))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        data = buf.getvalue()
    except Exception:  # noqa: BLE001
        return None
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(data).decode("ascii")}}


def schema(properties: dict[str, dict], required: list[str] | None = None) -> dict:
    """Petit utilitaire : objet JSON strict (additionalProperties false)."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(required if required is not None else properties.keys()),
        "additionalProperties": False,
    }


STR = {"type": "string"}
BOOL = {"type": "boolean"}
INT = {"type": "integer"}
STR_LIST = {"type": "array", "items": {"type": "string"}}
INT_LIST = {"type": "array", "items": {"type": "integer"}}
