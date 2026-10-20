"""Publication des items validés, à l'heure prévue, via l'API X officielle.

- posts (gm / tip / news / réactif) : image éventuelle (sauf sondage), puis post ;
- threads : reprise sans doublon à partir des tweets déjà en ligne
  (`meta.partial_ids`) si une publication a été coupée en route ;
- réponses à mes commentaires : réponse « summoned » (0,01 $) ;
- JAMAIS les suggestions du radar (c'est toi qui les publies dans l'app X).

Erreurs : définitives (clés, crédits, budget, refus, doublon) → échec + message
Telegram ; limite de débit → nouvel essai à l'heure de réinitialisation ;
réseau / serveur / autre → 3 essais espacés de 10 min. `publish_due` ne lève
jamais d'exception : un item raté ne bloque pas la boucle.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from .telegram import esc
from .utils import iso, now_local, parse_iso, tz
from .x_api import XError

log = logging.getLogger("xbot.publisher")

POST_KINDS = ("gm", "tip", "news", "reactive")
PUBLISHABLE = POST_KINDS + ("thread", "comment_reply")
FATAL_KINDS = ("auth", "credits", "budget", "forbidden", "invalid", "duplicate")
MAX_ATTEMPTS = 3
RETRY_MINUTES = 10
RATE_LIMIT_FALLBACK_MINUTES = 15
RATE_LIMIT_MAX_WAIT_HOURS = 24
STUCK_MINUTES = 15
# Un post validé qui n'a pas pu partir à l'heure (bot arrêté, panne) n'est plus
# publié au-delà de ce retard : un GM à 16 h ou une « actu » de la veille n'ont
# plus de sens. Les réponses aux commentaires restent utiles plus longtemps.
STALE_AFTER_MINUTES = {"reactive": 120, "comment_reply": 720}
STALE_DEFAULT_MINUTES = 180

def stale_after(kind: str | None) -> timedelta:
    return timedelta(minutes=STALE_AFTER_MINUTES.get(kind or "", STALE_DEFAULT_MINUTES))


def is_stale(item: dict, now: datetime) -> bool:
    """Item validé dont l'heure de publication est dépassée de trop. Un thread
    déjà en partie en ligne n'est jamais « trop vieux » : on le termine."""
    pa = parse_iso(item.get("publish_at"))
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    if pa is None or meta.get("partial_ids"):
        return False
    return now - pa > stale_after(item.get("kind"))


# (sujet, participe accordé) pour les messages d'échec.
KIND_FR = {
    "gm": ("Le GM", "publié"), "tip": ("L'astuce IA", "publiée"), "news": ("L'actu IA", "publiée"),
    "reactive": ("Le post réactif", "publié"), "thread": ("Le thread", "publié"),
    "comment_reply": ("La réponse au commentaire", "envoyée"),
}


def reason_fr(e: XError) -> str:
    """Explication en français d'une erreur API X (sans secret)."""
    detail = str(e).strip()[:160] or "sans détail"
    return {
        "auth": "Clés API X refusées — vérifie X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN et "
                "X_ACCESS_TOKEN_SECRET (droits « Read and write »)",
        "credits": "Plus de crédits API X — recharge sur console.x.com",
        "budget": "Plafond de budget API X atteint (X_BUDGET_MONTHLY_USD) — rien n'a été dépensé",
        "forbidden": f"X a refusé la publication : {detail}",
        "invalid": f"X a refusé la requête : {detail}",
        "duplicate": "X refuse les doublons : ce texte (ou presque) a déjà été publié",
        "not_summoned": "X n'autorise le bot à répondre que si on t'a mentionné ou répondu — "
                        "réponds toi-même avec le bouton 📲",
        "rate_limited": "Limite de débit X atteinte",
        "network": "Réseau indisponible",
        "server": f"Erreur du serveur X : {detail}",
        "not_found": f"Post introuvable (supprimé ?) : {detail}",
    }.get(e.kind, f"Erreur : {detail}")


