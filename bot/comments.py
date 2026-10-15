"""Réponses aux commentaires sous MES posts.

Toutes les COMMENTS_CHECK_EVERY_MINUTES dans COMMENTS_WINDOW :
  1. lit mes mentions (`XClient.mentions(since_id)`, « owned read » à 0,001 $) ;
  2. garde uniquement les réponses à un de mes posts publiés par le bot
     (in_reply_to_user_id == moi ET post visé / conversation dans mes posts) ;
  3. écarte moi-même, le déjà-vu, la blacklist, le bruit et les sujets bloqués ;
  4. demande un brouillon à Claude (`Writer.draft_comment_reply`) ;
  5. crée un item `comment_reply` à valider sur Telegram (rien n'est publié ici :
     c'est le Publisher qui répond, après ton ✅).

Chaque mention examinée est marquée « traitée » (même refusée) pour ne jamais
être relue ; au plus un brouillon par (auteur, post) et par jour, pour ne pas
enchaîner les allers-retours avec la même personne (étiquette
« fast_reply_spam_post » côté X).
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from . import filters
from .utils import in_window, now_local, today_str
from .x_api import XError, tweet_url

log = logging.getLogger("xbot.comments")

DRAFT_TTL = timedelta(hours=24)
# Garde-fous de coût Claude : un post qui fait le buzz peut amener des
# centaines de commentaires ; on étale sur plusieurs passages.
MAX_WRITER_CALLS_PER_CHECK = 12
WRITER_CALLS_PER_DRAFT_CAP = 3       # appels Claude/jour ≤ 3 × plafond de brouillons

_LEADING_MENTIONS = re.compile(r"^(?:\s*@\w+)+\s*")


def strip_leading_mentions(text: str) -> str:
    """'@sabislabi @bob merci !' -> 'merci !' (les @ ajoutés d'office par X)."""
    return _LEADING_MENTIONS.sub("", text or "").strip()


def _id_key(tweet_id) -> tuple[int, str]:
    s = str(tweet_id or "")
    return (int(s), s) if s.isdigit() else (0, s)


def _replied_to_id(tw: dict) -> str | None:
    for ref in tw.get("referenced_tweets") or []:
        if isinstance(ref, dict) and ref.get("type") == "replied_to" and ref.get("id"):
            return str(ref["id"])
    return None


class CommentDesk:
    def __init__(self, settings, state, x, writer, approvals) -> None:
        self.settings = settings
        self.state = state
        self.x = x
        self.writer = writer
        self.approvals = approvals

    # ------------------------------------------------------------------
    def check(self) -> int:
        """Nombre de brouillons créés. Ne lève jamais d'exception."""
        try:
            return self._check()
        except Exception as e:  # noqa: BLE001
            log.exception("Commentaires : erreur inattendue : %s", e)
            return 0

    # ------------------------------------------------------------------
    def _due(self) -> bool:
        s = self.settings
        if not s.comments_enabled:
            return False
        if self.state.data.get("paused"):
            return False
        if not in_window(now_local().time(), s.comments_window):
            return False
        since = self.state.minutes_since("comments")
        if since is not None and since < s.comments_check_every_min:
            return False
        return True

    def _check(self) -> int:
        if not self._due():
            return 0
        self.state.set_last_run("comments")
        cap = int(self.settings.comments_max_drafts_per_day)
        if self.state.count("comment_drafts") >= cap:
            log.info("Commentaires : plafond du jour atteint (%d brouillons), pas de lecture", cap)
            return 0
        if self._calls_cap_reached():
            log.info("Commentaires : plafond d'appels Claude du jour atteint, pas de lecture")
            return 0
        if not getattr(self.x, "configured", True):
            return 0
        my_ids = self.state.my_tweet_ids()
        if not my_ids:
            # Aucun post publié par le bot : aucune réponse ne peut nous concerner.
            return 0

        since = self.state.data.get("mentions_since_id")
        try:
            tweets, users, newest = self.x.mentions(since_id=since)
        except XError as e:
            if e.kind == "rate_limited":
                log.info("Commentaires : limite de débit API X, on réessaiera au prochain passage")
            else:
                log.warning("Commentaires : lecture des mentions impossible (%s) : %s", e.kind, e)
            return 0
        users = users or {}
        me = str(self.x.user_id())

        created = 0
        writer_calls = 0
        last_done: str | None = None
        stopped = False
        # Du plus ancien au plus récent : si on s'arrête en route (plafond),
        # since_id ne dépasse pas la dernière mention examinée.
        for tw in sorted(tweets or [], key=lambda t: _id_key(t.get("id"))):
            if (self.state.count("comment_drafts") >= cap or writer_calls >= MAX_WRITER_CALLS_PER_CHECK
                    or self._calls_cap_reached()):
                stopped = True
                break
            tid = str(tw.get("id") or "")
            if not tid:
                continue
            try:
                made, called = self._process(tw, users, me, my_ids)
            except Exception as e:  # noqa: BLE001
                log.exception("Commentaires : mention %s ignorée après erreur : %s", tid, e)
                self.state.mark_handled(tid)
                made, called = 0, False
            created += made
            if called:
                writer_calls += 1
                self.state.incr("comment_writer_calls")
            last_done = tid

        new_since = (last_done or since) if stopped else (newest or last_done or since)
        if new_since and str(new_since) != str(since or ""):
            # On n'écrit jamais un since_id plus ancien que celui qu'on avait.
            if since is None or _id_key(new_since) > _id_key(since):
                self.state.data["mentions_since_id"] = str(new_since)
                self.state.flush()
        if tweets:
            log.info("Commentaires : %d mention(s) lue(s), %d brouillon(s) créé(s)%s",
                     len(tweets), created, " (suite au prochain passage)" if stopped else "")
        return created

    def _calls_cap_reached(self) -> bool:
        cap = int(self.settings.comments_max_drafts_per_day) * WRITER_CALLS_PER_DRAFT_CAP
        return self.state.count("comment_writer_calls") >= cap

    # ------------------------------------------------------------------
    def _rejection(self, tw: dict, username: str, me: str, my_ids: set[str]) -> str | None:
        """Raison d'écarter la mention, ou None si elle mérite un brouillon."""
        if str(tw.get("in_reply_to_user_id") or "") != me:
            return "pas une réponse à moi"
        replied = _replied_to_id(tw)
        conv = str(tw.get("conversation_id") or "")
        if not ((replied and replied in my_ids) or (conv and conv in my_ids)):
            return "pas sous un de mes posts"
        if str(tw.get("author_id") or "") == me:
            return "moi-même"
        handle = (self.settings.handle or "").lower()
        if username and handle and username.lower() == handle:
            return "moi-même"
        if username and username.lower() in (self.settings.blacklist_users or []):
            return "auteur blacklisté"
        text = strip_leading_mentions(tw.get("text") or "")
        if len(text) < 2:
            return "vide"
        if filters.is_noise(text):
            return "bruit"
        blocked = filters.hard_blocked(text) or filters.off_limits_term(text)
        if blocked:
            return f"sujet bloqué ({blocked})"
        low = text.lower()
        if any(k and k in low for k in (self.settings.blacklist_keywords or [])):
            return "mot-clé blacklisté"
        return None

    def _my_post(self, tw: dict) -> tuple[str, str]:
        """(id du post concerné, texte de mon post). Le post visé d'abord (texte
        le plus précis), sinon la racine de la conversation."""
        by_id = {str(p.get("tweet_id")): p for p in self.state.data.get("published") or []}
        replied = _replied_to_id(tw)
        conv = str(tw.get("conversation_id") or "")
        for cand in (replied, conv):
            if cand and cand in by_id:
                return cand, str(by_id[cand].get("text") or "")
        return replied or conv, ""

    def _pair_seen(self, key: str) -> bool:
        pairs = self.state.data.setdefault("comment_pairs", {})
        return key in (pairs.get(today_str()) or [])

    def _remember_pair(self, key: str) -> None:
        today = today_str()
        pairs = self.state.data.setdefault("comment_pairs", {})
        for d in [d for d in pairs if d != today]:
            del pairs[d]
        pairs.setdefault(today, []).append(key)
        self.state.flush()

    def _process(self, tw: dict, users: dict, me: str, my_ids: set[str]) -> tuple[int, bool]:
        """Retourne (brouillons créés 0/1, Claude appelé ?)."""
        tid = str(tw["id"])
        if self.state.is_handled(tid):
            return 0, False
        # Marqué tout de suite : quoi qu'il arrive, cette mention ne repassera pas.
        self.state.mark_handled(tid)
        author_id = str(tw.get("author_id") or "")
        user = users.get(author_id) or users.get(tw.get("author_id")) or {}
        username = str(user.get("username") or "").lstrip("@")

        why = self._rejection(tw, username, me, my_ids)
        if why:
            log.debug("Mention %s de @%s écartée : %s", tid, username or author_id, why)
            return 0, False

        root_id, my_post_text = self._my_post(tw)
        conv = str(tw.get("conversation_id") or "")
        thread_key = conv if conv in my_ids else root_id
        pair = f"{(username or author_id).lower()}|{thread_key}"
        if self._pair_seen(pair):
            log.info("Commentaire de @%s : déjà un brouillon aujourd'hui sous ce post, ignoré", username)
            return 0, False

        comment = strip_leading_mentions(tw.get("text") or "")
        reply = self.writer.draft_comment_reply(my_post_text, username, comment)
        if not reply:
            return 0, True
        try:
            self.approvals.create_comment_item(
                text=reply, comment_id=tid, comment_text=comment, commenter=username,
                my_post_text=my_post_text, post_url=tweet_url(username, tid),
                expires_at=now_local() + DRAFT_TTL,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("Commentaire de @%s : création de la carte impossible : %s", username, e)
            return 0, True
        self.state.incr("comment_drafts")
        self._remember_pair(pair)
        # Anti-répétition (le Writer relit ces textes) : deux réponses presque
        # identiques = étiquette « COPYPASTA_SPAM » côté X.
        if reply not in self.state.recent_texts("comment_reply", 30):
            self.state.remember_text("comment_reply", reply)
        log.info("Commentaire de @%s : brouillon de réponse proposé", username)
        return 1, True
