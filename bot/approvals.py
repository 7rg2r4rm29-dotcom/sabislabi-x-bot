"""Validation humaine sur Telegram : cartes avec boutons, modification du
texte, nouvelle version, commandes (/statut, /plan, /budget, /pause,
/reprendre, /note, /aide).

Règle d'or : ce module n'appelle JAMAIS l'API X. Valider ne fait que changer
le statut (et l'heure de publication) d'un item ; c'est `Publisher`, appelé
juste après `poll()` dans le même tour de boucle, qui publie.

Les suggestions du radar ne passent jamais par l'API : la carte donne un lien
« intent » qui ouvre l'app X avec la réponse pré-remplie, et c'est toi qui
appuies sur « Répondre ».
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import filters
from .telegram import button, esc
from .utils import in_window, iso, now_local, parse_iso, tz
from .x_api import contains_url, intent_post_url, intent_reply_url, tweet_url

log = logging.getLogger("xbot.approvals")

POST_KINDS = ("gm", "tip", "news", "thread", "reactive")
MAX_REGEN = 3
RESUME_DELAY_MINUTES = 3  # /reprendre : délai avant le départ des posts en retard
CARD_MAX_CHARS = 3900          # Telegram coupe à 4096 ; on garde de la marge pour le HTML
EDIT_PROMPT_MAX_TEXT = 3000    # au-delà, on ne recopie pas le texte actuel dans l'invite ✏️
CARD_RETRY_MINUTES = 5         # carte non envoyée (Telegram en panne) : nouvel essai toutes les 5 min
CARD_MAX_RETRIES = 24          # … pendant 2 h au plus

KIND_LABELS = {
    "gm": ("☀️", "GM"),
    "tip": ("💡", "Astuce IA"),
    "news": ("📰", "Actu IA décryptée"),
    "thread": ("🧵", "Thread"),
    "reactive": ("⚡", "Post réactif"),
    "comment_reply": ("💬", "Réponse à un commentaire"),
    "radar": ("🎯", "Radar"),
}
THREAD_GENRES_FR = {"timely": "actu", "tutorial": "tuto", "practical": "pratique",
                    "beginner": "débutants", "mindset": "mindset", "story": "histoire vraie"}

# Statuts dans lesquels chaque bouton a encore un sens.
_ACTIONABLE = {
    "ok": {"post": ("awaiting",), "comment_reply": ("awaiting",), "radar": ()},
    "no": {"post": ("awaiting", "approved"), "comment_reply": ("awaiting", "approved"),
           "radar": ("suggested",)},
    "regen": {"post": ("awaiting",), "comment_reply": ("awaiting",), "radar": ("suggested",)},
    "edit": {"post": ("awaiting", "approved"), "comment_reply": ("awaiting",), "radar": ("suggested",)},
}

_COMMAND_ALIASES = {
    "aide": "aide", "help": "aide", "start": "aide", "?": "aide", "commandes": "aide",
    "statut": "statut", "status": "statut", "etat": "statut", "état": "statut",
    "plan": "plan", "planning": "plan", "programme": "plan",
    "budget": "budget", "couts": "budget", "coûts": "budget",
    "pause": "pause", "stop": "pause",
    "reprendre": "reprendre", "reprise": "reprendre", "resume": "reprendre", "go": "reprendre",
    "note": "note",
}

HELP_TEXT = (
    "🤖 <b>Commandes</b>\n"
    "/statut — ce qui attend ta validation, ce qui est programmé\n"
    "/plan — les prochains créneaux\n"
    "/budget — dépenses API X et Claude\n"
    "/pause — plus rien n'est publié jusqu'à /reprendre\n"
    "/reprendre — relancer les publications\n"
    "/note <i>un fait réel</i> — un vrai truc que tu as fait ou testé ; Claude pourra "
    "le raconter (sans jamais rien inventer au-delà)\n"
    "/aide — ce message\n\n"
    "Sur chaque carte : ✅ valider · ✏️ modifier · 🔄 autre version · ❌ annuler.\n"
    "Pour modifier : appuie sur ✏️ puis <b>réponds</b> au message qui s'affiche."
)


# ---------------------------------------------------------------------------
# Petits utilitaires d'affichage
# ---------------------------------------------------------------------------
def _local(dt: datetime) -> datetime:
    return dt.astimezone(tz())


def fmt_hm(dt: datetime) -> str:
    return _local(dt).strftime("%H:%M")


def fmt_when(dt: datetime, now: datetime | None = None) -> str:
    """« à 14:00 », « demain à 08:30 », « le 17/10 à 15:00 »."""
    now = now or now_local()
    d = _local(dt).date()
    today = _local(now).date()
    hm = fmt_hm(dt)
    if d == today:
        return f"à {hm}"
    if d == today + timedelta(days=1):
        return f"demain à {hm}"
    if d == today - timedelta(days=1):
        return f"hier à {hm}"
    return f"le {d:%d/%m} à {hm}"


def _trunc(text: str, n: int | None) -> str:
    t = (text or "").strip()
    if n is None or len(t) <= n:
        return t
    return t[: max(1, n - 1)].rstrip() + "…"


def _href(url: str) -> str:
    return html.escape(url or "", quote=True)


def _fmt_int(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")


def _is_separator(line: str) -> bool:
    """Ligne « --- » (les claviers iOS transforment parfois « -- » en « — »)."""
    s = line.strip().replace(" ", "")
    return bool(s) and set(s) <= {"-", "–", "—"} and (len(s) >= 3 or "—" in s or "–" in s)


def split_thread(text: str) -> list[str]:
    """Découpe un thread écrit à la main : une ligne contenant seulement --- sépare les tweets."""
    parts: list[str] = []
    cur: list[str] = []
    for line in (text or "").splitlines():
        if _is_separator(line):
            parts.append("\n".join(cur))
            cur = []
        else:
            cur.append(line)
    parts.append("\n".join(cur))
    return [p.strip() for p in parts if p.strip()]


def check_human_text(text: str) -> tuple[str | None, str | None]:
    """(raison de refus, avertissement) pour un texte écrit par toi.

    Bloquant : vide, lien / nom de domaine, fuite JSON, trop long.
    Simple avertissement (accepté puisque c'est toi qui l'as écrit) : hors ligne
    éditoriale, jargon, trop de chiffres."""
    t = (text or "").strip()
    if not t:
        return "texte vide", None
    if contains_url(t):
        return "contient un lien ou un nom de domaine (0,20 $ le post via l'API au lieu de 0,015 $) — enlève-le", None
    if filters.looks_like_json_leak(t):
        return "ressemble à du JSON (accolades, \\n…)", None
    wl = filters.weighted_length(t)
    if wl > filters.MAX_WEIGHTED:
        return f"trop long ({wl}/{filters.MAX_WEIGHTED} caractères pondérés)", None
    return None, filters.publish_problem(t)


class _Answer:
    """Répond au plus une fois à un clic de bouton (sinon Telegram fait tourner le sablier)."""

    def __init__(self, tg, callback_id: str) -> None:
        self.tg = tg
        self.callback_id = callback_id
        self.done = False

    def __call__(self, text: str = "") -> None:
        if self.done or not self.callback_id:
            return
        self.done = True
        try:
            self.tg.answer_callback(self.callback_id, text)
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram : réponse au bouton impossible (%s)", type(e).__name__)


# ---------------------------------------------------------------------------
class Approvals:
    def __init__(self, settings, state, telegram, hooks: dict | None = None) -> None:
        self.settings = settings
        self.state = state
        self.tg = telegram
        self.hooks: dict[str, Callable] = dict(hooks or {})
        tgs = self.state.data.get("telegram")
        if not isinstance(tgs, dict):
            tgs = self.state.data["telegram"] = {}
        tgs.setdefault("offset", None)
        if not isinstance(tgs.get("edit_waiting"), dict):
            tgs["edit_waiting"] = {}

    # ------------------------------------------------------------------
    @property
    def auto(self) -> bool:
        return self.settings.approval_mode == "auto"

    def _tgs(self) -> dict:
        return self.state.data["telegram"]

    def _waiting(self) -> dict:
        tgs = self._tgs()
        if not isinstance(tgs.get("edit_waiting"), dict):
            tgs["edit_waiting"] = {}
        return tgs["edit_waiting"]

    def _quiet(self) -> bool:
        qh = self.settings.quiet_hours
        return bool(qh) and in_window(now_local().time(), qh)

    @staticmethod
    def _family(item: dict) -> str:
        kind = item.get("kind")
        return kind if kind in ("comment_reply", "radar") else "post"

    def _label(self, item: dict) -> tuple[str, str]:
        kind = item.get("kind") or ""
        emoji, label = KIND_LABELS.get(kind, ("📝", kind or "Post"))
        if kind == "thread":
            angle = str((item.get("meta") or {}).get("angle") or "").strip().lower()
            genre = THREAD_GENRES_FR.get(angle, angle)
            if genre:
                label = f"Thread ({genre})"
        return emoji, label

    # ------------------------------------------------------------------
    # Création des items + carte
    # ------------------------------------------------------------------
    def create_post_item(self, draft, *, slot_key: str | None, slot_date: str | None,
                         publish_at: datetime, expires_at: datetime,
                         image_path: str | None = None, image_kind: str | None = None) -> dict:
        now = now_local()
        sources = []
        for s in getattr(draft, "sources", None) or []:
            try:
                sources.append(s.brief() if hasattr(s, "brief") else dict(s))
            except Exception:  # noqa: BLE001
                continue
        meta: dict[str, Any] = {
            "reason": draft.reason or "",
            "angle": draft.angle or "",
            "sources": sources,
            "used_notes": list(getattr(draft, "used_notes", None) or []),
            "from_fallback": bool(draft.from_fallback),
            "scheduled_at": iso(publish_at),
        }
        fields: dict[str, Any] = {
            "status": "awaiting",
            "publish_at": iso(publish_at),
            "expires_at": iso(expires_at),
            "slot_key": slot_key,
            "slot_date": slot_date,
            "image_path": image_path or None,
            "image_kind": (image_kind if image_path else None),
            "poll": draft.poll or None,
            "meta": meta,
        }
        if self.auto:
            fields["status"] = "approved"
            fields["approved_at"] = iso(now)
            meta["auto"] = True
        item = self.state.new_item(draft.kind, [t for t in draft.texts], **fields)
        log.info("Item %s créé (%s, %s) pour %s", item["id"], item["kind"], item["status"], item["publish_at"])
        self._send_card(item, photo=True)
        return item

    def create_comment_item(self, *, text: str, comment_id: str, comment_text: str, commenter: str,
                            my_post_text: str, post_url: str, expires_at: datetime) -> dict:
        now = now_local()
        item = self.state.new_item(
            "comment_reply", [text], status="awaiting", publish_at=iso(now), expires_at=iso(expires_at),
            meta={"comment_id": str(comment_id), "comment_text": comment_text or "",
                  "commenter": (commenter or "").lstrip("@"), "my_post_text": my_post_text or "",
                  "post_url": post_url or ""},
        )
        log.info("Item %s créé (réponse à @%s)", item["id"], commenter)
        self._send_card(item)
        return item

    def create_radar_item(self, *, text: str, angle: str, tweet_id: str, author: str, post_text: str,
                          post_url: str, age_min: float | None, likes: int | None,
                          expires_at: datetime) -> dict:
        now = now_local()
        item = self.state.new_item(
            "radar", [text], status="suggested", publish_at=iso(now), expires_at=iso(expires_at),
            meta={"tweet_id": str(tweet_id), "author": (author or "").lstrip("@"), "post_text": post_text or "",
                  "post_url": post_url or "", "age_min": age_min, "likes": likes, "angle": angle or "",
                  "intent_url": intent_reply_url(str(tweet_id), text)},
        )
        log.info("Item %s créé (radar sous @%s)", item["id"], author)
        self._send_card(item)
        return item

    def _send_card(self, item: dict, photo: bool = False) -> int | None:
        if self.tg is None:
            return None
        try:
            silent = self._quiet()
            path = item.get("image_path")
            if photo and path and Path(path).is_file():
                emoji, label = self._label(item)
                what = "image IA" if item.get("image_kind") == "ai" else "capture d'écran"
                self.tg.send_photo(path, caption=f"{emoji} {esc(label)} — {what} proposée avec ce post")
            text, buttons = self._render(item)
            mid = self.tg.send(text, buttons=buttons or None, silent=silent)
            if mid is None:
                log.warning("Carte %s non envoyée (Telegram indisponible ?) : nouvel essai au prochain tour",
                            item["id"])
                return None
            self.state.update_item(item["id"], tg_message_id=mid)
            return mid
        except Exception as e:  # noqa: BLE001
            log.exception("Carte %s : envoi impossible : %s", item.get("id"), e)
            return None

    def _resend_missing_cards(self) -> None:
        """Cartes jamais arrivées (Telegram en panne au moment de la création) :
        nouvel essai toutes les CARD_RETRY_MINUTES, CARD_MAX_RETRIES fois au plus."""
        now = now_local()
        for it in self.state.items(statuses=("awaiting", "suggested", "approved")):
            if it.get("tg_message_id"):
                continue
            meta = it.setdefault("meta", {})
            tries = int(meta.get("card_retries") or 0)
            last = parse_iso(meta.get("card_retry_at"))
            if tries >= CARD_MAX_RETRIES or (last and now - last < timedelta(minutes=CARD_RETRY_MINUTES)):
                continue
            meta["card_retries"] = tries + 1
            meta["card_retry_at"] = iso(now)
            self.state.flush()
            if self._send_card(it) is None:
                break  # Telegram toujours indisponible : inutile d'insister ce tour-ci

    # ------------------------------------------------------------------
    # Rendu des cartes
    # ------------------------------------------------------------------
    def _render(self, item: dict, notice: str | None = None) -> tuple[str, list[list[dict]]]:
        family = self._family(item)
        text = ""
        for limit in (None, 220, 140, 80, 40):
            if family == "radar":
                text = self._render_radar_text(item, limit)
            elif family == "comment_reply":
                text = self._render_comment_text(item, limit)
            else:
                text = self._render_post_text(item, limit)
            if notice:
                text += f"\n\n{notice}"
            if len(text) <= CARD_MAX_CHARS:
                break
        if family == "radar":
            buttons = self._radar_buttons(item)
        elif family == "comment_reply":
            buttons = self._comment_buttons(item)
        else:
            buttons = self._post_buttons(item)
        return text, buttons

    def _extras(self, item: dict, lines: list[str]) -> None:
        meta = item.get("meta") or {}
        if meta.get("edited"):
            lines.append("✏️ Modifié par toi")
        for w in meta.get("warnings") or []:
            lines.append(f"⚠️ {esc(str(w))}")

    def _published_line(self, item: dict, what: str) -> str:
        ids = [str(i) for i in (item.get("published_ids") or [])]
        if not ids:
            return f"✅ {what}"
        handle = self.settings.handle
        if item.get("kind") == "thread" and len(ids) > 1:
            line = (f"✅ Thread publié ({len(ids)} tweets) : "
                    f"<a href=\"{_href(tweet_url(handle, ids[0]))}\">voir le thread</a>")
        else:
            line = f"✅ {what} : <a href=\"{_href(tweet_url(handle, ids[0]))}\">voir sur X</a>"
        if self.settings.dry_run or ids[0].startswith("dry-"):
            line += "\n🧪 DRY_RUN : rien n'a vraiment été publié"
        return line

    def _partial_line(self, item: dict) -> str | None:
        partial = [str(i) for i in ((item.get("meta") or {}).get("partial_ids") or [])]
        if not partial:
            return None
        total = len(item.get("texts") or [])
        handle = self.settings.handle
        return (f"🧩 Déjà en ligne : {len(partial)}/{total} tweets — "
                f"<a href=\"{_href(tweet_url(handle, partial[0]))}\">début</a> · "
                f"<a href=\"{_href(tweet_url(handle, partial[-1]))}\">dernier publié</a>")

    def _approved_line(self, item: dict, now: datetime, immediate: str, scheduled: str) -> str:
        meta = item.get("meta") or {}
        pa = parse_iso(item.get("publish_at"))
        if item.get("last_error") and pa and pa > now:
            line = f"🔁 Nouvel essai {fmt_when(pa, now)} — {esc(item['last_error'])}"
        elif meta.get("auto") and pa:
            line = f"🤖 Mode auto : publication automatique {fmt_when(pa, now)}" if pa > now else \
                "🤖 Mode auto : publication automatique imminente"
        elif pa and pa > now:
            line = f"{scheduled} {fmt_when(pa, now)}"
        else:
            line = immediate
        if self.state.data.get("paused"):
            line += "\n⏸️ Bot en pause : rien ne part avant /reprendre"
        return line

    # ---- Posts (gm / tip / news / thread / réactif) -------------------
    def _render_post_text(self, item: dict, limit: int | None) -> str:
        now = now_local()
        meta = item.get("meta") or {}
        status = item.get("status")
        emoji, label = self._label(item)
        pa = parse_iso(item.get("publish_at"))
        head = f"{emoji} <b>{esc(label)}</b>"
        if pa and status in ("awaiting", "approved"):
            head += f" · prévu {fmt_when(pa, now)}"
        lines = [head, ""]
        texts = list(item.get("texts") or [])
        if item.get("kind") == "thread" or len(texts) > 1:
            for i, t in enumerate(texts, 1):
                lines.append(f"<b>{i}/</b> {esc(_trunc(t, limit))}")
                lines.append(f"<i>{filters.weighted_length(t)}/{filters.MAX_WEIGHTED}</i>")
                lines.append("")
        else:
            t = texts[0] if texts else ""
            lines.append(esc(_trunc(t, limit)))
            lines.append("")
            lines.append(f"📏 {filters.weighted_length(t)}/{filters.MAX_WEIGHTED}")
        poll = item.get("poll") or None
        if poll and poll.get("options"):
            dur = int(poll.get("duration_minutes") or 1440)
            dur_txt = f"{dur // 60} h" if dur % 60 == 0 else f"{dur} min"
            lines.append(f"📊 Sondage ({dur_txt}) : " + " · ".join(esc(str(o)) for o in poll["options"]))
        if item.get("image_path"):
            if poll:
                lines.append("⚠️ Sondage + image : X n'accepte pas les deux, l'image ne sera pas publiée")
            elif item.get("image_kind") == "ai":
                lines.append("🎨 Image IA jointe (photo au-dessus, étiquette « Made with AI »)")
            else:
                lines.append("🖼️ Capture d'écran jointe (photo au-dessus)")
        reason = str(meta.get("reason") or "").strip()
        if reason:
            lines.append(f"💭 Pourquoi : {esc(_trunc(reason, 300 if limit is None else 120))}")
        srcs = []
        for s in (meta.get("sources") or [])[:3]:
            if not isinstance(s, dict):
                continue
            title = _trunc(str(s.get("title") or ""), 90 if limit is None else 50)
            src = str(s.get("source") or "").strip()
            if title:
                srcs.append(esc(title) + (f" ({esc(src)})" if src else ""))
        if srcs:
            lines.append("📚 Sources : " + " · ".join(srcs))
        notes = [n for n in (meta.get("used_notes") or []) if isinstance(n, str) and n.strip()]
        if notes:
            lines.append("📝 Utilise ta note : " + " · ".join(f"«{esc(_trunc(n, 80))}»" for n in notes[:2]))
        if meta.get("from_fallback") and not meta.get("edited"):
            lines.append("🧯 Texte de secours (Claude n'a rien proposé de publiable)")
        self._extras(item, lines)
        lines.append("")
        lines.append(self._post_status_line(item, now))
        partial = self._partial_line(item)
        if partial and status != "published":
            lines.append(partial)
        return "\n".join(lines).strip()

    def _post_status_line(self, item: dict, now: datetime) -> str:
        status = item.get("status")
        if status == "awaiting":
            exp = parse_iso(item.get("expires_at"))
            pa = parse_iso(item.get("publish_at"))
            line = "⏳ À valider" + (f" ({fmt_when(exp, now).replace('à ', 'avant ', 1)})" if exp else "")
            if pa and pa <= now:
                line += "\nL'heure prévue est passée : ✅ publie tout de suite"
            return line
        if status == "approved":
            return self._approved_line(item, now, "✅ Validé — publication immédiate", "✅ Validé — publication")
        if status == "publishing":
            return "⏳ Publication en cours…"
        if status == "published":
            return self._published_line(item, "Publié")
        if status == "failed":
            return f"❌ Échec : {esc(item.get('last_error') or 'erreur inconnue')}"
        if status == "rejected":
            m = item.get("meta") or {}
            if m.get("uncertain") or m.get("partial_ids"):
                return ("❌ Arrêté — mais X n'avait pas confirmé : il est peut-être déjà en ligne, "
                        "vérifie ton profil")
            return "❌ Annulé — rien n'a été publié"
        if status == "expired":
            why = item.get("last_error")
            return "⌛ Expiré — rien n'a été publié" + (f" ({esc(why)})" if why else "")
        return f"Statut : {esc(str(status))}"

    def _post_buttons(self, item: dict) -> list[list[dict]]:
        status = item.get("status")
        iid = item["id"]
        texts = item.get("texts") or []
        single = len(texts) == 1 and item.get("kind") != "thread"
        now = now_local()
        pa = parse_iso(item.get("publish_at"))
        if status == "awaiting":
            rows = [[button("✅ Valider", f"ok:{iid}:{int(item.get('rev') or 0)}"),
                     button("✏️ Modifier", f"edit:{iid}")]]
            row2 = []
            if int(item.get("regen_count") or 0) < MAX_REGEN:
                row2.append(button("🔄 Autre version", f"regen:{iid}"))
            row2.append(button("❌ Annuler", f"no:{iid}"))
            rows.append(row2)
            if single and not item.get("image_path") and not item.get("poll"):
                rows.append([button("📲 Publier moi-même", url=intent_post_url(texts[0]))])
            return rows
        if status == "approved" and pa and pa > now:
            m = item.get("meta") or {}
            if m.get("partial_ids") or m.get("uncertain"):
                # Peut-être (en partie) déjà en ligne : modifier ferait un 2e post.
                return [[button("❌ Ne plus réessayer", f"no:{iid}")]]
            return [[button("✏️ Modifier", f"edit:{iid}"), button("❌ Annuler", f"no:{iid}")]]
        if status == "published":
            ids = item.get("published_ids") or []
            if ids:
                return [[button("🔗 Voir sur X", url=tweet_url(self.settings.handle, str(ids[0])))]]
        if status == "failed" and single and texts:
            return [[button("📲 Publier moi-même", url=intent_post_url(texts[0]))]]
        return []

    # ---- Réponses à mes commentaires ---------------------------------
    def _render_comment_text(self, item: dict, limit: int | None) -> str:
        now = now_local()
        meta = item.get("meta") or {}
        status = item.get("status")
        who = esc(str(meta.get("commenter") or "?"))
        lines = [f"💬 <b>@{who}</b> a répondu à ton post : «{esc(_trunc(meta.get('comment_text') or '', 500))}»"]
        my_post = str(meta.get("my_post_text") or "").strip()
        if my_post:
            lines.append(f"<i>Ton post : «{esc(_trunc(my_post, 160))}»</i>")
        lines.append("")
        lines.append("Ta réponse :" if status == "published" else "Réponse proposée :")
        t = (item.get("texts") or [""])[0]
        lines.append(esc(_trunc(t, limit)))
        lines.append("")
        lines.append(f"📏 {filters.weighted_length(t)}/{filters.MAX_WEIGHTED}")
        self._extras(item, lines)
        lines.append("")
        if status == "awaiting":
            exp = parse_iso(item.get("expires_at"))
            lines.append("⏳ À valider" + (f" ({fmt_when(exp, now).replace('à ', 'avant ', 1)})" if exp else ""))
        elif status == "approved":
            lines.append(self._approved_line(item, now, "✅ Validé — envoi immédiat", "✅ Validé — envoi"))
        elif status == "publishing":
            lines.append("⏳ Envoi en cours…")
        elif status == "published":
            lines.append(self._published_line(item, "Réponse envoyée"))
        elif status == "failed":
            lines.append(f"❌ Échec : {esc(item.get('last_error') or 'erreur inconnue')}")
            lines.append("📲 Réponds toi-même : le texte est pré-rempli dans le bouton ci-dessous.")
        elif status == "rejected":
            lines.append("❌ Ignoré — rien n'a été envoyé")
        elif status == "expired":
            lines.append("⌛ Expiré — rien n'a été envoyé")
        return "\n".join(lines).strip()

    def _comment_buttons(self, item: dict) -> list[list[dict]]:
        status = item.get("status")
        iid = item["id"]
        meta = item.get("meta") or {}
        post_url = str(meta.get("post_url") or "")
        voir = [button("🔗 Voir", url=post_url)] if post_url else []
        if status == "awaiting":
            rows = [[button("✅ Envoyer", f"ok:{iid}:{int(item.get('rev') or 0)}"),
                     button("✏️ Modifier", f"edit:{iid}")]]
            row2 = []
            if int(item.get("regen_count") or 0) < MAX_REGEN:
                row2.append(button("🔄 Autre", f"regen:{iid}"))
            row2.append(button("❌ Ignorer", f"no:{iid}"))
            rows.append(row2)
            if voir:
                rows.append(voir)
            return rows
        if status == "failed":
            texts = item.get("texts") or [""]
            rows = []
            if meta.get("comment_id"):
                rows.append([button("📲 Répondre moi-même",
                                    url=intent_reply_url(str(meta["comment_id"]), texts[0]))])
            if voir:
                rows.append(voir)
            return rows
        if status == "published":
            ids = item.get("published_ids") or []
            if ids:
                return [[button("🔗 Voir ma réponse", url=tweet_url(self.settings.handle, str(ids[0])))]]
        return []

    # ---- Radar (c'est toi qui publies) --------------------------------
    def _render_radar_text(self, item: dict, limit: int | None) -> str:
        meta = item.get("meta") or {}
        status = item.get("status")
        bits = []
        age = meta.get("age_min")
        if isinstance(age, (int, float)):
            bits.append(f"{max(0, round(age))} min")
        likes = meta.get("likes")
        if isinstance(likes, (int, float)):
            bits.append(f"{_fmt_int(likes)} likes")
        head = f"🎯 <b>@{esc(str(meta.get('author') or '?'))}</b> vient de poster"
        if bits:
            head += f" ({', '.join(bits)})"
        lines = [head, f"«{esc(_trunc(meta.get('post_text') or '', 400 if limit is None else 160))}»", ""]
        lines.append("Réponse suggérée — c'est toi qui la publies :")
        t = (item.get("texts") or [""])[0]
        lines.append(esc(_trunc(t, limit)))
        lines.append("")
        lines.append(f"📏 {filters.weighted_length(t)}/{filters.MAX_WEIGHTED}")
        angle = str(meta.get("angle") or "").strip()
        if angle:
            lines.append(f"💡 Angle : {esc(angle)}")
        self._extras(item, lines)
        lines.append("")
        if status == "suggested":
            exp = parse_iso(item.get("expires_at"))
            lines.append("📲 Touche « Répondre dans X » : l'app s'ouvre avec le texte, tu publies toi-même."
                         + (f"\n⏳ Valable jusqu'à {fmt_hm(exp)}" if exp else ""))
        elif status == "dismissed":
            lines.append("❌ Passé")
        elif status == "expired":
            lines.append("⌛ Plus d'actualité")
        else:
            lines.append(f"Statut : {esc(str(status))}")
        return "\n".join(lines).strip()

    def _radar_buttons(self, item: dict) -> list[list[dict]]:
        if item.get("status") != "suggested":
            return []
        iid = item["id"]
        meta = item.get("meta") or {}
        text = (item.get("texts") or [""])[0]
        intent = intent_reply_url(str(meta.get("tweet_id") or ""), text)
        rows = [[button("📲 Répondre dans X", url=intent)]]
        row2 = []
        if int(item.get("regen_count") or 0) < MAX_REGEN:
            row2.append(button("🔄 Autre réponse", f"regen:{iid}"))
        row2.append(button("❌ Passer", f"no:{iid}"))
        rows.append(row2)
        if meta.get("post_url"):
            rows.append([button("🔗 Voir le post", url=str(meta["post_url"]))])
        return rows

    # ------------------------------------------------------------------
    # Mise à jour d'une carte / notifications
    # ------------------------------------------------------------------
    def refresh_card(self, item: dict, notice: str | None = None) -> None:
        if self.tg is None or not item:
            return
        try:
            text, buttons = self._render(item, notice)
            mid = item.get("tg_message_id")
            if mid:
                if not self.tg.edit(int(mid), text, buttons):
                    log.info("Carte %s : édition refusée par Telegram (inchangée ou supprimée)", item.get("id"))
            else:
                mid = self.tg.send(text, buttons=buttons or None, silent=self._quiet())
                if mid:
                    self.state.update_item(item["id"], tg_message_id=mid)
        except Exception as e:  # noqa: BLE001
            log.exception("Carte %s : mise à jour impossible : %s", item.get("id"), e)

    def notify(self, text: str, silent: bool = False) -> int | None:
        if self.tg is None:
            log.info("Notification (pas de Telegram) : %s", (text or "")[:300])
            return None
        silent = silent or self._quiet()
        try:
            mid = self.tg.send(text, silent=silent)
            if mid is None and "parse" in str(getattr(self.tg, "last_error", "") or "").lower():
                # HTML invalide (un « < » non échappé…) : on réessaie en texte brut.
                # (Pas après une coupure réseau : le message est peut-être déjà arrivé.)
                mid = self.tg.send(esc(text), silent=silent)
            return mid
        except Exception as e:  # noqa: BLE001
            log.warning("Notification Telegram impossible (%s)", type(e).__name__)
            return None

    # ------------------------------------------------------------------
    # Expiration
    # ------------------------------------------------------------------
    def expire_due(self) -> int:
        now = now_local()
        n = 0
        for it in self.state.items(statuses=("awaiting", "suggested")):
            try:
                exp = parse_iso(it.get("expires_at"))
                if exp is None or exp >= now:
                    continue
                self.state.update_item(it["id"], status="expired")
                self._forget_edit_prompts(it["id"])
                log.info("Item %s (%s) expiré sans validation", it["id"], it.get("kind"))
                self.refresh_card(it)
                n += 1
            except Exception as e:  # noqa: BLE001
                log.exception("Expiration de %s impossible : %s", it.get("id"), e)
        return n

    def _forget_edit_prompts(self, item_id: str) -> None:
        waiting = self._waiting()
        stale = [k for k, v in waiting.items() if v == item_id]
        for k in stale:
            del waiting[k]
        if stale:
            self.state.flush()

    # ------------------------------------------------------------------
    # Lecture des messages Telegram
    # ------------------------------------------------------------------
    def _recover_interrupted_regens(self) -> None:
        """Bot tué pendant un 🔄 : la carte affichait « en cours » sans ✅. On la remet."""
        for it in self.state.items(statuses=("awaiting", "suggested", "approved")):
            meta = it.get("meta") or {}
            if meta.pop("regen_in_progress", None):
                self.state.update_item(it["id"], meta=meta)
                self.refresh_card(it, notice="⚠️ La nouvelle version a été interrompue : voici la version actuelle.")

    def poll(self) -> int:
        if self.tg is None:
            return 0
        if not getattr(self, "_regens_recovered", False):
            self._regens_recovered = True
            try:
                self._recover_interrupted_regens()
            except Exception as e:  # noqa: BLE001
                log.warning("Cartes interrompues : remise en état impossible (%s)", e)
        try:
            self._resend_missing_cards()
        except Exception as e:  # noqa: BLE001
            log.warning("Renvoi des cartes manquantes impossible : %s", e)
        tgs = self._tgs()
        offset = tgs.get("offset")
        try:
            updates = self.tg.updates(offset) or []
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram : lecture des messages impossible (%s)", type(e).__name__)
            return 0
        if offset is None:
            return self._skip_backlog(updates)

        count = 0
        for upd in sorted(updates, key=lambda u: u.get("update_id") or 0):
            uid = upd.get("update_id")
            # L'offset avance AVANT le traitement : un message qui ferait planter
            # le bot ne sera pas rejoué en boucle.
            if isinstance(uid, int) and uid + 1 > int(tgs.get("offset") or 0):
                tgs["offset"] = uid + 1
                self.state.flush()
            try:
                if self._handle_update(upd):
                    count += 1
            except Exception as e:  # noqa: BLE001
                log.exception("Telegram : message %s mal traité : %s", uid, e)
        return count

    def _skip_backlog(self, updates: list[dict]) -> int:
        """Premier démarrage : on ignore l'historique (vieux clics, vieux messages)."""
        tgs = self._tgs()
        skipped = 0
        try:
            for _ in range(20):
                ids = [u.get("update_id") for u in updates if isinstance(u.get("update_id"), int)]
                if not ids:
                    break
                tgs["offset"] = max(ids) + 1
                skipped += len(updates)
                updates = self.tg.updates(tgs["offset"]) or []
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram : lecture de l'historique interrompue (%s)", type(e).__name__)
        if tgs.get("offset") is None:
            tgs["offset"] = 0
        self.state.flush()
        if skipped:
            log.info("Telegram : %d ancien(s) message(s) ignoré(s) au premier démarrage", skipped)
        return 0

    def _handle_update(self, upd: dict) -> bool:
        cq = upd.get("callback_query")
        if isinstance(cq, dict):
            return self._on_callback(cq)
        msg = upd.get("message")
        if isinstance(msg, dict):
            return self._on_message(msg)
        return False

    # ---- Boutons ------------------------------------------------------
    def _on_callback(self, cq: dict) -> bool:
        ans = _Answer(self.tg, str(cq.get("id") or ""))
        try:
            chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
            if not self._from_owner(chat_id, cq.get("from")):
                log.warning("Telegram : bouton venant d'un autre chat ou d'une autre personne ignoré")
                return False
            action, item_id, rev = (str(cq.get("data") or "").split(":") + ["", ""])[:3]
            self._callback(action, item_id.strip(), ans, rev.strip() or None)
            return True
        finally:
            ans("")

    def _from_owner(self, chat_id: Any, sender: Any) -> bool:
        """Seul le propriétaire, dans son chat privé avec le bot, peut agir (dans un
        groupe, n'importe quel membre pourrait sinon valider une publication)."""
        sender_id = (sender or {}).get("id") if isinstance(sender, dict) else None
        return self.tg.is_owner_chat(chat_id) and sender_id is not None and self.tg.is_owner_chat(sender_id)

    def _callback(self, action: str, item_id: str, ans: _Answer, rev: str | None = None) -> None:
        item = self.state.item(item_id) if item_id else None
        if item is None:
            ans("Introuvable (trop ancien ?)")
            return
        allowed = _ACTIONABLE.get(action)
        if allowed is None:
            ans("Action inconnue")
            return
        if item.get("status") not in allowed[self._family(item)]:
            ans("Déjà traité")
            return
        if action == "ok" and rev is not None and rev != str(int(item.get("rev") or 0)):
            # Le texte a changé depuis l'affichage de ce bouton (🔄 ou ✏️ en cours) :
            # on ne valide jamais une version que tu n'as pas lue.
            ans("Le texte a changé : relis la carte, puis ✅")
            self.refresh_card(item)
            return
        if action == "edit" and ((item.get("meta") or {}).get("partial_ids")
                                 or (item.get("meta") or {}).get("uncertain")):
            ans("Peut-être déjà (en partie) en ligne : plus modifiable")
            return
        log.info("Telegram : bouton %s sur %s (%s)", action, item_id, item.get("kind"))
        {"ok": self._cb_ok, "no": self._cb_no, "regen": self._cb_regen, "edit": self._cb_edit}[action](item, ans)

    def _cb_ok(self, item: dict, ans: _Answer) -> None:
        from .publisher import is_stale
        now = now_local()
        pa = parse_iso(item.get("publish_at")) or now
        exp = parse_iso(item.get("expires_at"))
        meta = item.setdefault("meta", {})
        if item.get("kind") == "comment_reply":
            meta["scheduled_at"] = iso(now)
        # Bouton touché trop tard (ou traité au redémarrage du bot) : rien ne part.
        if (exp is not None and exp < now) or is_stale(item, now):
            self.state.update_item(item["id"], status="expired", meta=meta,
                                   last_error="validé trop tard, après le délai")
            self._forget_edit_prompts(item["id"])
            ans("⌛ Trop tard : délai dépassé, rien n'est publié")
            self.refresh_card(item)
            return
        fields: dict[str, Any] = {"status": "approved", "approved_at": iso(now), "meta": meta}
        immediate = item.get("kind") == "comment_reply" or pa <= now
        if immediate:
            fields["publish_at"] = iso(now)
        self.state.update_item(item["id"], **fields)
        if self.state.data.get("paused"):
            ans("✅ Validé — bot en pause, ça partira à la reprise")
        elif immediate:
            ans("✅ Validé — publication immédiate")
        else:
            ans(f"✅ Validé — publication {fmt_when(pa, now)}")
        self.refresh_card(item)

    def _cb_no(self, item: dict, ans: _Answer) -> None:
        kind = item.get("kind")
        self.state.update_item(item["id"], status="dismissed" if kind == "radar" else "rejected")
        self._forget_edit_prompts(item["id"])
        ans({"radar": "Suggestion écartée", "comment_reply": "Ignoré"}.get(kind or "", "Annulé"))
        self.refresh_card(item)

    def _cb_regen(self, item: dict, ans: _Answer) -> None:
        count = int(item.get("regen_count") or 0)
        if count >= MAX_REGEN:
            ans(f"Maximum {MAX_REGEN} nouvelles versions — ✏️ Modifier pour écrire la tienne")
            return
        family = self._family(item)
        hook_name = {"radar": "regen_radar", "comment_reply": "regen_comment"}.get(family, "regenerate")
        hook = self.hooks.get(hook_name)
        if not callable(hook):
            ans("Nouvelle version indisponible pour l'instant")
            return
        # Réponse tout de suite : Claude peut mettre un moment, Telegram n'attend pas.
        ans("🔄 Je rédige une autre version…")
        item.setdefault("meta", {})["regen_in_progress"] = True
        self.state.update_item(item["id"], regen_count=count + 1, meta=item["meta"])
        mid = item.get("tg_message_id")
        if mid:
            try:
                text, _ = self._render(item, "🔄 Nouvelle version en cours…")
                self.tg.edit(int(mid), text, [[button("❌ Annuler", f"no:{item['id']}")]])
            except Exception:  # noqa: BLE001
                pass
        try:
            result = hook(item)
        except Exception as e:  # noqa: BLE001
            log.exception("Nouvelle version de %s : le crochet %s a planté : %s", item["id"], hook_name, e)
            result = None
        finally:
            item["meta"].pop("regen_in_progress", None)
            self.state.update_item(item["id"], meta=item["meta"])
        if not self._apply_regen(item, family, result):
            self.refresh_card(item, notice="⚠️ Pas de nouvelle version publiable cette fois. "
                                           "Garde celle-ci, ✏️ modifie-la ou ❌ annule.")
            return
        log.info("Item %s : nouvelle version %d/%d", item["id"], count + 1, MAX_REGEN)
        self.refresh_card(item)

    def _apply_regen(self, item: dict, family: str, result: Any) -> bool:
        meta = item.setdefault("meta", {})
        if family == "radar":
            if not (isinstance(result, (tuple, list)) and len(result) == 2
                    and isinstance(result[0], str) and result[0].strip()):
                return False
            text, angle = result[0].strip(), str(result[1] or "")
            meta["angle"] = angle or meta.get("angle", "")
            meta["intent_url"] = intent_reply_url(str(meta.get("tweet_id") or ""), text)
            texts = [text]
            # Anti-répétition : le Writer relit les suggestions radar récentes.
            if text not in self.state.recent_texts("radar", 30):
                self.state.remember_text("radar", text)
        elif family == "comment_reply":
            if not isinstance(result, str) or not result.strip():
                return False
            texts = [result.strip()]
        else:
            draft_texts = getattr(result, "texts", None)
            if not draft_texts or not all(isinstance(t, str) and t.strip() for t in draft_texts):
                return False
            texts = [t for t in draft_texts]
            item["poll"] = getattr(result, "poll", None) or None
            if getattr(result, "reason", ""):
                meta["reason"] = result.reason
            if getattr(result, "angle", ""):
                meta["angle"] = result.angle
            srcs = getattr(result, "sources", None) or []
            if srcs:
                meta["sources"] = [s.brief() if hasattr(s, "brief") else dict(s) for s in srcs]
            meta["used_notes"] = list(getattr(result, "used_notes", None) or [])
            meta["from_fallback"] = bool(getattr(result, "from_fallback", False))
        meta["edited"] = False
        meta["warnings"] = []
        self.state.update_item(item["id"], texts=texts, meta=meta, rev=int(item.get("rev") or 0) + 1)
        return True

    def _cb_edit(self, item: dict, ans: _Answer) -> None:
        texts = item.get("texts") or [""]
        thread = item.get("kind") == "thread"
        current = "\n---\n".join(texts) if thread else texts[0]
        lines = ["✏️ <b>Réponds à CE message</b> avec le nouveau texte."]
        if thread:
            lines.append("Thread : sépare les tweets par une ligne contenant seulement <code>---</code>")
        if len(current) <= EDIT_PROMPT_MAX_TEXT:
            lines.append("")
            lines.append("Texte actuel (touche pour copier) :")
            lines.append(f"<code>{esc(current)}</code>")
        mid = self.tg.send("\n".join(lines), reply_to=item.get("tg_message_id"), force_reply=True)
        if mid is None:
            ans("Telegram indisponible, réessaie dans un instant")
            return
        self._waiting()[str(mid)] = item["id"]
        self.state.flush()
        ans("✏️ Envoie le nouveau texte en réponse")

    # ---- Messages -----------------------------------------------------
    def _on_message(self, msg: dict) -> bool:
        chat_id = (msg.get("chat") or {}).get("id")
        if not self._from_owner(chat_id, msg.get("from")):
            log.warning("Telegram : message venant d'un autre chat ou d'une autre personne ignoré")
            return False
        text = str(msg.get("text") or msg.get("caption") or "")
        reply_id = (msg.get("reply_to_message") or {}).get("message_id")
        waiting = self._waiting()
        if reply_id is not None and str(reply_id) in waiting:
            self._apply_edit(waiting[str(reply_id)], str(reply_id), text)
            return True
        self._command(text)
        return True

    def _apply_edit(self, item_id: str, prompt_id: str, text: str) -> None:
        waiting = self._waiting()
        item = self.state.item(item_id)
        m = (item or {}).get("meta") or {}
        if item is None or item.get("status") not in _ACTIONABLE["edit"][self._family(item)] \
                or m.get("partial_ids") or m.get("uncertain"):
            waiting.pop(prompt_id, None)
            self.state.flush()
            status = (item or {}).get("status", "supprimé")
            why = ("il est peut-être déjà (en partie) en ligne" if m.get("partial_ids") or m.get("uncertain")
                   else f"statut : {esc(str(status))}")
            self.notify(f"⌛ Trop tard : ce brouillon n'est plus modifiable ({why}). Rien n'a été changé.")
            return
        raw = (text or "").strip()
        if not raw:
            self.notify("✏️ Envoie le nouveau texte <b>en texte</b>, en réponse au message ✏️.")
            return
        thread = item.get("kind") == "thread"
        parts = split_thread(raw) if thread else [raw]
        if not parts:
            self.notify("✏️ Je n'ai trouvé aucun texte. Réponds de nouveau au message ✏️.")
            return
        problems: list[str] = []
        warnings: list[str] = []
        for i, part in enumerate(parts, 1):
            prefix = f"tweet {i} : " if len(parts) > 1 else ""
            block, warn = check_human_text(part)
            if block:
                problems.append(prefix + block)
            elif warn:
                warnings.append(prefix + warn)
        if problems:
            self.notify("⛔ Texte refusé, rien n'a changé :\n• " + "\n• ".join(esc(p) for p in problems)
                        + "\n\nCorrige et réponds de nouveau au message ✏️.")
            return
        meta = item.setdefault("meta", {})
        meta["edited"] = True
        meta["warnings"] = warnings
        if item.get("kind") == "radar":
            meta["intent_url"] = intent_reply_url(str(meta.get("tweet_id") or ""), parts[0])
        self.state.update_item(item_id, texts=parts, meta=meta, rev=int(item.get("rev") or 0) + 1)
        waiting.pop(prompt_id, None)
        self.state.flush()
        log.info("Item %s : texte modifié à la main (%d partie(s))", item_id, len(parts))
        self.refresh_card(item)
        msg = "✏️ Texte mis à jour" + (f" ({len(parts)} tweets)" if thread else "") + "."
        status = item.get("status")
        if status == "awaiting":
            msg += " Appuie sur ✅ sur la carte pour le valider."
        elif status == "approved":
            pa = parse_iso(item.get("publish_at"))
            if pa:
                msg += f" Il partira {fmt_when(pa)}."
        if warnings:
            msg += ("\n⚠️ À vérifier (accepté, c'est toi qui l'as écrit) :\n• "
                    + "\n• ".join(esc(w) for w in warnings))
        self.notify(msg)

    # ---- Commandes ----------------------------------------------------
    def _command(self, text: str) -> None:
        raw = (text or "").strip()
        if not raw:
            return
        parts = raw.split(None, 1)
        word = parts[0]
        slash = word.startswith("/")
        cmd = word.lstrip("/").split("@", 1)[0].rstrip(":").lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        cmd = _COMMAND_ALIASES.get(cmd, "")
        if cmd == "note":
            self._cmd_note(arg)
            return
        # Sans « / », un mot-commande doit être seul (« pause café » n'est pas /pause).
        if not cmd or (not slash and arg):
            self.notify("🤷 Je n'ai pas compris.\n\n" + HELP_TEXT)
            return
        log.info("Telegram : commande /%s", cmd)
        if cmd == "aide":
            self.notify(HELP_TEXT)
        elif cmd == "statut":
            self.notify(self._call_hook("status") or self._default_status())
        elif cmd == "plan":
            self.notify(self._call_hook("plan") or "🗓️ Planning indisponible pour l'instant.")
        elif cmd == "budget":
            self.notify(self._call_hook("budget") or "💰 Budget indisponible pour l'instant.")
        elif cmd == "pause":
            self.state.data["paused"] = True
            self.state.flush()
            self.notify("⏸️ Pause activée : plus rien n'est publié. Les posts validés attendent. "
                        "/reprendre pour relancer.")
        elif cmd == "reprendre":
            was = bool(self.state.data.get("paused"))
            self.state.data["paused"] = False
            self.state.flush()
            from .publisher import is_stale
            now = now_local()
            late = [it for it in self.state.items(statuses=("approved",))
                    if (parse_iso(it.get("publish_at")) or now) <= now]
            # Les posts en retard partent dans quelques minutes (le temps d'annuler),
            # ceux qui ont trop attendu ne partent plus (même règle que le Publisher).
            resume_at = now + timedelta(minutes=RESUME_DELAY_MINUTES)
            # Évalué à l'heure où ils partiraient, pour ne rien annoncer qui expirerait entre-temps.
            go = [it for it in late if not is_stale(it, resume_at)]
            old = [it for it in late if is_stale(it, resume_at)]
            for it in go:
                self.state.update_item(it["id"], publish_at=iso(resume_at))
                self.refresh_card(it)
            for it in old:
                self.state.update_item(it["id"], status="expired",
                                       last_error="trop de retard pendant la pause : pas publié")
                self.refresh_card(it)
            msg = "▶️ C'est reparti." if was else "▶️ Le bot n'était pas en pause."
            if go:
                msg += (f"\n{len(go)} post(s) validé(s) en retard partiront à {resume_at:%H:%M} "
                        "(❌ Annuler sur leur carte d'ici là si tu préfères pas).")
            if old:
                msg += f"\n⌛ {len(old)} post(s) validé(s) trop ancien(s) ne partiront pas."
            self.notify(msg)

    def _cmd_note(self, arg: str) -> None:
        if not arg:
            self.notify("📝 Usage : /note <i>un fait réel</i>\nEx. : /note j'ai testé Claude pour trier "
                        "200 mails clients, 1 h gagnée")
            return
        self.state.add_note(arg)
        log.info("Telegram : note perso ajoutée (%d caractères)", len(arg))
        self.notify(f"📝 Noté : «{esc(_trunc(arg, 300))}»\nClaude pourra s'en servir (2 fois max, "
                    "pendant 14 jours), sans rien inventer au-delà.")

    def _call_hook(self, name: str) -> str | None:
        hook = self.hooks.get(name)
        if not callable(hook):
            return None
        try:
            out = hook()
        except Exception as e:  # noqa: BLE001
            log.exception("Crochet %s : erreur : %s", name, e)
            return f"⚠️ /{name} : erreur ({esc(type(e).__name__)}), regarde les logs."
        return str(out) if out else None

    def _default_status(self) -> str:
        paused = bool(self.state.data.get("paused"))
        awaiting = self.state.items(statuses=("awaiting",))
        approved = self.state.items(statuses=("approved",))
        suggested = self.state.items(statuses=("suggested",))
        lines = [f"🤖 Bot {'⏸️ en pause' if paused else '▶️ actif'}",
                 f"⏳ {len(awaiting)} brouillon(s) à valider",
                 f"🗓️ {len(approved)} publication(s) programmée(s)",
                 f"🎯 {len(suggested)} suggestion(s) radar en cours"]
        return "\n".join(lines)
