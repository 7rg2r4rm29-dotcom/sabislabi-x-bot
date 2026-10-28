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


# Coupe-circuit : après 2 pannes réseau / délais dépassés / surcharges de suite,
# on n'appelle plus Claude pendant 10 min (sinon chaque appel bloque la boucle
# plusieurs minutes et les publications validées attendent).
BREAKER_FAILURES = 2
BREAKER_PAUSE_S = 600
ACCOUNT_PAUSE_S = 900    # plus de crédit / clé refusée : on réessaie toutes les 15 min


class LLM:
    def __init__(self, settings, state=None, client=None) -> None:
        self.settings = settings
        self.state = state
        self._client = client
        self._fallback_ok = settings.use_server_fallback
        self._failures = 0
        self._paused_until = 0.0
        # Problème de compte Claude (crédit épuisé, clé refusée) : le bot le signale
        # sur Telegram et attend au lieu de sauter ou de remplir des créneaux.
        self.account_problem: str | None = None

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
            self.account_problem = None
        if resp is None and self.account_problem:
            self._paused_until = time.monotonic() + ACCOUNT_PAUSE_S
        return resp

    _last_was_outage = False

    def paused(self) -> bool:
        """Coupe-circuit ouvert : Claude n'est pas appelé en ce moment."""
        import time
        return time.monotonic() < self._paused_until

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
                    self.account_problem = "plus de crédit sur ton compte Claude (console.anthropic.com → Billing)"
                log.error("Claude [%s] : requête invalide : %s", label, msg[:300])
                return None
            except anthropic.AuthenticationError:
                self.account_problem = "clé ANTHROPIC_API_KEY refusée (console.anthropic.com → API Keys)"
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
