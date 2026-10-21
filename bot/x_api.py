"""Client minimal de l'API X v2 OFFICIELLE (OAuth 1.0a, contexte utilisateur).

Remplace entièrement l'ancien pilotage du site x.com par navigateur (interdit :
« Non-API automation (scraping, browser automation) results in permanent
suspension », docs.x.com/developer-guidelines).

Ce que l'API pay-per-use permet encore (oct. 2026) et que le bot utilise :
  - publier un post / un thread / un sondage, avec image ;
  - répondre à quelqu'un qui t'a « convoqué » (il t'a mentionné ou a répondu
    à ton post) — les autres réponses sont refusées par l'API depuis fév. 2026 ;
  - lire tes mentions et tes propres posts (« owned reads », 0,001 $) ;
  - lire les posts récents de comptes publics (0,005 $ le post).
Likes, follows et citations ne sont plus disponibles en self-serve : le bot
n'en fait pas.
"""
from __future__ import annotations

import html
import logging
import re
import time
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("xbot.x_api")

API = "https://api.x.com"
# X transforme en lien tout nom de domaine sur un vrai TLD (« bolt.new »,
# « labs.google », « t3.chat »…) et un post avec lien coûte 0,20 $ au lieu de
# 0,015 $ : la détection est volontairement large (un faux positif fait juste
# réécrire le texte).
_TLDS = (
    "com|net|org|io|ai|co|dev|app|new|google|xyz|tech|chat|studio|page|so|me|gg|tv|video|tools|site|"
    "online|store|shop|blog|news|im|sh|fm|ly|to|art|design|run|cloud|inc|world|life|live|space|info|biz|"
    "pro|one|plus|zone|wiki|link|click|top|vip|website|today|media|global|group|company|academy|ventures|"
    "capital|fund|finance|money|codes|build|agency|digital|systems|works|network|work|email|cafe|guru|"
    "ninja|rocks|social|team|bot|science|ink|mobi|asia|games|game|watch|stream|exchange|market|trade|club|"
    "land|city|place|lol|wtf|fyi|edu|gov|us|uk|de|fr|ch|cn|jp|ca|eu|be|cc|in|it|es|nl|se|fi|dk|pl|pt|ru|"
    "br|mx|ar|au|nz|kr|tw|hk|sg|vn|th|ph|za|ng|ke|ma|tn|il|ae|tr|gr|cz|at|ie|lu|li|is|ws|la|nu|sx|ac|gl|pw"
)
URL_RE = re.compile(
    r"https?://\S+|\bwww\.\S+"
    r"|\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:" + _TLDS + r")\b(?![-.]?[a-z0-9])"
    r"|\b[a-z0-9][a-z0-9-]*\.[a-z]{2,}/\S",
    re.I,
)

TWEET_FIELDS = "created_at,author_id,conversation_id,in_reply_to_user_id,referenced_tweets,public_metrics,lang"