class Publisher:
    def __init__(self, settings, state, x, approvals) -> None:
        self.settings = settings
        self.state = state
        self.x = x
        self.approvals = approvals

    # ------------------------------------------------------------------
    def _due(self) -> list[dict]:
        now = now_local()
        due: list[tuple[datetime, dict]] = []
        for it in self.state.items(statuses=("approved",)):
            if it.get("kind") not in PUBLISHABLE:
                continue
            pa = parse_iso(it.get("publish_at")) or now
            if pa <= now:
                due.append((pa, it))
        due.sort(key=lambda p: p[0])
        return [it for _, it in due]

    def publish_due(self) -> int:
        """Publie les items validés dont l'heure est venue. Retourne le nombre publié."""
        try:
            self._recover_stuck()
        except Exception as e:  # noqa: BLE001
            log.exception("Vérification des publications interrompues impossible : %s", e)
        try:
            due = self._due()
        except Exception as e:  # noqa: BLE001
            log.exception("Lecture des items à publier impossible : %s", e)
            return 0
        if not due:
            return 0
        if self.state.data.get("paused"):
            log.info("En pause : %d publication(s) validée(s) attendent /reprendre", len(due))
            return 0
        now = now_local()
        n = 0
        for item in due:
            if item.get("status") != "approved":
                continue
            if is_stale(item, now):
                self._expire_late(item)
                continue
            try:
                if self.publish_item(item):
                    n += 1
            except Exception as e:  # noqa: BLE001
                log.exception("Publication de %s : erreur inattendue : %s", item.get("id"), e)
        return n

    # ------------------------------------------------------------------
    def publish_item(self, item: dict) -> bool:
        item_id = item.get("id")
        kind = item.get("kind")
        if kind not in PUBLISHABLE:
            # Le radar, en particulier, n'est JAMAIS publié par l'API.
            log.error("Item %s (%s) : ce type n'est jamais publié par le bot", item_id, kind)
            return False
        if item.get("status") != "approved":
            log.warning("Item %s : statut %s, pas de publication", item_id, item.get("status"))
            return False
        texts = [t for t in (item.get("texts") or []) if isinstance(t, str) and t.strip()]
        if not texts:
            self._fail(item, "aucun texte à publier")
            return False
        meta = item.get("meta")
        if not isinstance(meta, dict):
            meta = item["meta"] = {}
        meta["publishing_at"] = iso(now_local())
        self.state.update_item(item_id, status="publishing", meta=meta)
        log.info("Publication de %s (%s, %d texte(s))", item_id, kind, len(texts))
        try:
            ids = self._send(item, texts)
        except XError as e:
            self._on_error(item, e)
            return False
        except Exception as e:  # noqa: BLE001
            log.exception("Publication de %s : erreur inattendue : %s", item_id, e)
            self._on_error(item, XError("other", f"{type(e).__name__}: {e}"))
            return False
        self._on_success(item, texts, ids)
        return True

    # ------------------------------------------------------------------
    def _send(self, item: dict, texts: list[str]) -> list[str]:
        kind = item["kind"]
        meta = item["meta"]
        if kind == "comment_reply":
            cid = str(meta.get("comment_id") or "").strip()
            if not cid:
                raise XError("invalid", "identifiant du commentaire manquant")
            return [str(self.x.post(texts[0], reply_to=cid))]
        if kind == "thread":
            return self._send_thread(item, texts)
        if len(texts) > 1:
            log.warning("Item %s (%s) : %d textes, seul le premier est publié", item["id"], kind, len(texts))
        media_ids, ai = self._media(item)
        tid = self.x.post(texts[0], media_ids=media_ids or None, poll=item.get("poll") or None,
                          made_with_ai=ai)
        return [str(tid)]

    def _send_thread(self, item: dict, texts: list[str]) -> list[str]:
        partial = [str(i) for i in (item["meta"].get("partial_ids") or []) if i]
        if partial:
            # Reprise : on enchaîne en réponse au dernier tweet déjà en ligne.
            if len(partial) >= len(texts):
                return partial
            log.info("Thread %s : reprise après %d/%d tweets", item["id"], len(partial), len(texts))
            ids = list(partial)
            for t in texts[len(partial):]:
                try:
                    ids.append(str(self.x.post(t, reply_to=ids[-1])))
                except XError as e:
                    e.partial_ids = list(ids)  # type: ignore[attr-defined]
                    raise
            return ids
        media_ids, ai = self._media(item)
        return [str(i) for i in self.x.post_thread(texts, media_ids_first=media_ids or None,
                                                   made_with_ai=ai)]

    def _media(self, item: dict) -> tuple[list[str], bool]:
        """Upload de l'image (jamais avec un sondage). Image illisible ou refusée :
        on publie sans elle plutôt que de perdre le post."""
        path = item.get("image_path")
        if not path or item.get("poll"):
            return [], False
        p = Path(path)
        if not p.is_file():
            log.warning("Item %s : image %s introuvable, publication sans image", item["id"], p.name)
            return [], False
        try:
            media_id = self.x.upload_image(p)
        except XError as e:
            if e.kind in ("invalid", "not_found"):
                log.warning("Item %s : image refusée par X (%s), publication sans image", item["id"], e)
                return [], False
            raise
        except OSError as e:
            log.warning("Item %s : image illisible (%s), publication sans image", item["id"], e)
            return [], False
        return [str(media_id)], item.get("image_kind") == "ai"

    # ------------------------------------------------------------------
    def _on_success(self, item: dict, texts: list[str], ids: list[str]) -> None:
        item_id = item["id"]
        kind = item["kind"]
        meta = item["meta"]
        for k in ("partial_ids", "retry_at", "publishing_at"):
            meta.pop(k, None)
        # Le statut d'abord : quoi qu'il arrive ensuite, on ne republie pas.
        self.state.update_item(item_id, status="published", published_ids=[str(i) for i in ids],
                               last_error="", meta=meta)
        log.info("Publié : %s %s -> %s", kind, item_id, ids[0] if ids else "?")
        try:
            # Les réponses aux commentaires ne vont pas dans `published` (liste de
            # mes POSTS, plafonnée à 200 : 40 réponses/jour en chasseraient les
            # posts en 5 jours et fausseraient les statistiques).
            if kind != "comment_reply" and ids:
                self.state.add_published(ids[0], kind, texts[0], item_id, item.get("slot_key"))
            if texts[0] not in self.state.recent_texts(kind, 30):
                self.state.remember_text(kind, texts[0])
            if kind == "thread":
                self._remember_thread_kind(item)
            notes = [n for n in (meta.get("used_notes") or []) if isinstance(n, str) and n]
            if notes:
                self.state.mark_notes_used(notes)
            self.state.incr(f"published_{kind}")
        except Exception as e:  # noqa: BLE001
            log.exception("Item %s publié, mais mise à jour de l'état incomplète : %s", item_id, e)
        self._refresh(item)

    def _remember_thread_kind(self, item: dict) -> None:
        """Genre du thread publié -> rotation des genres (6 derniers). L'id est noté
        dans `thread_kinds_seen` pour que le Writer ne le compte pas une 2e fois."""
        data = self.state.data
        seen = data.get("thread_kinds_seen")
        if not isinstance(seen, list):
            seen = []
        if item["id"] in seen:
            return
        seen.append(item["id"])
        data["thread_kinds_seen"] = seen[-50:]
        angle = str(item["meta"].get("angle") or "").strip().lower()
        if angle:
            kinds = [k for k in (data.get("thread_recent_kinds") or []) if isinstance(k, str)]
            kinds.append(angle)
            data["thread_recent_kinds"] = kinds[-6:]
        self.state.flush()

    # ------------------------------------------------------------------
    def _on_error(self, item: dict, e: XError) -> None:
        item_id = item["id"]
        kind = item["kind"]
        meta = item["meta"]
        meta.pop("publishing_at", None)
        partial = getattr(e, "partial_ids", None)
        if kind == "thread" and partial:
            meta["partial_ids"] = [str(i) for i in partial]
        reason = reason_fr(e)
        log.warning("Publication de %s (%s) : erreur %s : %s", item_id, kind, e.kind, str(e)[:200])
        now = now_local()

        if e.kind == "not_summoned":
            # Réponse refusée par X : la carte propose « 📲 Répondre moi-même ».
            self._fail(item, reason, notify=kind != "comment_reply")
            if kind == "comment_reply":
                who = (meta.get("commenter") or "").strip()
                self._notify(f"💬 X refuse que le bot réponde{(' à @' + esc(who)) if who else ''} : "
                             "réponds toi-même avec le bouton 📲 sur la carte.", silent=True)
            return
        if e.kind in FATAL_KINDS:
            self._fail(item, reason)
            return
        if e.kind == "rate_limited":
            when = now + timedelta(minutes=RATE_LIMIT_FALLBACK_MINUTES)
            if e.reset_at:
                try:
                    reset = datetime.fromtimestamp(float(e.reset_at), tz()) + timedelta(seconds=30)
                    when = min(max(reset, now + timedelta(minutes=1)),
                               now + timedelta(hours=RATE_LIMIT_MAX_WAIT_HOURS))
                except (OverflowError, OSError, ValueError):
                    pass
            meta["retry_at"] = iso(when)
            self.state.update_item(item_id, status="approved", publish_at=iso(when),
                                   last_error=reason, meta=meta)
            log.info("Item %s : limite de débit, nouvel essai à %s", item_id, iso(when))
            self._refresh(item)
            return
        # Réseau, serveur, introuvable, autre : nouvel essai dans 10 min, 3 essais au total.
        attempts = int(item.get("attempts") or 0) + 1
        if attempts >= MAX_ATTEMPTS:
            self.state.update_item(item_id, attempts=attempts)
            self._fail(item, f"{reason} (après {attempts} essais)")
            return
        when = now + timedelta(minutes=RETRY_MINUTES)
        meta["retry_at"] = iso(when)
        self.state.update_item(item_id, status="approved", publish_at=iso(when), attempts=attempts,
                               last_error=f"{reason} (essai {attempts}/{MAX_ATTEMPTS})", meta=meta)
        log.info("Item %s : essai %d/%d raté, nouvel essai à %s", item_id, attempts, MAX_ATTEMPTS, iso(when))
        self._refresh(item)

    def _fail(self, item: dict, reason: str, notify: bool = True) -> None:
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        meta.pop("publishing_at", None)
        self.state.update_item(item["id"], status="failed", last_error=reason, meta=meta)
        log.error("Item %s (%s) en échec : %s", item["id"], item.get("kind"), reason)
        self._refresh(item)
        if notify:
            label, verb = KIND_FR.get(item.get("kind") or "", ("Le post", "publié"))
            msg = f"❌ {label} n'a pas été {verb} : {esc(reason)}"
            partial = meta.get("partial_ids") or []
            if partial:
                msg += f"\n🧩 {len(partial)}/{len(item.get('texts') or [])} tweets sont déjà en ligne (voir la carte)."
            self._notify(msg)

    def _expire_late(self, item: dict) -> None:
        pa = parse_iso(item.get("publish_at"))
        when = f"{pa.astimezone(tz()):%H:%M}" if pa else "?"
        hours = int(stale_after(item.get("kind")).total_seconds() // 3600)
        reason = f"pas parti à l'heure (prévu à {when}, bot arrêté ?) : trop tard pour le publier"
        self.state.update_item(item["id"], status="expired", last_error=reason)
        log.warning("Item %s (%s) : prévu à %s, plus de %d h de retard, pas publié",
                    item["id"], item.get("kind"), when, hours)
        self._refresh(item)
        label, verb = KIND_FR.get(item.get("kind") or "", ("Le post", "publié"))
        self._notify(f"⌛ {label} validé pour {when} n'a pas pu partir à temps (bot arrêté ?). "
                     f"Avec plus de {hours} h de retard, je ne le publie pas : rien n'est parti. "
                     "Le prochain créneau suit son cours.")

    def _recover_stuck(self) -> None:
        """Item resté « en cours de publication » (bot tué au mauvais moment) :
        on ne republie pas à l'aveugle (risque de doublon), on prévient."""
        now = now_local()
        for it in self.state.items(statuses=("publishing",)):
            meta = it.get("meta") or {}
            started = parse_iso(meta.get("publishing_at")) or parse_iso(it.get("publish_at")) or now
            if now - started < timedelta(minutes=STUCK_MINUTES):
                continue
            self._fail(it, "publication interrompue (redémarrage du bot ?) — vérifie ton profil X "
                           "avant de republier")

    # ------------------------------------------------------------------
    def _refresh(self, item: dict) -> None:
        if self.approvals is None:
            return
        try:
            self.approvals.refresh_card(item)
        except Exception as e:  # noqa: BLE001
            log.warning("Carte %s : mise à jour impossible (%s)", item.get("id"), e)

    def _notify(self, text: str, silent: bool = False) -> None:
        if self.approvals is None:
            log.info("Notification : %s", text[:300])
            return
        try:
            self.approvals.notify(text, silent=silent)
        except Exception as e:  # noqa: BLE001
            log.warning("Notification impossible (%s)", e)
