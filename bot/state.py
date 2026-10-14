"""Mémoire persistante du bot (state/state.json), écrite de façon atomique.

Schéma (version 2) :
  items          {id: Item}           brouillons et suggestions (posts, threads,
                                      réponses aux commentaires, radar)
  slots_done     {date: {slot_key: id}} créneau déjà rédigé ce jour-là
  published      [Published]          mes posts publiés (200 derniers)
  handled_ids    [tweet_id]           mentions / posts radar déjà traités
  mentions_since_id
  radar          {user_ids, since_ids, rotation}
  counters       {date: {nom: n}}
  telegram       {offset, edit_waiting: {msg_id: item_id}}
  last_runs      {job: iso}
  paused         bool
  notes          [{text, created_at, uses}]   faits réels envoyés par /note
  recent_texts   {kind: [texte]}      anti-répétition
  thread_recent_kinds [kind]
  costs, llm_usage, performance, reports   (gérés par leurs modules)
"""
from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from .utils import STATE_DIR, iso, load_json, now_local, parse_iso, save_json, today_str

ITEM_STATUSES = ("awaiting", "approved", "publishing", "published",
                 "rejected", "expired", "failed", "suggested", "dismissed")
OPEN_STATUSES = ("awaiting", "approved", "publishing", "suggested")


def _default() -> dict[str, Any]:
    return {
        "version": 2,
        "items": {},
        "slots_done": {},
        "published": [],
        "handled_ids": [],
        "mentions_since_id": None,
        "radar": {"user_ids": {}, "since_ids": {}, "rotation": 0},
        "counters": {},
        "telegram": {"offset": None, "edit_waiting": {}},
        "last_runs": {},
        "paused": False,
        "notes": [],
        "recent_texts": {},
        "thread_recent_kinds": [],
    }


