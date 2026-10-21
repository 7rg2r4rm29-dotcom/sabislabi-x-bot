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

import html
import logging
import re
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
UNCERTAIN_RETRY_MINUTES = 3   # le temps que le post apparaisse sur le profil
RATE_LIMIT_FALLBACK_MINUTES = 15
RATE_LIMIT_MAX_WAIT_HOURS = 24
STUCK_MINUTES = 15
# Un post validé qui n'a pas pu partir à l'heure (bot arrêté, panne) n'est plus
# publié au-delà de ce retard : un GM à 16 h ou une « actu » de la veille n'ont
# plus de sens. Les réponses aux commentaires restent utiles plus longtemps.
STALE_AFTER_MINUTES = {"reactive": 180, "comment_reply": 720}
STALE_DEFAULT_MINUTES = 180

def stale_after(kind: str | None) -> timedelta:
    return timedelta(minutes=STALE_AFTER_MINUTES.get(kind or "", STALE_DEFAULT_MINUTES))


def scheduled_at(item: dict) -> datetime | None:
    """Heure prévue d'origine (créneau). `publish_at` peut être repoussé (limite de
    débit, nouvel essai, validation tardive) : le retard se mesure depuis celle-ci."""
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    return parse_iso(meta.get("scheduled_at")) or parse_iso(item.get("publish_at"))


def is_stale(item: dict, now: datetime) -> bool:
    """Item dont l'heure prévue est dépassée de trop. Un thread déjà en partie en
    ligne, ou une publication peut-être faite, n'est jamais « trop vieux »."""
    pa = scheduled_at(item)
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    if pa is None or meta.get("partial_ids") or meta.get("uncertain"):
        return False
    return now - pa > stale_after(item.get("kind"))


_TCO_RE = re.compile(r"https://t\.co/\S+")
_LEADING_MENTIONS_RE = re.compile(r"^(?:\s*@\w{1,15})+\s*")


