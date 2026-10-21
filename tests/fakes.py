"""Doublures de test partagées : aucun appel réseau dans la suite de tests."""
from __future__ import annotations

import itertools
from datetime import timezone
from typing import Any, Callable

from bot import utils
from bot.x_api import XError


class FakeLLM:
    """`responses` : {préfixe de label: dict | None | callable(label, system, user, schema) -> dict|None}.
    Le premier préfixe qui correspond au label gagne. Sans correspondance : None."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = dict(responses or {})
        self.calls: list[dict] = []

    def json(self, label: str, system: str, user: str, schema: dict,
             effort: str | None = None, max_tokens: int = 16000) -> dict | None:
        self.calls.append({"label": label, "system": system, "user": user, "schema": schema,
                           "effort": effort})
        for prefix, resp in self.responses.items():
            if label.startswith(prefix):
                if callable(resp):
                    return resp(label, system, user, schema)
                if isinstance(resp, list):
                    return resp.pop(0) if resp else None
                return resp
        return None

    def labels(self) -> list[str]:
        return [c["label"] for c in self.calls]


class FakeX:
    def __init__(self) -> None:
        self.dry_run = False
        self.configured = True
        self._ids = itertools.count(1000)
        self.posts: list[dict] = []
        self.uploads: list[str] = []
        self.mention_batches: list[tuple[list[dict], dict, str | None]] = []
        self.user_posts_map: dict[str, list[dict]] = {}
        self.ids_map: dict[str, str] = {}
        self.recent_posts: list[dict] = []
        self.fail_next: list[XError] = []
        self.lose_response_next = 0   # n prochains posts : créés sur X, mais réponse perdue
        self.calls: list[tuple] = []
        self.me_id = "42"

    def user_id(self) -> str:
        return self.me_id

    def upload_image(self, path) -> str:
        self.calls.append(("upload", str(path)))
        if self.fail_next:
            raise self.fail_next.pop(0)
        mid = str(next(self._ids))
        self.uploads.append(mid)
        return mid

    def post(self, text: str, *, reply_to=None, media_ids=None, poll=None, made_with_ai=False) -> str:
        self.calls.append(("post", text, reply_to))
        if self.fail_next:
            raise self.fail_next.pop(0)
        tid = str(next(self._ids))
        self.posts.append({"id": tid, "text": text, "reply_to": reply_to, "media_ids": media_ids,
                           "poll": poll, "made_with_ai": made_with_ai,
                           "created_at": utils.now_local().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")})
        if self.lose_response_next:
            self.lose_response_next -= 1
            raise XError("uncertain", "réponse de X perdue (ReadTimeout)")
        return tid

    def post_thread(self, texts, media_ids_first=None, made_with_ai=False) -> list[str]:
        ids: list[str] = []
        for i, t in enumerate(texts):
            try:
                ids.append(self.post(t, reply_to=ids[-1] if ids else None,
                                     media_ids=media_ids_first if i == 0 else None))
            except XError as e:
                e.partial_ids = list(ids)  # type: ignore[attr-defined]
                raise
        return ids

    def me(self) -> dict:
        self.calls.append(("me",))
        return {"id": self.me_id, "username": "sabislabi", "public_metrics": {"followers_count": 1234}}

    def mentions(self, since_id=None, max_results: int = 50, max_pages: int = 4):
        self.calls.append(("mentions", since_id))
        if self.mention_batches:
            return self.mention_batches.pop(0)
        return [], {}, since_id

    def recent_own_tweets(self, max_results: int = 20):
        self.calls.append(("recent_own_tweets",))
        out = []
        for p in reversed(self.posts[-max_results:]):
            tw = {"id": p["id"], "text": p["text"], "created_at": p["created_at"]}
            if p["reply_to"]:
                tw["referenced_tweets"] = [{"type": "replied_to", "id": p["reply_to"]}]
            out.append(tw)
        return out

    def my_recent_posts(self, max_results: int = 50):
        self.calls.append(("my_recent_posts",))
        return list(self.recent_posts)

    def user_posts(self, user_id: str, since_id=None, max_results: int = 5, optional: bool = True):
        self.calls.append(("user_posts", user_id, since_id))
        tweets = self.user_posts_map.get(user_id, [])
        newest = tweets[0]["id"] if tweets else since_id
        return list(tweets), newest

    def user_ids(self, usernames):
        self.calls.append(("user_ids", tuple(usernames)))
        return {u.lower(): self.ids_map[u.lower()] for u in usernames if u.lower() in self.ids_map}


class FakeTelegram:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.answers: list[tuple[str, str]] = []
        self.photos: list[dict] = []
        self.pending_updates: list[dict] = []
        self.chat_id = "777"
        self.last_error = ""   # comme Telegram : "network" ou la description d'erreur

    def send(self, text: str, buttons=None, reply_to=None, force_reply=False, silent=False) -> int:
        mid = next(self._ids)
        self.sent.append({"id": mid, "text": text, "buttons": buttons, "reply_to": reply_to,
                          "force_reply": force_reply, "silent": silent})
        return mid

    def send_photo(self, path: str, caption: str = "") -> int:
        mid = next(self._ids)
        self.photos.append({"id": mid, "path": path, "caption": caption})
        return mid

    def edit(self, message_id: int, text: str, buttons=None) -> bool:
        self.edits.append({"id": message_id, "text": text, "buttons": buttons})
        return True

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answers.append((callback_id, text))

    def updates(self, offset):
        ups, self.pending_updates = self.pending_updates, []
        return ups

    def is_owner_chat(self, chat_id) -> bool:
        return str(chat_id) == self.chat_id

    # --- aides pour fabriquer des updates ---
    _uid = itertools.count(1)

    def push_callback(self, data: str, message_id: int | None = None, from_id: str | None = None) -> None:
        self.pending_updates.append({
            "update_id": next(self._uid),
            "callback_query": {"id": f"cb{next(self._uid)}", "data": data,
                               "message": {"message_id": message_id or 0, "chat": {"id": self.chat_id}},
                               "from": {"id": int(from_id or self.chat_id)}},
        })

    def push_message(self, text: str, reply_to: int | None = None, chat_id: str | None = None,
                     from_id: str | None = None) -> None:
        msg: dict[str, Any] = {"message_id": next(self._ids), "text": text,
                               "chat": {"id": chat_id or self.chat_id},
                               "from": {"id": int(from_id or chat_id or self.chat_id)}}
        if reply_to:
            msg["reply_to_message"] = {"message_id": reply_to}
        self.pending_updates.append({"update_id": next(self._uid), "message": msg})

    def buttons_of(self, sent_index: int = -1) -> list[dict]:
        rows = self.sent[sent_index]["buttons"] or []
        return [b for row in rows for b in row]


def callback_data_for(fake_tg: FakeTelegram, label_startswith: str, sent_index: int = -1) -> str:
    for b in fake_tg.buttons_of(sent_index):
        if b["text"].startswith(label_startswith) and "callback_data" in b:
            return b["callback_data"]
    raise AssertionError(f"bouton {label_startswith!r} introuvable")


def make_tweet(tid: str, text: str, author_id: str = "7", created_at: str = "2026-10-15T08:00:00Z",
               likes: int = 0, in_reply_to_user_id: str | None = None, conversation_id: str | None = None,
               referenced_reply_to: str | None = None, impressions: int = 0) -> dict:
    tw = {"id": tid, "text": text, "author_id": author_id, "created_at": created_at,
          "conversation_id": conversation_id or tid,
          "public_metrics": {"like_count": likes, "reply_count": 0, "retweet_count": 0, "quote_count": 0,
                             "bookmark_count": 0, "impression_count": impressions}}
    if in_reply_to_user_id:
        tw["in_reply_to_user_id"] = in_reply_to_user_id
    if referenced_reply_to:
        tw["referenced_tweets"] = [{"type": "replied_to", "id": referenced_reply_to}]
    return tw


Responder = Callable[[str, str, str, dict], dict | None]
