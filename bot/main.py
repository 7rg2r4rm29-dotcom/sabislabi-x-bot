"""Point d'entrée du bot.

  python -m bot.main                 lance le bot (service systemd)
  python -m bot.main --check         vérifie la config, Telegram, l'API X et Claude
  python -m bot.main --once JOB      lance une seule étape (plan, publish, comments,
                                     radar, reactive, metrics, daily, weekly, poll)
  python -m bot.main --busy-check [--state CHEMIN]
                                     code 3 si une publication est en cours ou
                                     imminente (< 15 min), 0 sinon (deploy.sh)
  --log-level DEBUG|INFO|…           (accepté comme en v1)

Codes de sortie : 0 OK, 1 échec d'une vérification, 2 configuration invalide,
3 occupé (--busy-check).

Seules la bibliothèque standard et bot.utils sont importées au chargement :
`--busy-check` tourne avec le code de la NOUVELLE version avant même
l'installation de ses dépendances, et ne fait que LIRE l'état.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Callable

from . import utils

log = logging.getLogger("xbot.main")

JOBS = ("plan", "publish", "comments", "radar", "reactive", "metrics", "daily", "weekly", "poll")
EXIT_OK, EXIT_FAIL, EXIT_CONFIG, EXIT_BUSY = 0, 1, 2, 3
BUSY_AHEAD_MIN = 15      # publication prévue dans moins de 15 min → occupé
BUSY_STALE_MIN = 30      # au-delà, un item en retard est considéré bloqué (bot arrêté), pas « en cours »

KIND_FR = {"gm": "GM", "tip": "astuce", "news": "actu", "thread": "thread", "reactive": "post réactif",
           "comment_reply": "réponse à un commentaire"}


# ---------------------------------------------------------------------------
# Environnement
# ---------------------------------------------------------------------------
def load_env() -> None:
    """config/.env puis .env (sans écraser ce qui est déjà défini). XBOT_ENV_FILE :
    un autre fichier (vérifier une nouvelle version avec les clés déjà installées)."""
    import os
    try:
        from dotenv import load_dotenv
    except ImportError:
        print("⚠️ python-dotenv absent : seules les variables d'environnement déjà définies sont lues.",
              file=sys.stderr)
        return
    forced = os.getenv("XBOT_ENV_FILE", "").strip()
    candidates = [Path(forced)] if forced else [utils.PROJECT_ROOT / "config" / ".env", utils.PROJECT_ROOT / ".env"]
    for candidate in candidates:
        if candidate.is_file():
            load_dotenv(candidate, override=False)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m bot.main", description="Assistant X de @sabislabi (v2)")
    p.add_argument("--check", action="store_true", help="vérifie la config, Telegram, l'API X et Claude")
    p.add_argument("--once", choices=JOBS, metavar="JOB", help=f"une seule étape : {', '.join(JOBS)}")
    p.add_argument("--busy-check", action="store_true",
                   help="code 3 si une publication est en cours ou prévue dans les 15 min")
    p.add_argument("--report", action="store_true",
                   help="derniers posts, réponses, coûts et erreurs (lecture seule, aucun secret)")
    p.add_argument("--state", metavar="CHEMIN", help="fichier d'état (défaut : state/state.json)")
    p.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING…")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# --busy-check : LECTURE SEULE de l'état (le bot en service l'écrit en parallèle)
# ---------------------------------------------------------------------------
def busy_check(state_path: str | Path | None = None, now=None) -> tuple[int, str]:
    path = Path(state_path) if state_path else utils.STATE_DIR / "state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return EXIT_OK, "Libre : aucun fichier d'état (bot jamais lancé ici)."
    except (OSError, ValueError) as e:
        return EXIT_OK, f"Libre : fichier d'état illisible ({type(e).__name__}), rien de v2 en cours."
    if not isinstance(data, dict) or data.get("version") != 2:
        return EXIT_OK, "Libre côté v2 : l'état est celui de l'ancienne version (v1)."
    now = now or utils.now_local()
    paused = bool(data.get("paused"))
    ahead = now + timedelta(minutes=BUSY_AHEAD_MIN)
    stale = now - timedelta(minutes=BUSY_STALE_MIN)
    busy: list[str] = []
    for it in (data.get("items") or {}).values():
        if not isinstance(it, dict):
            continue
        status = it.get("status")
        kind = KIND_FR.get(str(it.get("kind")), str(it.get("kind")))
        if status == "publishing":
            meta = it.get("meta") if isinstance(it.get("meta"), dict) else {}
            started = utils.parse_iso(meta.get("publishing_at")) or utils.parse_iso(it.get("publish_at"))
            if started is None or started >= stale:
                busy.append(f"{kind} en cours de publication")
        elif status == "approved" and not paused:
            pa = utils.parse_iso(it.get("publish_at"))
            if pa is None or stale <= pa <= ahead:
                when = "maintenant" if pa is None or pa <= now else f"à {pa.astimezone(utils.tz()):%H:%M}"
                busy.append(f"{kind} validé, publication {when}")
    if busy:
        return EXIT_BUSY, "Occupé : " + " ; ".join(busy[:5])
    return EXIT_OK, "Libre : aucune publication en cours ni dans les 15 prochaines minutes."


# ---------------------------------------------------------------------------
# --report : ce que le bot a vraiment écrit et publié (LECTURE SEULE, aucun secret)
# ---------------------------------------------------------------------------
POST_FAMILY = ("gm", "tip", "news", "thread", "reactive")


def report(state_path: str | Path | None = None, log_path: str | Path | None = None, n: int = 15) -> str:
    path = Path(state_path) if state_path else utils.STATE_DIR / "state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return f"État illisible ({type(e).__name__})."
    items = [i for i in (data.get("items") or {}).values() if isinstance(i, dict)]
    items.sort(key=lambda i: str(i.get("created_at") or ""))
    out = [f"=== {n} derniers posts (du plus ancien au plus récent) ==="]
    for it in [i for i in items if i.get("kind") in POST_FAMILY][-n:]:
        meta = it.get("meta") if isinstance(it.get("meta"), dict) else {}
        flags = []
        if meta.get("from_fallback"):
            flags.append("TEXTE DE SECOURS")
        if it.get("image_kind"):
            flags.append(f"image {it['image_kind']}")
        if it.get("poll"):
            flags.append("sondage")
        texts = [str(t) for t in (it.get("texts") or [])]
        body = " // ".join(texts).replace("\n", " / ")
        out.append(f"- {str(it.get('created_at') or '')[:16]} {it.get('kind')} [{it.get('status')}]"
                   + (f" ({', '.join(flags)})" if flags else ""))
        out.append(f"  texte : {body[:600]}")
        if meta.get("reason"):
            out.append(f"  raison : {str(meta['reason'])[:200]}")
        srcs = [str(x.get("title") or "")[:90] for x in (meta.get("sources") or []) if isinstance(x, dict)]
        out.append("  sources : " + (" | ".join(srcs[:3]) if srcs else "aucune"))
        if it.get("last_error"):
            out.append(f"  erreur : {str(it['last_error'])[:200]}")
    def count(kind: str) -> str:
        sts: dict[str, int] = {}
        for it in items:
            if it.get("kind") == kind:
                sts[str(it.get("status"))] = sts.get(str(it.get("status")), 0) + 1
        return ", ".join(f"{k} {v}" for k, v in sorted(sts.items())) or "aucune"
    out.append("=== Réponses aux commentaires : " + count("comment_reply"))
    out.append("=== Suggestions radar : " + count("radar"))
    days = (data.get("costs") or {}).get("days") or {}
    month = utils.now_local().strftime("%Y-%m")
    x_month = sum(float(v.get("total") or 0) for d, v in days.items() if str(d).startswith(month))
    llm_days = (data.get("llm_usage") or {}).get("days") or {}
    llm_month = sum(float(v.get("usd") or 0) for d, v in llm_days.items() if str(d).startswith(month))
    out.append(f"=== Coûts estimés ce mois : API X {x_month:.2f} $, Claude {llm_month:.2f} $")
    lp = Path(log_path) if log_path else utils.LOGS_DIR / "bot.log"
    try:
        errors = [ln.rstrip() for ln in lp.read_text(encoding="utf-8", errors="replace").splitlines()
                  if "| ERROR" in ln or "| CRITICAL" in ln]
    except OSError:
        errors = []
    out.append("=== Dernières erreurs ===")
    out.extend((e[:300] for e in errors[-8:]) if errors else ["aucune"])
    return "\n".join(out)


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------
def _defined(value: str) -> str:
    return "défini" if value else "manquant"


def config_summary(settings) -> list[str]:
    """Résumé lisible de la config. JAMAIS de secret : seulement défini / manquant."""
    s = settings
    jours = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")
    posts = ", ".join(f"{sl.kind} {sl.at:%H:%M}" for sl in s.post_slots) or "aucun"
    threads = ", ".join(f"{jours[sl.weekday]} {sl.at:%H:%M}" for sl in s.thread_slots
                        if sl.weekday is not None) or "aucun"
    leads = ", ".join(f"{k} {v} min" for k, v in sorted(s.draft_leads.items()))
    quiet = f"{s.quiet_hours[0]:%H:%M}-{s.quiet_hours[1]:%H:%M}" if s.quiet_hours else "aucune"
    weekly = (f"{jours[s.weekly_report.weekday]} {s.weekly_report.at:%H:%M}"
              if s.weekly_report and s.weekly_report.weekday is not None else "désactivé")
    try:
        import importlib.util
        has_pw = importlib.util.find_spec("playwright") is not None
        has_openai = importlib.util.find_spec("openai") is not None
    except (ImportError, ValueError):
        has_pw = has_openai = False
    warn = [f"⚠️ {w}" for w in (getattr(s, "warnings", None) or [])]
    return warn + [
        f"Compte : @{s.handle or '?'} (X_USER_ID {_defined(s.x_user_id)})",
        f"Mode : {'validation sur Telegram' if s.approval_mode == 'telegram' else 'AUTOMATIQUE'} · "
        f"DRY_RUN : {'oui (simulation, rien n’est publié)' if s.dry_run else 'non (publication réelle)'}",
        "Clés : " + ", ".join(f"{name} {_defined(val)}" for name, val in (
            ("X_API_KEY", s.x_api_key), ("X_API_SECRET", s.x_api_secret), ("X_ACCESS_TOKEN", s.x_access_token),
            ("X_ACCESS_TOKEN_SECRET", s.x_access_token_secret), ("ANTHROPIC_API_KEY", s.anthropic_api_key),
            ("TELEGRAM_BOT_TOKEN", s.telegram_token), ("TELEGRAM_CHAT_ID", s.telegram_chat_id),
            ("OPENAI_API_KEY", s.openai_api_key))),
        f"Claude : {s.llm_model}, effort {s.llm_effort} (threads : {s.llm_effort_thread})",
        f"Créneaux : {posts} · threads : {threads} · aléa de publication 0-{s.publish_jitter_minutes} min",
        f"Rédaction à l'avance : {leads} · délai de validation {s.approval_grace_minutes} min · "
        f"heures calmes {quiet}",
        f"Réactif : {'oui' if s.reactive_enabled else 'non'} (max {s.reactive_max_per_day}/jour, "
        f"{s.reactive_window[0]:%H:%M}-{s.reactive_window[1]:%H:%M})",
        f"Commentaires : {'oui' if s.comments_enabled else 'non'} (toutes les {s.comments_check_every_min} min) · "
        f"Radar : {'oui' if s.radar_enabled else 'non'} ({len(s.radar_accounts)} comptes, "
        f"max {s.radar_max_per_day}/jour)",
        f"Actu : {len(s.news_feeds)} flux + {len(s.x_news_accounts)} comptes X "
        f"({'lus' if s.x_news_accounts_enabled else 'non lus'})",
        f"Budget API X : {s.x_budget_daily_usd:.2f} $/jour, {s.x_budget_monthly_usd:.0f} $/mois",
        f"Visuels : captures {'oui' if s.screenshots_enabled else 'non'}"
        f"{'' if has_pw or not s.screenshots_enabled else ' (Playwright absent : aucune capture)'} · "
        f"photos IA {'oui' if s.image_ai_enabled else 'non'}"
        f"{'' if has_openai or not s.image_ai_enabled else ' (paquet openai absent)'}",
        f"Récap du matin {s.health_report_at:%H:%M} · bilan hebdo {weekly}",
    ]


def _x_reason(e) -> str:
    kind = getattr(e, "kind", "")
    return {
        "auth": "clés refusées (401) : régénère « Access Token and Secret » sur console.x.com avec les droits "
                "Read and write, puis relance set_secrets.sh",
        "credits": "plus de crédits API : ajoute du crédit sur console.x.com (Billing)",
        "forbidden": "accès refusé (403) : l'app a-t-elle les droits « Read and write » ?",
        "rate_limited": "limite de débit, réessaie dans 15 min",
        "network": "réseau indisponible",
    }.get(kind, f"erreur {kind or type(e).__name__}")


def run_check(settings, *, telegram=None, x=None, llm=None, out: Callable[[str], None] = print) -> int:
    """Vérifie Telegram, l'API X (≈ 0,01 $) et Claude. 0 si tout va bien, 1 sinon.
    N'écrit JAMAIS dans l'état du bot en service (état jetable)."""
    ok = True
    parts = {"telegram": "missing", "x": "missing", "claude": "missing"}
    out("=== Configuration ===")
    for line in config_summary(settings):
        out("  " + line)
    tmp = tempfile.TemporaryDirectory(prefix="xbot-check-")
    try:
        state = budget = None

        def _state():
            nonlocal state, budget
            if state is None:
                from .costs import Budget
                from .state import StateStore
                state = StateStore(Path(tmp.name) / "state.json")
                budget = Budget(state, settings.x_budget_daily_usd, settings.x_budget_monthly_usd)
            return state, budget

        out("=== Vérifications ===")
        # --- Telegram
        if telegram is None and settings.telegram_ok():
            from .telegram import Telegram
            telegram = Telegram(settings.telegram_token, settings.telegram_chat_id)
        if telegram is None:
            if settings.approval_mode == "telegram":
                out("❌ Telegram : TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID manquants")
                ok = False
            else:
                out("⚠️ Telegram : non configuré (tu ne recevras aucun message)")
        elif str(settings.telegram_chat_id).startswith("-"):
            parts["telegram"] = "group"
            out("❌ Telegram : TELEGRAM_CHAT_ID est un GROUPE. Le bot n'accepte que ton chat privé avec lui "
                "(sinon n'importe quel membre pourrait valider une publication)")
            ok = False
        else:
            try:
                mid = telegram.send("✅ Test v2 OK — le bot @{} est bien relié à ce chat.".format(settings.handle))
            except Exception as e:  # noqa: BLE001
                mid = None
                log.warning("Telegram : %s", type(e).__name__)
            if mid:
                parts["telegram"] = "ok"
                out("✅ Telegram : message de test envoyé")
            else:
                parts["telegram"] = "bad"
                out("❌ Telegram : envoi impossible (jeton faux, mauvais TELEGRAM_CHAT_ID, ou tu n'as jamais "
                    "écrit /start au bot ?)")
                ok = False
        # --- API X
        if not settings.x_credentials_ok() and x is None:
            if settings.dry_run:
                out("⚠️ API X : clés absentes — acceptable seulement en simulation (DRY_RUN=true)")
            else:
                out("❌ API X : clés absentes (lance deploy/set_secrets.sh)")
                ok = False
        else:
            if x is None:
                from .x_api import XClient
                x = XClient(settings, _state()[1])
            try:
                me = x.me() or {}
            except Exception as e:  # noqa: BLE001 — XError ou réseau
                parts["x"] = {"auth": "auth", "credits": "credits", "forbidden": "forbidden"}.get(
                    getattr(e, "kind", ""), "error")
                out(f"❌ API X : {_x_reason(e)}")
                ok = False
            else:
                username = str(me.get("username") or "")
                followers = (me.get("public_metrics") or {}).get("followers_count")
                if not username:
                    out("❌ API X : réponse sans nom de compte")
                    ok = False
                elif username.lower() != settings.handle.lower():
                    parts["x"] = "wrong_account"
                    out(f"❌ API X : les clés sont celles de @{username}, pas de @{settings.handle} — "
                        "régénère l'Access Token en étant connecté au bon compte")
                    ok = False
                elif settings.x_user_id and str(me.get("id") or "") != str(settings.x_user_id):
                    out(f"❌ API X : X_USER_ID ({settings.x_user_id}) n'est pas l'id de @{username} "
                        f"({me.get('id')}) — corrige ou supprime X_USER_ID dans config/.env")
                    ok = False
                else:
                    parts["x"] = "ok"
                    extra = f", {followers} abonnés" if followers is not None else ""
                    out(f"✅ API X : connecté en tant que @{username}{extra}")
        # --- Claude
        if llm is None:
            if not settings.anthropic_api_key:
                out("❌ Claude : ANTHROPIC_API_KEY manquante")
                ok = False
            else:
                from .llm import LLM
                llm = LLM(settings, _state()[0])
        if llm is not None:
            from .llm import BOOL, schema
            try:
                res = llm.json("check", "You are a health check endpoint. Answer with the JSON object only.",
                               'Answer {"ok": true}.', schema({"ok": BOOL}), effort="low", max_tokens=2000)
            except Exception as e:  # noqa: BLE001
                log.warning("Claude : %s", type(e).__name__)
                res = None
            if isinstance(res, dict) and res.get("ok") is True:
                parts["claude"] = "ok"
                out(f"✅ Claude : répond ({settings.llm_model})")
            else:
                parts["claude"] = "bad"
                out(f"❌ Claude : pas de réponse valide (clé ANTHROPIC_API_KEY, crédits, ou modèle "
                    f"{settings.llm_model} indisponible ? voir logs/bot.log)")
                ok = False
    finally:
        tmp.cleanup()
    out("=== Résultat : " + ("tout est OK ✅" if ok else "au moins un problème ❌") + " ===")
    # Ligne lue par l'assistant d'installation (deploy/push_from_mac.sh).
    out("PARTS " + " ".join(f"{k}={v}" for k, v in parts.items()))
    return EXIT_OK if ok else EXIT_FAIL


# ---------------------------------------------------------------------------
# Le service ne peut pas démarrer : le dire dans le journal ET sur Telegram
# ---------------------------------------------------------------------------
CONFIG_ALERT_EVERY_S = 6 * 3600


def alert_config_problem(errors: list[str]) -> None:
    """Sinon le bot redémarre en boucle sans que personne ne le sache (le message ne
    partirait que sur la sortie standard). Une alerte Telegram toutes les 6 h au plus."""
    import os
    import time
    for err in errors:
        log.error("Démarrage impossible : %s", err)
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        return
    marker = utils.STATE_DIR / "config_alert.ts"
    try:
        if marker.exists() and time.time() - marker.stat().st_mtime < CONFIG_ALERT_EVERY_S:
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(int(time.time())))
    except OSError:
        pass
    try:
        from .telegram import Telegram, esc
        Telegram(token, chat).send("❌ <b>Le bot ne peut pas démarrer</b> (config/.env) :\n• "
                                   + "\n• ".join(esc(e) for e in errors[:6])
                                   + "\nCorrige le réglage puis relance le bot. Rien n'est publié en attendant.")
    except Exception as e:  # noqa: BLE001
        log.warning("Alerte Telegram impossible (%s)", type(e).__name__)


# ---------------------------------------------------------------------------
# Verrou : une seule instance à la fois sur un même état
# ---------------------------------------------------------------------------
def acquire_lock(state_path: str | Path | None):
    """Verrou exclusif à côté du fichier d'état. None si une autre instance le tient."""
    lock_path = (Path(state_path).parent if state_path else utils.STATE_DIR) / "bot.lock"
    try:
        import fcntl
    except ImportError:  # pas de verrou possible (Windows) : on fait confiance
        return True
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")  # noqa: SIM115 — gardé ouvert toute la vie du process
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