class XError(RuntimeError):
    """Erreur API X. `kind` : auth | credits | rate_limited | not_summoned |
    duplicate | forbidden | not_found | invalid | server | network | budget | dry_run |
    uncertain (écriture envoyée mais réponse perdue : le post est PEUT-ÊTRE en ligne)"""

    def __init__(self, kind: str, message: str, status: int | None = None, reset_at: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.reset_at = reset_at


def contains_url(text: str) -> bool:
    return bool(URL_RE.search(text or ""))


class XClient:
    def __init__(self, settings, budget, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.budget = budget
        self.dry_run = settings.dry_run
        self._session = session
        self._dry_counter = 0
        self._user_id = settings.x_user_id or None

    # ------------------------------------------------------------------
    @property
    def session(self) -> requests.Session:
        if self._session is None:
            from requests_oauthlib import OAuth1
            s = requests.Session()
            s.auth = OAuth1(
                self.settings.x_api_key, self.settings.x_api_secret,
                self.settings.x_access_token, self.settings.x_access_token_secret,
            )
            s.headers["User-Agent"] = "sabislabi-x-assistant/2.0"
            self._session = s
        return self._session

    @property
    def configured(self) -> bool:
        return self.settings.x_credentials_ok()

    def _classify(self, resp: requests.Response) -> XError:
        status = resp.status_code
        try:
            body = resp.json()
        except ValueError:
            body = {}
        detail = (body.get("detail") or body.get("title") or "")
        if not detail and body.get("errors"):
            detail = "; ".join(str(e.get("message") or e.get("detail") or e) for e in body["errors"])[:300]
        detail = detail or resp.text[:300]
        low = detail.lower()
        reset = resp.headers.get("x-rate-limit-reset")
        if status == 429:
            return XError("rate_limited", detail, status, float(reset) if reset else None)
        if status == 401:
            return XError("auth", detail, status)
        if status == 402 or "credit" in low or "payment" in low or "spend" in low:
            return XError("credits", detail, status)
        if status == 403 and ("summon" in low or "mentioned" in low or "engaged" in low
                              or "not allowed to reply" in low or "reply" in low and "restrict" in low):
            return XError("not_summoned", detail, status)
        if status == 403 and "duplicate" in low:
            return XError("duplicate", detail, status)
        if status == 403:
            return XError("forbidden", detail, status)
        if status == 404:
            return XError("not_found", detail, status)
        if 400 <= status < 500:
            return XError("invalid", detail, status)
        return XError("server", detail, status)

    def _req(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None,
             files: dict | None = None, data: dict | None = None, retry: bool | None = None) -> dict:
        """Une écriture n'est JAMAIS renvoyée à l'aveugle (sauf `retry=True`) : si X a
        pu la recevoir (délai dépassé, coupure, erreur 5xx), XError("uncertain") et le
        Publisher vérifie sur le profil avant de republier (sinon : doublon)."""
        if not self.configured:
            raise XError("auth", "Clés API X absentes")
        if retry is None:
            retry = method.upper() == "GET"
        url = f"{API}{path}"
        last_exc: Exception | None = None
        for attempt in (1, 2):
            try:
                resp = self.session.request(method, url, params=params, json=json_body, files=files,
                                            data=data, timeout=(10, 45))
            except requests.ConnectTimeout as e:  # connexion jamais établie : rien n'est parti
                last_exc = e
                log.warning("API X %s %s : connexion impossible, essai %d/2", method, path, attempt)
                time.sleep(3)
                continue
            except requests.RequestException as e:
                last_exc = e
                if not retry:
                    raise XError("uncertain", f"réponse de X perdue ({type(e).__name__})") from e
                log.warning("API X %s %s : réseau (%s), essai %d/2", method, path, type(e).__name__, attempt)
                time.sleep(3)
                continue
            if resp.status_code >= 500 and not retry:
                raise XError("uncertain", f"HTTP {resp.status_code} de X", resp.status_code)
            if resp.status_code >= 500 and attempt == 1:
                log.warning("API X %s %s : HTTP %s, nouvel essai", method, path, resp.status_code)
                time.sleep(3)
                continue
            if resp.status_code >= 400:
                raise self._classify(resp)
            try:
                return resp.json() if resp.content else {}
            except ValueError:
                return {}
        raise XError("network", f"Réseau indisponible : {last_exc}")

    # ------------------------------------------------------------------
    # Identité
    # ------------------------------------------------------------------
    def user_id(self) -> str:
        if self._user_id:
            return self._user_id
        if self.dry_run and not self.configured:
            return "0"
        data = self._req("GET", "/2/users/me", params={"user.fields": "username"})
        self.budget.record("user_read")
        self._user_id = str(data["data"]["id"])
        return self._user_id

    def me(self) -> dict:
        data = self._req("GET", "/2/users/me", params={"user.fields": "username,public_metrics,verified"})
        self.budget.record("user_read")
        return data.get("data") or {}

    # ------------------------------------------------------------------
    # Écriture
    # ------------------------------------------------------------------
    def _dry_id(self) -> str:
        self._dry_counter += 1
        return f"dry-{int(time.time())}-{self._dry_counter}"

    def upload_image(self, path: Path) -> str:
        if self.dry_run:
            log.info("[DRY_RUN] upload image %s", path)
            return self._dry_id()
        if not self.budget.can_spend_essential(0.01):
            raise XError("budget", "Budget mensuel API X atteint")
        with open(path, "rb") as f:
            data = self._req("POST", "/2/media/upload", files={"media": f},
                             data={"media_category": "tweet_image"}, retry=True)  # sans effet visible
        self.budget.record("media")
        media_id = str((data.get("data") or {}).get("id") or "")
        if not media_id:
            raise XError("invalid", f"Upload sans identifiant : {str(data)[:200]}")
        return media_id

    def post(self, text: str, *, reply_to: str | None = None, media_ids: list[str] | None = None,
             poll: dict | None = None, made_with_ai: bool = False) -> str:
        """Publie et retourne l'id du post. Lève XError en cas d'échec."""
        price_kind = "post_url" if contains_url(text) else ("reply_summoned" if reply_to else "post")
        if self.dry_run:
            log.info("[DRY_RUN] post%s : %s", f" (réponse à {reply_to})" if reply_to else "", text)
            return self._dry_id()
        if not self.budget.can_spend_essential(0.2):
            raise XError("budget", "Budget mensuel API X atteint")
        body: dict[str, Any] = {"text": text}
        if reply_to:
            body["reply"] = {"in_reply_to_tweet_id": str(reply_to)}
        if media_ids:
            body["media"] = {"media_ids": [str(m) for m in media_ids][:4]}
        if poll:
            body["poll"] = {"options": list(poll["options"])[:4],
                            "duration_minutes": int(poll.get("duration_minutes", 1440))}
        if made_with_ai:
            body["made_with_ai"] = True
        data = self._req("POST", "/2/tweets", json_body=body)
        self.budget.record(price_kind)
        tweet_id = str((data.get("data") or {}).get("id") or "")
        if not tweet_id:
            raise XError("invalid", f"Réponse sans id : {str(data)[:200]}")
        return tweet_id

    def post_thread(self, texts: list[str], media_ids_first: list[str] | None = None,
                    made_with_ai: bool = False) -> list[str]:
        """Publie un thread. Si un tweet échoue en cours de route, lève XError
        avec `partial_ids` (les ids déjà publiés) pour reprendre sans doublon."""
        ids: list[str] = []
        for i, text in enumerate(texts):
            try:
                tid = self.post(text, reply_to=ids[-1] if ids else None,
                                media_ids=media_ids_first if i == 0 else None,
                                made_with_ai=made_with_ai and i == 0)
            except XError as e:
                e.partial_ids = list(ids)  # type: ignore[attr-defined]
                raise
            ids.append(tid)
        return ids

    # ------------------------------------------------------------------
    # Lecture
    # ------------------------------------------------------------------
    def mentions(self, since_id: str | None = None, max_results: int = 50,
                 max_pages: int = 4) -> tuple[list[dict], dict, str | None]:
        """Mentions de mon compte (dont les réponses à mes posts). Owned read.
        Suit la pagination (une nuit chargée dépasse vite 50 mentions).
        Retourne (posts, {author_id: user}, newest_id)."""
        if not self.configured:
            return [], {}, since_id
        params = {
            "max_results": max(5, min(100, max_results)),
            "tweet.fields": TWEET_FIELDS,
            "expansions": "author_id",
            "user.fields": "username,name,verified,public_metrics",
        }
        if since_id:
            params["since_id"] = since_id
        tweets: list[dict] = []
        users: dict = {}
        newest = since_id
        for page in range(max(1, max_pages)):
            data = self._req("GET", f"/2/users/{self.user_id()}/mentions", params=params)
            batch = data.get("data") or []
            tweets.extend(batch)
            users.update({u["id"]: u for u in (data.get("includes") or {}).get("users", [])})
            self.budget.record("owned_read", len(batch))
            meta = data.get("meta") or {}
            if page == 0:
                newest = meta.get("newest_id") or since_id
            token = meta.get("next_token")
            if not token or not batch:
                break
            params = dict(params, pagination_token=token)
        else:
            log.info("Mentions : plus de %d pages d'un coup, les plus anciennes sont ignorées", max_pages)
        return tweets, users, newest

    def my_recent_posts(self, max_results: int = 50) -> list[dict]:
        """Mes posts récents (sans mes réponses) avec leurs statistiques. Owned read."""
        if not self.configured:
            return []
        params = {"max_results": max(5, min(100, max_results)), "tweet.fields": TWEET_FIELDS,
                  "exclude": "retweets,replies"}
        data = self._req("GET", f"/2/users/{self.user_id()}/tweets", params=params)
        tweets = data.get("data") or []
        self.budget.record("owned_read", len(tweets))
        return tweets

    def recent_own_tweets(self, max_results: int = 20) -> list[dict]:
        """Mes derniers tweets, réponses comprises (vérifier si une publication
        incertaine est en ligne). Owned read."""
        if not self.configured:
            return []
        params = {"max_results": max(5, min(100, max_results)), "tweet.fields": TWEET_FIELDS,
                  "exclude": "retweets"}
        data = self._req("GET", f"/2/users/{self.user_id()}/tweets", params=params)
        tweets = data.get("data") or []
        self.budget.record("owned_read", len(tweets))
        return tweets

    def user_posts(self, user_id: str, since_id: str | None = None, max_results: int = 5,
                   optional: bool = True) -> tuple[list[dict], str | None]:
        """Derniers posts originaux d'un compte public (0,005 $ le post lu)."""
        if not self.configured:
            return [], since_id
        est = 0.005 * max_results
        if optional and not self.budget.can_spend_optional(est):
            raise XError("budget", "Budget du jour pour les lectures atteint")
        params = {"max_results": max(5, min(100, max_results)), "tweet.fields": TWEET_FIELDS,
                  "exclude": "retweets,replies"}
        if since_id:
            params["since_id"] = since_id
        data = self._req("GET", f"/2/users/{user_id}/tweets", params=params)
        tweets = data.get("data") or []
        self.budget.record("post_read", len(tweets))
        newest = (data.get("meta") or {}).get("newest_id") or since_id
        return tweets, newest

    def user_ids(self, usernames: list[str]) -> dict[str, str]:
        """{handle_minuscule: id} — 0,010 $ par compte, à mettre en cache."""
        out: dict[str, str] = {}
        if not self.configured or not usernames:
            return out
        for i in range(0, len(usernames), 100):
            chunk = usernames[i:i + 100]
            if not self.budget.can_spend_optional(0.01 * len(chunk)):
                break
            data = self._req("GET", "/2/users/by", params={"usernames": ",".join(chunk)})
            users = data.get("data") or []
            self.budget.record("user_read", len(users))
            for u in users:
                out[str(u["username"]).lower()] = str(u["id"])
        return out


def tweet_text(tw: dict) -> str:
    """Texte d'un tweet tel que l'API le renvoie, sans les entités HTML (&amp;…)."""
    return html.unescape(str((tw or {}).get("text") or ""))


def intent_reply_url(tweet_id: str, text: str) -> str:
    """Lien qui ouvre l'app X avec la réponse pré-remplie : c'est TOI qui
    appuies sur « Répondre » (aucune automatisation)."""
    from urllib.parse import quote
    return f"https://x.com/intent/post?in_reply_to={tweet_id}&text={quote(text)}"


def intent_post_url(text: str) -> str:
    from urllib.parse import quote
    return f"https://x.com/intent/post?text={quote(text)}"


def tweet_url(handle: str, tweet_id: str) -> str:
    return f"https://x.com/{handle or 'i'}/status/{tweet_id}"
