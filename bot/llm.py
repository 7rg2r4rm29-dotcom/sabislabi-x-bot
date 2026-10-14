"""Appels à Claude (SDK officiel `anthropic`).

- Sorties structurées (`output_config.format`) : la réponse est un JSON valide
  conforme au schéma — fini le JSON réparé à la regex qui avait publié
  '"text": ...' en septembre.
- Cache de prompt d'1 h sur les longues consignes système (≈ 90 % moins cher
  à la relecture).
- Bascule serveur en cas de refus (`fallbacks: "default"`) sur les modèles qui
  la proposent ; un refus final renvoie None, jamais un texte vide publié.
- Délais bornés : 150 s par essai, 2 nouvelles tentatives au plus.
"""
from __future__ import annotations

import json
import logging
from typing import Any

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


class LLM:
    def __init__(self, settings, state=None, client=None) -> None:
        self.settings = settings
        self.state = state
        self._client = client
        self._fallback_ok = settings.use_server_fallback

    # ------------------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(
                api_key=self.settings.anthropic_api_key,
                timeout=anthropic.Timeout(150.0, connect=10.0),
                max_retries=2,
            )
        return self._client

    def _request(self, kwargs: dict) -> Any:
        if self._fallback_ok:
            return self.client.beta.messages.create(
                betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        return self.client.messages.create(**kwargs)

    def _call(self, label: str, kwargs: dict) -> Any | None:
        import anthropic
        with heartbeat(f"claude {label}", every_s=60.0, max_total_s=480.0):
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
                log.error("Claude [%s] : requête invalide : %s", label, msg[:300])
                return None
            except anthropic.AuthenticationError:
                log.error("Claude [%s] : clé ANTHROPIC_API_KEY refusée", label)
                return None
            except anthropic.RateLimitError:
                log.warning("Claude [%s] : limite de débit, on réessaiera plus tard", label)
                return None
            except anthropic.APIStatusError as e:
                log.error("Claude [%s] : erreur API %s : %s", label, e.status_code, str(e)[:200])
                return None
            except anthropic.APIConnectionError as e:
                log.error("Claude [%s] : réseau indisponible : %s", label, e)
                return None

    # ------------------------------------------------------------------
    def _record_usage(self, label: str, resp: Any) -> None:
        if self.state is None:
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
        days = self.state.data.setdefault("llm_usage", {}).setdefault("days", {})
        day = days.setdefault(now_local().strftime("%Y-%m-%d"), {"calls": 0, "usd": 0.0})
        day["calls"] += 1
        day["usd"] = round(day["usd"] + cost, 5)
        for d in sorted(days)[:-70]:
            del days[d]
        self.state.flush()
        log.info("Claude [%s] %s : %d in (+%d cache) / %d out ≈ %.4f $", label, model, inp, cread, out, cost)

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
             effort: str | None = None, max_tokens: int = 16000) -> dict | None:
        """Retourne l'objet JSON produit par Claude, conforme à `schema`, ou None."""
        kwargs = {
            "model": self.settings.llm_model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            "messages": [{"role": "user", "content": user}],
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
