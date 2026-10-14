"""Client Telegram minimal (Bot API) : messages, boutons, réponses, édition."""
from __future__ import annotations

import html
import logging
from typing import Any

import requests

log = logging.getLogger("xbot.telegram")

MAX_TEXT = 4000  # Telegram coupe à 4096


def esc(text: str) -> str:
    """Échappe un texte pour parse_mode=HTML."""
    return html.escape(text or "", quote=False)


def button(text: str, data: str | None = None, url: str | None = None) -> dict:
    b: dict[str, Any] = {"text": text}
    if url:
        b["url"] = url
    else:
        b["callback_data"] = (data or "")[:64]
    return b


class Telegram:
    def __init__(self, token: str, chat_id: str, session: requests.Session | None = None) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.api = f"https://api.telegram.org/bot{token}"
        self.session = session or requests.Session()

    def _call(self, method: str, payload: dict) -> Any:
        try:
            r = self.session.post(f"{self.api}/{method}", json=payload, timeout=20)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            # Ne jamais logger l'URL : elle contient le jeton du bot.
            log.warning("Telegram %s : réseau (%s)", method, type(e).__name__)
            return None
        if not data.get("ok"):
            log.warning("Telegram %s : %s", method, str(data.get("description"))[:200])
            return None
        return data.get("result")

    # ------------------------------------------------------------------
    def send(self, text: str, buttons: list[list[dict]] | None = None, reply_to: int | None = None,
             force_reply: bool = False, silent: bool = False) -> int | None:
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text[:MAX_TEXT],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": silent,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        elif force_reply:
            payload["reply_markup"] = {"force_reply": True, "selective": True}
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        res = self._call("sendMessage", payload)
        return int(res["message_id"]) if res and "message_id" in res else None

    def send_photo(self, path: str, caption: str = "") -> int | None:
        try:
            with open(path, "rb") as f:
                r = self.session.post(f"{self.api}/sendPhoto",
                                      data={"chat_id": self.chat_id, "caption": caption[:1000],
                                            "parse_mode": "HTML"},
                                      files={"photo": f}, timeout=60)
            data = r.json()
            if data.get("ok"):
                return int(data["result"]["message_id"])
            log.warning("Telegram sendPhoto : %s", str(data.get("description"))[:200])
        except (OSError, requests.RequestException, ValueError) as e:
            log.warning("Telegram sendPhoto : %s", type(e).__name__)
        return None

    def edit(self, message_id: int, text: str, buttons: list[list[dict]] | None = None) -> bool:
        payload: dict[str, Any] = {
            "chat_id": self.chat_id, "message_id": message_id, "text": text[:MAX_TEXT],
            "parse_mode": "HTML", "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": buttons or []},
        }
        return self._call("editMessageText", payload) is not None

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]})

    def updates(self, offset: int | None) -> list[dict]:
        payload: dict[str, Any] = {"timeout": 0, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        res = self._call("getUpdates", payload)
        return res if isinstance(res, list) else []

    def is_owner_chat(self, chat_id: Any) -> bool:
        return str(chat_id) == self.chat_id