def _norm(text: str) -> str:
    """Texte comparable : sans entités HTML, sans le lien t.co qu'X ajoute pour
    une image, espaces normalisés."""
    t = _TCO_RE.sub(" ", html.unescape(text or ""))
    return " ".join(t.split()).casefold()


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
        "uncertain": "X n'a pas confirmé la publication (réponse perdue)",
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
        meta.setdefault("first_attempt_at", meta["publishing_at"])
        if self.approvals is not None and hasattr(self.approvals, "_forget_edit_prompts"):
            # Une invite ✏️ ouverte avant la publication ne doit plus rien changer.
            self.approvals._forget_edit_prompts(item_id)
        # L'heure prévue d'origine, avant tout report (limite de débit, nouvel essai).
        meta.setdefault("scheduled_at", item.get("publish_at") or meta["publishing_at"])
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
    def _find_live(self, item: dict, text: str, reply_to: str | None = None) -> str | None:
        """Après une publication incertaine : ce texte est-il déjà sur mon profil ?
        (owned read, 0,001 $ le tweet). Lève XError si X ne répond pas : on ne
        republie jamais sans savoir."""
        since = parse_iso(item["meta"].get("first_attempt_at"))
        want = _norm(text)
        replies_here: list[str] = []
        for tw in self.x.recent_own_tweets(max_results=20) or []:
            created = parse_iso(str(tw.get("created_at") or "").replace("Z", "+00:00"))
            if since and created and created < since - timedelta(minutes=5):
                continue
            got = str(tw.get("text") or "")
            if reply_to:
                refs = [str(r.get("id")) for r in (tw.get("referenced_tweets") or [])
                        if r.get("type") == "replied_to"]
                if str(reply_to) not in refs:
                    continue
                replies_here.append(str(tw.get("id")))
                # X ajoute « @pseudo » devant le texte d'une réponse.
                got = _LEADING_MENTIONS_RE.sub("", html.unescape(got))
            if _norm(got) == want:
                log.info("Item %s : déjà en ligne (%s), pas de nouvelle publication", item["id"], tw.get("id"))
                return str(tw.get("id"))
        if reply_to and len(replies_here) == 1:
            # Une seule de mes réponses à ce tweet depuis l'essai : c'est elle.
            log.info("Item %s : réponse déjà en ligne (%s)", item["id"], replies_here[0])
            return replies_here[0]
        return None

    def _send(self, item: dict, texts: list[str]) -> list[str]:
        kind = item["kind"]
        meta = item["meta"]
        if kind == "comment_reply":
            cid = str(meta.get("comment_id") or "").strip()
            if not cid:
                raise XError("invalid", "identifiant du commentaire manquant")
            if meta.get("uncertain"):
                live = self._find_live(item, texts[0], reply_to=cid)
                if live:
                    return [live]
            return [str(self.x.post(texts[0], reply_to=cid))]
        if kind == "thread":
            return self._send_thread(item, texts)
        if len(texts) > 1:
            log.warning("Item %s (%s) : %d textes, seul le premier est publié", item["id"], kind, len(texts))
        if meta.get("uncertain"):
            live = self._find_live(item, texts[0])
            if live:
                return [live]
        media_ids, ai = self._media(item)
        tid = self.x.post(texts[0], media_ids=media_ids or None, poll=item.get("poll") or None,
                          made_with_ai=ai)
        return [str(tid)]

    def _send_thread(self, item: dict, texts: list[str]) -> list[str]:
        partial = [str(i) for i in (item["meta"].get("partial_ids") or []) if i]
        if item["meta"].get("uncertain") and len(partial) < len(texts):
            live = self._find_live(item, texts[len(partial)], reply_to=partial[-1] if partial else None)
            if live:
                partial.append(live)
                item["meta"]["partial_ids"] = list(partial)
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
        for k in ("partial_ids", "retry_at", "publishing_at", "uncertain"):
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
            if item.get("poll"):
                dates = [d for d in (self.state.data.get("poll_dates") or []) if isinstance(d, str)]
                self.state.data["poll_dates"] = (dates + [iso(now_local())])[-20:]
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
        if e.kind == "duplicate" and (meta.get("uncertain") or int(item.get("attempts") or 0) > 0):
            # « Doublon » juste après une publication incertaine : il est sans doute
            # déjà en ligne. On vérifie au prochain passage au lieu d'annoncer un échec.
            e = XError("uncertain", "X signale un doublon après une réponse perdue")
            reason = reason_fr(e)
        if e.kind in FATAL_KINDS:
            self._fail(item, reason)
            return
        if e.kind == "uncertain":
            attempts = int(item.get("attempts") or 0) + 1
            meta["uncertain"] = True
            if attempts >= MAX_ATTEMPTS + 1:
                self.state.update_item(item_id, attempts=attempts)
                self._fail(item, "X n'a jamais confirmé la publication — regarde ton profil X avant de "
                                 "republier (elle est peut-être en ligne)")
                return
            when = now + timedelta(minutes=UNCERTAIN_RETRY_MINUTES)
            meta["retry_at"] = iso(when)
            self.state.update_item(item_id, status="approved", publish_at=iso(when), attempts=attempts,
                                   last_error=f"{reason} — vérification sur ton profil avant tout nouvel essai",
                                   meta=meta)
            log.info("Item %s : publication incertaine, vérification à %s", item_id, iso(when))
            self._refresh(item)
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
            first = not meta.get("rate_limited_notified")
            meta["rate_limited_notified"] = True
            self.state.update_item(item_id, status="approved", publish_at=iso(when),
                                   last_error=reason, meta=meta)
            log.info("Item %s : limite de débit, nouvel essai à %s", item_id, iso(when))
            self._refresh(item)
            if first:
                label, _ = KIND_FR.get(kind, ("Le post", "publié"))
                self._notify(f"⏳ X limite le nombre de publications : {label.lower()} repartira vers "
                             f"{when.astimezone(tz()):%H:%M} (s'il n'est pas trop tard d'ici là).", silent=True)
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
        if meta.get("uncertain") and "profil" not in reason:
            reason += " — il est peut-être quand même en ligne : regarde ton profil X avant de republier"
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
        pa = scheduled_at(item)
        when = f"{pa.astimezone(tz()):%H:%M}" if pa else "?"
        hours = int(stale_after(item.get("kind")).total_seconds() // 3600)
        reason = f"pas parti à l'heure (prévu à {when}, bot arrêté ou X saturé ?) : trop tard pour le publier"
        self.state.update_item(item["id"], status="expired", last_error=reason)
        log.warning("Item %s (%s) : prévu à %s, plus de %d h de retard, pas publié",
                    item["id"], item.get("kind"), when, hours)
        self._refresh(item)
        label, verb = KIND_FR.get(item.get("kind") or "", ("Le post", "publié"))
        self._notify(f"⌛ {label} validé pour {when} n'a pas pu partir à temps (bot arrêté ou X saturé ?). "
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