_LOCK_HANDLE = None


def _take_lock(state_path) -> bool:
    global _LOCK_HANDLE
    handle = acquire_lock(state_path)
    if handle is None:
        return False
    _LOCK_HANDLE = handle
    return True


def _make_state(state_path):
    from .state import StateStore
    return StateStore(Path(state_path)) if state_path else StateStore(utils.STATE_DIR / "state.json")


def run_once(settings, job: str, state_path=None, out: Callable[[str], None] = print) -> int:
    if not _take_lock(state_path):
        out("❌ Le bot tourne déjà (service systemd ?) : arrête-le d'abord avec "
            "« sudo systemctl stop x-degen-bot », ou attends.")
        return EXIT_FAIL
    from .scheduler import Bot
    bot = Bot(settings, state=_make_state(state_path))
    if job == "plan":
        n = bot.plan_slots()
        out(f"plan : {n} créneau(x) rédigé(s)")
    elif job == "publish":
        n = bot.publisher.publish_due()
        out(f"publish : {n} publication(s)")
    elif job == "comments":
        n = bot.comments.check()
        out(f"comments : {n} brouillon(s) de réponse")
    elif job == "radar":
        n = bot.radar.check()
        out(f"radar : {n} suggestion(s)")
    elif job == "reactive":
        n = bot.maybe_reactive(force=True)
        out(f"reactive : {n} post(s) réactif(s) proposé(s)")
    elif job == "metrics":
        n = bot.analytics.refresh_metrics()
        out(f"metrics : {n} post(s) mis à jour")
    elif job == "daily":
        bot.approvals.notify(bot.analytics.daily_report())
        out("daily : récap envoyé sur Telegram")
    elif job == "weekly":
        bot.approvals.notify(bot.analytics.weekly_report())
        out("weekly : bilan envoyé sur Telegram")
    elif job == "poll":
        n = bot.approvals.poll()
        p = bot.publisher.publish_due()
        out(f"poll : {n} message(s) Telegram traité(s), {p} publication(s)")
    return EXIT_OK