class StateStore:
    def __init__(self, path=None) -> None:
        from pathlib import Path
        self.path = Path(path) if path else STATE_DIR / "state.json"
        data = load_json(self.path, default={})
        if not isinstance(data, dict) or data.get("version") != 2:
            # Ancien état v1 (navigateur) : on repart proprement, en gardant
            # une copie pour mémoire.
            if data:
                save_json(self.path.with_name("state_v1_backup.json"), data)
            data = _default()
        for k, v in _default().items():
            data.setdefault(k, v)
        self.data: dict[str, Any] = data
        self.flush()

    # ------------------------------------------------------------------
    def flush(self) -> None:
        save_json(self.path, self.data)

    # ---- Items --------------------------------------------------------
    def new_item(self, kind: str, texts: list[str], **fields: Any) -> dict:
        item_id = secrets.token_hex(4)
        while item_id in self.data["items"]:
            item_id = secrets.token_hex(4)
        now = now_local()
        item = {
            "id": item_id,
            "kind": kind,
            "status": "awaiting",
            "texts": list(texts),
            "image_path": None,
            "image_kind": None,
            "poll": None,
            "publish_at": iso(now),
            "expires_at": iso(now + timedelta(hours=3)),
            "created_at": iso(now),
            "approved_at": None,
            "published_ids": [],
            "tg_message_id": None,
            "regen_count": 0,
            "attempts": 0,
            "last_error": "",
            "slot_key": None,
            "slot_date": None,
            "meta": {},
        }
        item.update(fields)
        self.data["items"][item_id] = item
        self.flush()
        return item

    def item(self, item_id: str) -> dict | None:
        return self.data["items"].get(item_id)

    def update_item(self, item_id: str, **fields: Any) -> dict | None:
        it = self.data["items"].get(item_id)
        if it is None:
            return None
        it.update(fields)
        self.flush()
        return it

    def items(self, kinds: tuple[str, ...] | None = None,
              statuses: tuple[str, ...] | None = None) -> list[dict]:
        out = []
        for it in self.data["items"].values():
            if kinds and it.get("kind") not in kinds:
                continue
            if statuses and it.get("status") not in statuses:
                continue
            out.append(it)
        out.sort(key=lambda i: i.get("created_at") or "")
        return out

    def item_by_message(self, message_id: int) -> dict | None:
        for it in self.data["items"].values():
            if it.get("tg_message_id") == message_id:
                return it
        return None

    # ---- Créneaux -----------------------------------------------------
    def slot_item_id(self, date: str, slot_key: str) -> str | None:
        return (self.data["slots_done"].get(date) or {}).get(slot_key)

    def mark_slot(self, date: str, slot_key: str, item_id: str) -> None:
        self.data["slots_done"].setdefault(date, {})[slot_key] = item_id
        self.flush()

    # ---- Posts publiés ------------------------------------------------
    def add_published(self, tweet_id: str, kind: str, text: str, item_id: str | None = None,
                      slot_key: str | None = None) -> None:
        self.data["published"].append({
            "tweet_id": str(tweet_id), "kind": kind, "text": (text or "")[:500],
            "item_id": item_id, "slot_key": slot_key, "created_at": iso(now_local()),
            "metrics": None,
        })
        self.data["published"] = self.data["published"][-200:]
        self.flush()

    def published_since(self, hours: float) -> list[dict]:
        limit = now_local() - timedelta(hours=hours)
        out = []
        for p in self.data["published"]:
            dt = parse_iso(p.get("created_at"))
            if dt and dt >= limit:
                out.append(p)
        return out

    def my_tweet_ids(self) -> set[str]:
        return {p["tweet_id"] for p in self.data["published"]}

    # ---- Déjà traités -------------------------------------------------
    def is_handled(self, tweet_id: str) -> bool:
        return str(tweet_id) in self.data["handled_ids"]

    def mark_handled(self, tweet_id: str) -> None:
        ids = self.data["handled_ids"]
        if str(tweet_id) not in ids:
            ids.append(str(tweet_id))
            self.data["handled_ids"] = ids[-5000:]
            self.flush()

    # ---- Compteurs ----------------------------------------------------
    def incr(self, name: str, n: int = 1, date: str | None = None) -> int:
        date = date or today_str()
        day = self.data["counters"].setdefault(date, {})
        day[name] = int(day.get(name, 0)) + n
        self.flush()
        return day[name]

    def count(self, name: str, date: str | None = None) -> int:
        date = date or today_str()
        return int((self.data["counters"].get(date) or {}).get(name, 0))

    # ---- Horodatage des tâches ---------------------------------------
    def last_run(self, job: str):
        return parse_iso(self.data["last_runs"].get(job))

    def set_last_run(self, job: str, when=None) -> None:
        self.data["last_runs"][job] = iso(when or now_local())
        self.flush()

    def minutes_since(self, job: str) -> float | None:
        last = self.last_run(job)
        if last is None:
            return None
        return (now_local() - last).total_seconds() / 60.0

    # ---- Textes récents (anti-répétition) ----------------------------
    def recent_texts(self, kind: str, n: int = 14) -> list[str]:
        return list(self.data["recent_texts"].get(kind, []))[-n:]

    def remember_text(self, kind: str, text: str) -> None:
        lst = self.data["recent_texts"].setdefault(kind, [])
        lst.append(text)
        self.data["recent_texts"][kind] = lst[-30:]
        self.flush()

    # ---- Notes perso (/note) ------------------------------------------
    def add_note(self, text: str) -> None:
        self.data["notes"].append({"text": text.strip()[:500], "created_at": iso(now_local()), "uses": 0})
        self.data["notes"] = self.data["notes"][-30:]
        self.flush()

    def active_notes(self, max_age_days: int = 14, max_uses: int = 2) -> list[dict]:
        limit = now_local() - timedelta(days=max_age_days)
        out = []
        for n in self.data["notes"]:
            dt = parse_iso(n.get("created_at"))
            if dt and dt >= limit and int(n.get("uses", 0)) < max_uses:
                out.append(n)
        return out

    def mark_notes_used(self, texts: list[str]) -> None:
        wanted = set(texts)
        for n in self.data["notes"]:
            if n.get("text") in wanted:
                n["uses"] = int(n.get("uses", 0)) + 1
        self.flush()

    # ---- Ménage -------------------------------------------------------
    def gc(self, keep_days: int = 10) -> int:
        """Supprime les items clos de plus de `keep_days` jours, les compteurs
        et créneaux anciens. Retourne le nombre d'items supprimés."""
        limit = now_local() - timedelta(days=keep_days)
        removed = 0
        for item_id in list(self.data["items"]):
            it = self.data["items"][item_id]
            if it.get("status") in OPEN_STATUSES:
                continue
            dt = parse_iso(it.get("created_at"))
            if dt is None or dt < limit:
                del self.data["items"][item_id]
                removed += 1
        cutoff = limit.strftime("%Y-%m-%d")
        for key in ("counters", "slots_done"):
            for d in [d for d in self.data[key] if d < cutoff]:
                del self.data[key][d]
        waiting = self.data["telegram"].get("edit_waiting") or {}
        for msg_id in list(waiting):
            if waiting[msg_id] not in self.data["items"]:
                del waiting[msg_id]
        self.flush()
        return removed
