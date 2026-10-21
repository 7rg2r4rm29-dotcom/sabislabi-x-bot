"""Radar : repère les posts tout frais des gros comptes IA/tech qui décollent et
te propose une réponse à publier TOI-MÊME.

Rien n'est jamais publié par l'API ici (X refuse les réponses non « summoned »
depuis le 23/02/2026, et répondre automatiquement sous les posts des autres est
interdit). La carte Telegram porte un lien `x.com/intent/post?in_reply_to=…`
qui ouvre l'app X avec la réponse pré-remplie : c'est toi qui appuies.

Pourquoi viser les posts JEUNES qui montent vite : les premières réponses
utiles sous un post qui part sont vues par beaucoup de monde. Sous les comptes
de plus de 250k abonnés, Grok note chaque réponse (0-3) : une réponse
générique est étiquetée et enterrée. D'où un filtrage strict, au plus 2
suggestions par passage et une seule par compte et par jour.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from . import filters
from .utils import in_window, now_local, today_str
from .x_api import XError, tweet_text, tweet_url

log = logging.getLogger("xbot.radar")

SUGGESTION_TTL = timedelta(minutes=90)
MAX_SUGGESTIONS_PER_CHECK = 2
PRIORITY_ACCOUNTS = 10          # les 10 premiers comptes de la liste
PRIORITY_MAX_AGE_MIN = 20.0     # … passent sans seuil de vitesse s'ils ont < 20 min
MIN_TEXT_LEN = 20
MIN_AGE_FOR_RATE = 2.0          # évite qu'un post de 30 s avec 3 likes fasse « 6/min »
USER_IDS_CHUNK = 100
# Erreurs qui touchent tous les comptes : on arrête de lire pour ce passage.
STOP_KINDS = {"budget", "rate_limited", "auth", "credits"}

_URL_RE = re.compile(r"https?://\S+")


def parse_created(value: str | None) -> datetime | None:
    """'2026-10-15T08:00:00.000Z' -> datetime aware (UTC)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt


class Radar:
    def __init__(self, settings, state, x, writer, approvals) -> None:
        self.settings = settings
        self.state = state
        self.x = x
        self.writer = writer
        self.approvals = approvals

    # ------------------------------------------------------------------
    def check(self) -> int:
        """Nombre de suggestions créées. Ne lève jamais d'exception."""
        try:
            return self._check()
        except Exception as e:  # noqa: BLE001
            log.exception("Radar : erreur inattendue : %s", e)
            return 0

    # ------------------------------------------------------------------
    @property
    def _rd(self) -> dict:
        rd = self.state.data.setdefault("radar", {})
        rd.setdefault("user_ids", {})
        rd.setdefault("since_ids", {})
        rd.setdefault("rotation", 0)
        return rd

    def _accounts(self) -> list[str]:
        me = (self.settings.handle or "").lower()
        out, seen = [], set()
        for h in self.settings.radar_accounts or []:
            h = str(h or "").strip().lstrip("@")
            if h and h.lower() != me and h.lower() not in seen:
                seen.add(h.lower())
                out.append(h)
        return out

    def _due(self) -> bool:
        s = self.settings
        if not s.radar_enabled or self.state.data.get("paused"):
            return False
        if not in_window(now_local().time(), s.radar_window):
            return False
        since = self.state.minutes_since("radar")
        if since is not None and since < s.radar_check_every_min:
            return False
        if self.state.count("radar_suggestions") >= s.radar_max_per_day:
            return False
        if not getattr(self.x, "configured", False):
            return False
        return bool(self.settings.radar_accounts)

    def _done_today(self) -> list[str]:
        done = self._rd.setdefault("done_today", {})
        today = today_str()
        for d in [d for d in done if d != today]:
            del done[d]
        return done.setdefault(today, [])

    # ------------------------------------------------------------------
    def _resolve_ids(self, accounts: list[str]) -> None:
        """Résout (et met en cache) les ids manquants. Un compte introuvable
        n'est retenté qu'une fois par jour."""
        rd = self._rd
        ids: dict = rd["user_ids"]
        failed: dict = rd.setdefault("unresolved", {})
        today = today_str()
        missing = [h for h in accounts if h.lower() not in ids and failed.get(h.lower()) != today]
        if not missing:
            return
        for i in range(0, len(missing), USER_IDS_CHUNK):
            chunk = missing[i:i + USER_IDS_CHUNK]
            try:
                got = self.x.user_ids(chunk) or {}
            except XError as e:
                log.warning("Radar : résolution des comptes impossible (%s) : %s", e.kind, e)
                break
            for h in chunk:
                uid = got.get(h.lower())
                if uid:
                    ids[h.lower()] = str(uid)
                    failed.pop(h.lower(), None)
                else:
                    failed[h.lower()] = today
            log.info("Radar : %d/%d compte(s) résolu(s)", sum(1 for h in chunk if h.lower() in got), len(chunk))
        self.state.flush()

    def _pick(self, accounts: list[str], done: list[str]) -> list[str]:
        """Les prochains comptes de la rotation, hors comptes déjà servis
        aujourd'hui et comptes sans id."""
        rd = self._rd
        n = len(accounts)
        if n == 0:
            return []
        start = int(rd.get("rotation") or 0) % n
        want = max(1, int(self.settings.radar_accounts_per_check))
        done_set = {d.lower() for d in done}
        picked: list[str] = []
        consumed = 0
        while len(picked) < want and consumed < n:
            h = accounts[(start + consumed) % n]
            consumed += 1
            if h.lower() in done_set or h.lower() not in rd["user_ids"]:
                continue
            picked.append(h)
        rd["rotation"] = (start + consumed) % n
        self.state.flush()
        return picked

    # ------------------------------------------------------------------
    def _rejection(self, tw: dict, handle: str, priority: set[str], now: datetime) -> tuple[str | None, float, float]:
        """(raison du refus ou None, âge en minutes, likes/min)."""
        created = parse_created(tw.get("created_at"))
        if created is None:
            return "sans date", 0.0, 0.0
        age = max(0.0, (now - created).total_seconds() / 60.0)
        likes = int((tw.get("public_metrics") or {}).get("like_count") or 0)
        rate = likes / max(age, MIN_AGE_FOR_RATE)
        if age >= self.settings.radar_max_age_min:
            return "trop vieux", age, rate
        fast = rate >= self.settings.radar_min_likes_per_min
        early_priority = handle.lower() in priority and age < PRIORITY_MAX_AGE_MIN
        if not (fast or early_priority):
            return "trop lent", age, rate
        text = tweet_text(tw).strip()
        if text.startswith("RT @"):
            return "repost", age, rate
        if len(_URL_RE.sub("", text).strip()) < MIN_TEXT_LEN:
            return "trop court", age, rate
        if filters.is_noise(text):
            return "bruit", age, rate
        blocked = filters.hard_blocked(text) or filters.off_limits_term(text)
        if blocked:
            return f"sujet bloqué ({blocked})", age, rate
        low = text.lower()
        if any(k and k in low for k in (self.settings.blacklist_keywords or [])):
            return "mot-clé blacklisté", age, rate
        return None, age, rate

    def _check(self) -> int:
        if not self._due():
            return 0
        self.state.set_last_run("radar")
        accounts = self._accounts()
        if not accounts:
            return 0
        self._resolve_ids(accounts)
        done = self._done_today()
        picked = self._pick(accounts, done)
        if not picked:
            log.info("Radar : aucun compte disponible pour ce passage")
            return 0

        rd = self._rd
        priority = {h.lower() for h in accounts[:PRIORITY_ACCOUNTS]}
        now = now_local()
        candidates: list[tuple[float, str, dict, float, int]] = []
        reasons: dict[str, int] = {}
        for handle in picked:
            uid = rd["user_ids"].get(handle.lower())
            try:
                tweets, newest = self.x.user_posts(uid, since_id=rd["since_ids"].get(handle.lower()),
                                                   max_results=5, optional=True)
            except XError as e:
                if e.kind in STOP_KINDS:
                    log.info("Radar : lecture arrêtée pour ce passage (%s)", e.kind)
                    break
                log.warning("Radar : @%s illisible (%s), compte suivant", handle, e.kind)
                continue
            if newest:
                rd["since_ids"][handle.lower()] = str(newest)
            for tw in tweets or []:
                tid = str(tw.get("id") or "")
                if not tid or self.state.is_handled(tid):
                    continue
                self.state.mark_handled(tid)
                why, age, rate = self._rejection(tw, handle, priority, now)
                if why:
                    key = why.split(" (")[0]
                    reasons[key] = reasons.get(key, 0) + 1
                    continue
                likes = int((tw.get("public_metrics") or {}).get("like_count") or 0)
                candidates.append((rate, handle, tw, age, likes))
        self.state.flush()
        log.info("Radar (%s) : %d candidat(s) %s", ", ".join(f"@{h}" for h in picked), len(candidates),
                 reasons or "")
        if not candidates:
            return 0

        # Le plus rapide d'abord : c'est là qu'une réponse early a le plus de portée.
        candidates.sort(key=lambda c: c[0], reverse=True)
        created = 0
        for rate, handle, tw, age, likes in candidates:
            if created >= MAX_SUGGESTIONS_PER_CHECK:
                break
            if self.state.count("radar_suggestions") >= self.settings.radar_max_per_day:
                break
            if handle.lower() in {d.lower() for d in done}:
                continue  # un seul post par compte et par jour
            text = tweet_text(tw)
            result = self.writer.draft_radar_reply(handle, text, round(age, 1), likes)
            if not result:
                continue
            reply, angle = result
            tid = str(tw["id"])
            try:
                self.approvals.create_radar_item(
                    text=reply, angle=angle, tweet_id=tid, author=handle, post_text=text,
                    post_url=tweet_url(handle, tid), age_min=round(age, 1), likes=likes,
                    expires_at=now_local() + SUGGESTION_TTL,
                )
            except Exception as e:  # noqa: BLE001
                log.exception("Radar : carte pour @%s impossible : %s", handle, e)
                continue
            self.state.incr("radar_suggestions")
            done.append(handle)
            # Anti-répétition : le Writer relit les suggestions récentes.
            if reply not in self.state.recent_texts("radar", 30):
                self.state.remember_text("radar", reply)
            created += 1
            log.info("Radar : suggestion sous @%s (%d likes en %.0f min, %.1f/min)", handle, likes, age, rate)
        self.state.flush()
        return created