def run_bot(settings, state_path=None) -> int:
    if not _take_lock(state_path):
        log.error("Une autre instance du bot tourne déjà sur cet état : arrêt (aucun doublon de publication).")
        return EXIT_FAIL
    from .scheduler import Bot
    Bot(settings, state=_make_state(state_path)).run_forever()
    return EXIT_OK


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.report:
        print(report(args.state))
        return EXIT_OK
    if args.busy_check:
        code, msg = busy_check(args.state)
        print(msg)
        return code
    load_env()
    utils.setup_logging(args.log_level)
    from .config import load_settings
    try:
        settings = load_settings()
    except ValueError as e:  # ConfigError en hérite
        print(f"❌ Configuration invalide : {e}")
        if not (args.check or args.once):
            alert_config_problem([str(e)])
        return EXIT_CONFIG
    errors = settings.validate()
    if errors:
        print("❌ Configuration incomplète (config/.env) :")
        for err in errors:
            print(f"  - {err}")
        if args.check:  # on vérifie quand même tout le reste : l'assistant sait quoi demander
            run_check(settings)
            return EXIT_CONFIG
        if not args.once:
            alert_config_problem(errors)
        return EXIT_CONFIG
    if args.check:
        return run_check(settings)
    if args.once:
        return run_once(settings, args.once, args.state)
    return run_bot(settings, args.state)


if __name__ == "__main__":
    sys.exit(main())
