"""L'agent d'analyse : chiffres, tendances et algorithme → décisions appliquées toutes seules
(consigne du propriétaire, 24/11/2026 : « pas besoin de passer ici, analyse avec les agents et
prends les bonnes décisions, tout est question de trend, de chiffre et d'algorithme »).

Tous les trois jours, sans personne :
  1. le code réunit une feuille de chiffres (Analytics.figures_sheet : portée par sujet, type,
     format, accroche, heure, longueur, fin de post… avec le nombre de posts de chaque groupe), ce
     que X a publié sur son algorithme (bot/algo.py) et les tendances du jour (bot/trends.py) ;
  2. un agent analyste (Claude, réflexion longue : Writer.strategy_review) en tire au plus cinq
     consignes pour la rédaction en chef, cinq pour le rédacteur et une idée d'essai, chacune avec
     les chiffres qui la fondent ;
  3. le code écarte ce qui touche aux règles du compte (sujets interdits, liens, hashtags, sujets
     pour initiés) ou repose sur trop peu de posts, puis un agent relecteur (Writer.strategy_check :
     statisticien strict et gardien des règles) revoit chaque consigne ; ce qu'il refuse est écarté ;
  4. ce qui reste est noté dans l'état (`strategy`), lu avant chaque post par la rédaction en chef
     (`editor_block`) et le rédacteur (`writer_block`), et résumé au propriétaire sur Telegram.

Les consignes affinent le choix des histoires et l'écriture ; elles ne passent jamais devant les
règles du compte, ni devant les contrôles de chaque post (faits, mots simples, sujets interdits).
Un post prévu sur quatre reste un essai, hors consignes : sinon le compte n'apprendrait plus rien.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from .filters import blocked_topic, off_limits_term
from .telegram import esc
from .utils import iso, now_local, parse_iso
from .x_api import contains_url

log = logging.getLogger("xbot.strategy")

EVERY_DAYS = 3               # une analyse tous les trois jours…
REACT_MIN_GAP_H = 20         # … ou dès le lendemain quand X a changé son algorithme
MAX_AGE_DAYS = 10            # des consignes plus vieilles ne sont plus suivies
MIN_POSTS = 20               # posts mesurés avant la première analyse
MAX_DIRECTIVES = 5
TEXT_MAX = 220
MIN_N = 5                    # posts de chaque côté pour une consigne fondée sur nos chiffres
HISTORY_KEEP = 6
RUN_HOURS = (7, 23)          # jamais la nuit : l'analyse passe après les créneaux, pas pendant
BASES = ("numbers", "algorithm", "trends")
BASIS_FR = {"numbers": "nos chiffres", "algorithm": "l'algorithme de X", "trends": "les tendances"}
# Ce qu'une consigne ne peut pas toucher : les liens, hashtags et mentions, les réponses sous les
# autres comptes, le nombre de posts ; et jamais les sujets pour initiés.
_RULE_RE = re.compile(
    r"(?i)\bhashtags?\b|\burls?\b|\b(?:a|the|with|add|adding|include|including|external|outbound) links?\b|@\w|#\w|"
    r"\b(?:mention|tag|tagging|mentioning) (?:other |big |the )?(?:accounts?|people|users|handles?|brands?|them)\b|"
    r"\brepl(?:y|ies|ying) (?:to|under|on|in) (?:other|big|bigger|large|larger|popular|viral)\b|"
    r"\bunder (?:other|big|bigger|large|larger|popular) accounts?\b|\bother (?:accounts'?|people'?s?) posts\b|"
    r"\bquote[- ]?(?:tweets?|posts?)\b|\b(?:more|fewer|less) posts? (?:a|per|each) day\b|\bposts? per day\b|"
    r"\bdevelopers?\b|\bprogrammers?\b|\bAPIs?\b|\bSDKs?\b|\bbenchmarks?\b|\bopen[- ]source\b")


# Une consigne vit plusieurs jours : rien qui tienne à l'actu du jour (la rédaction en chef voit déjà
# les tendances avant chaque choix).
_TODAY_RE = re.compile(r"(?i)\b(?:today|tonight|tomorrow|yesterday|this (?:week|weekend|morning|evening)|right now|"
                       r"currently trending|trending (?:now|today))\b")


def directive_problem(d: dict) -> str | None:
    """Pourquoi une consigne proposée est écartée par le code (en français), ou None."""
    if not isinstance(d, dict):
        return "illisible"
    text = " ".join(str(d.get("text") or "").split())
    if len(text) < 12:
        return "vide"
    if len(text) > TEXT_MAX:
        return "trop longue"
    if contains_url(text):
        return "contient un lien"
    if _RULE_RE.search(text):
        return "touche à une règle du compte"
    if _TODAY_RE.search(text):
        return "liée à l'actu du jour"
    bad = off_limits_term(text) or blocked_topic(text)
    if bad:
        return f"sujet interdit ({bad})"
    basis = str(d.get("basis") or "")
    if basis not in BASES:
        return "sans fondement déclaré"
    n = d.get("n")
    if basis == "numbers" and not (isinstance(n, int) and not isinstance(n, bool) and n >= MIN_N):
        return f"repose sur moins de {MIN_N} posts"
    if not str(d.get("why") or "").strip():
        return "sans justification"
    return None


def _clean(d: dict) -> dict:
    return {"text": " ".join(str(d.get("text") or "").split()),
            "fr": " ".join(str(d.get("fr") or "").split())[:300], "basis": str(d.get("basis") or ""),
            "n": int(d.get("n") or 0) if isinstance(d.get("n"), int) and not isinstance(d.get("n"), bool) else 0,
            "why": " ".join(str(d.get("why") or "").split())[:300]}


class Strategist:
    def __init__(self, settings, state, analytics, writer, trends=None, algo=None, limits=None) -> None:
        self.settings = settings
        self.state = state
        self.analytics = analytics
        self.writer = writer
        self.trends = trends
        self.algo = algo
        self.limits = limits          # bot/limits.py : le nombre de posts par jour, et ce que le budget permet

    # ---- État ---------------------------------------------------------
    def memo(self) -> dict | None:
        """Les consignes en vigueur, ou None (jamais analysé, ou analyse trop ancienne)."""
        m = self.state.data.get("strategy")
        if not isinstance(m, dict):
            return None
        at = parse_iso(m.get("at"))
        if at is None or now_local() - at > timedelta(days=MAX_AGE_DAYS):
            return None
        return m

    def due(self) -> bool:
        now = now_local()
        if not (RUN_HOURS[0] <= now.hour < RUN_HOURS[1]):
            return False
        tried = parse_iso((self.state.data.get("strategy_run") or {}).get("tried_at"))
        if tried is None or now - tried >= timedelta(days=EVERY_DAYS):
            return True
        # X a modifié son classement depuis la dernière analyse, ou l'agent spécialiste n'a encore
        # jamais lu le code : on n'attend pas trois jours.
        try:
            if self.algo is None or now - tried < timedelta(hours=REACT_MIN_GAP_H):
                return False
            if self.algo.changed_since(tried):
                return True
            brief = getattr(self.algo, "brief", None)
            return callable(brief) and brief() is None and bool(self.algo.brief_due())
        except Exception as e:  # noqa: BLE001
            log.warning("Analyse : veille de l'algorithme illisible (%s)", e)
            return False

    def watch(self) -> None:
        """Chaque jour, gratuitement : relire le journal et les poids de l'algorithme de X. C'est
        ce qui permet à `due` de réagir dès qu'ils changent. Ne lève jamais d'exception."""
        if self.algo is not None:
            try:
                self.algo.refresh()
            except Exception as e:  # noqa: BLE001
                log.warning("Analyse : veille de l'algorithme impossible (%s)", e)

    # ---- L'analyse ----------------------------------------------------
    def review(self, force: bool = False) -> dict | None:
        """Lance l'analyse si c'est l'heure (ou `force`). Rend les nouvelles consignes, ou None
        (pas l'heure, pas assez de chiffres, Claude muet : les consignes précédentes restent).
        Ne lève jamais d'exception."""
        try:
            if not (force or self.due()):
                return None
            self.state.data["strategy_run"] = {"tried_at": iso(now_local())}
            self.state.flush()
            return self._review()
        except Exception as e:  # noqa: BLE001
            log.exception("Analyse : erreur inattendue : %s", e)
            return None

    def _review(self) -> dict | None:
        figures, n_posts = self.analytics.figures_sheet()
        if not figures or n_posts < MIN_POSTS:
            log.info("Analyse : pas assez de posts mesurés (%d sur %d), consignes inchangées", n_posts, MIN_POSTS)
            return None
        algo = ""
        new_brief = False
        if self.algo is not None:
            self.algo.refresh()
            maker = getattr(self.algo, "make_brief", None)
            if callable(maker) and self.algo.brief_due():
                new_brief = bool(maker(self.writer))        # l'agent spécialiste relit le code de X
            algo = self.algo.block() or ""
        trends = ""
        if self.trends is not None:
            try:
                trends = self.trends.block() or ""
            except Exception as e:  # noqa: BLE001
                log.warning("Analyse : tendances indisponibles (%s)", e)
        limits_sheet = ""
        if self.limits is not None:
            try:
                limits_sheet = self.limits.sheet() or ""
            except Exception as e:  # noqa: BLE001
                log.warning("Analyse : limites de posts illisibles (%s)", e)
        proposal = self.writer.strategy_review(figures, algo, trends, self._previous_block(), limits_sheet)
        if not isinstance(proposal, dict):
            log.warning("Analyse : pas de réponse de l'analyste, consignes inchangées")
            return None
        kept: dict[str, list[dict]] = {"editor": [], "writer": []}
        dropped = 0
        for side in ("editor", "writer"):
            for d in (proposal.get(side) or [])[:MAX_DIRECTIVES]:
                why = directive_problem(d)
                if why:
                    dropped += 1
                    log.info("Analyse : consigne écartée par le code (%s) — %s", why,
                             str((d or {}).get("text") if isinstance(d, dict) else d)[:120])
                    continue
                kept[side].append(_clean(d))
        flat = [(side, d) for side in ("editor", "writer") for d in kept[side]]
        # Le nombre de posts par jour : le code vérifie les bornes, le cran et le budget ; le
        # relecteur dit ensuite si les chiffres justifient le changement.
        change, limits_note = self._limits_change(proposal.get("limits")) if limits_sheet else (None, "")
        shown = [f"({'for choosing stories' if side == 'editor' else 'for writing'}; basis: {d['basis']}"
                 f"{', ' + str(d['n']) + ' posts' if d['basis'] == 'numbers' else ''}) {d['text']} — {d['why']}"
                 for side, d in flat]
        if change:
            before = self.limits.target()
            shown.append(f"(posting limits) Scheduled posts per day: {before[0]} → {change['scheduled']}; breaking-news "
                         f"posts per day at most: {before[1]} → {change['reactive']} — {change['why']}")
        if shown:
            verdicts = self.writer.strategy_check(
                "\n\n".join(p for p in (figures, limits_sheet) if p), algo, trends, shown)
            if verdicts is None:
                log.warning("Analyse : pas de réponse du relecteur, consignes inchangées")
                return None
            kept = {"editor": [], "writer": []}
            for (side, d), ok in zip(flat, verdicts):
                if ok:
                    kept[side].append(d)
                else:
                    dropped += 1
            if change and not verdicts[-1]:
                change, limits_note = None, "le relecteur a refusé de changer le nombre de posts"
                dropped += 1
        adopted = None
        if change:
            adopted = self.limits.adopt(change["scheduled"], change["reactive"], change["why"], change["fr"])
            if adopted is None:
                limits_note = "le nombre de posts n'a pas pu être changé"
        test = " ".join(str(proposal.get("test") or "").split())
        if test and (len(test) > TEXT_MAX or directive_problem({"text": test, "basis": "trends", "n": 0, "why": "essai"})):
            test = ""
        findings = []
        for f in (proposal.get("findings") or [])[:MAX_DIRECTIVES]:
            if isinstance(f, dict) and str(f.get("what") or "").strip():
                findings.append({"what": " ".join(str(f["what"]).split())[:300],
                                 "n": f.get("n") if isinstance(f.get("n"), int) else 0,
                                 "gap": " ".join(str(f.get("gap") or "").split())[:60]})
        memo = {"at": iso(now_local()), "posts": n_posts, "editor": kept["editor"], "writer": kept["writer"],
                "test": test, "findings": findings, "dropped": dropped, "limits": adopted,
                "limits_note": limits_note, "algo_brief_new": new_brief,
                "summary_fr": " ".join(str(proposal.get("summary_fr") or "").split())[:700]}
        history = [h for h in self.state.data.get("strategy_history") or [] if isinstance(h, dict)]
        old = self.state.data.get("strategy")
        if isinstance(old, dict):
            history.append({"at": old.get("at"), "editor": [d.get("text") for d in old.get("editor") or []],
                            "writer": [d.get("text") for d in old.get("writer") or []]})
        self.state.data["strategy_history"] = history[-HISTORY_KEEP:]
        self.state.data["strategy"] = memo
        self.state.flush()
        log.info("Analyse faite sur %d posts : %d consigne(s) pour le choix, %d pour l'écriture, %d écartée(s)",
                 n_posts, len(memo["editor"]), len(memo["writer"]), dropped)
        return memo

    def _limits_change(self, raw) -> tuple[dict | None, str]:
        """Le changement du nombre de posts demandé par l'analyste : (changement valide ou None,
        note en français quand le code le refuse). Rien de demandé, ou les mêmes valeurs : (None, "")."""
        if not isinstance(raw, dict):
            return None, ""
        scheduled, reactive = raw.get("scheduled_per_day"), raw.get("reactive_per_day")
        try:
            if not self.limits.changed(scheduled, reactive):
                return None, ""
            bad = self.limits.problem(scheduled, reactive)
        except Exception as e:  # noqa: BLE001
            log.warning("Analyse : limites de posts illisibles (%s)", e)
            return None, ""
        why = " ".join(str(raw.get("why") or "").split())
        if not bad and not why:
            bad = "sans justification"
        if bad:
            log.info("Analyse : changement du nombre de posts refusé par le code (%s)", bad)
            return None, f"changement du nombre de posts refusé : {bad}"
        return {"scheduled": scheduled, "reactive": reactive, "why": why[:300],
                "fr": " ".join(str(raw.get("fr") or "").split())[:300]}, ""

    def _previous_block(self) -> str:
        m = self.state.data.get("strategy")
        if not isinstance(m, dict) or not (m.get("editor") or m.get("writer")):
            return ""
        at = parse_iso(m.get("at"))
        lines = [f"YOUR EARLIER DIRECTIVES (decided on {at:%B %d}; the figures above now include the posts written "
                 "since): keep the ones the figures still support, drop or change the others."
                 if at is not None else "YOUR EARLIER DIRECTIVES:"]
        lines += [f"- (choosing stories) {d.get('text')}" for d in m.get("editor") or []]
        lines += [f"- (writing) {d.get('text')}" for d in m.get("writer") or []]
        return "\n".join(lines)

    # ---- Ce que lisent la rédaction en chef et le rédacteur -------------
    def _block(self, side: str, title: str) -> str:
        m = self.memo()
        rows = (m or {}).get(side) or []
        if not rows:
            return ""
        at = parse_iso(m.get("at"))
        head = (f"{title} (decided on {at:%B %d} from our own numbers, X's ranking code and the trends). They refine "
                "your choices; they never override the rules above:")
        return "\n".join([head] + [f"- {d['text']}" for d in rows if d.get("text")])

    def editor_block(self) -> str:
        """Pour la rédaction en chef : les consignes de choix d'histoire en vigueur. "" sinon."""
        try:
            return self._block("editor", "STRATEGY FOR CHOOSING STORIES")
        except Exception as e:  # noqa: BLE001
            log.warning("Analyse : consignes de choix illisibles (%s)", e)
            return ""

    def writer_block(self) -> str:
        """Pour le rédacteur : les consignes d'écriture en vigueur. "" sinon."""
        try:
            return self._block("writer", "STRATEGY FOR WRITING")
        except Exception as e:  # noqa: BLE001
            log.warning("Analyse : consignes d'écriture illisibles (%s)", e)
            return ""

    def test_block(self) -> str:
        """Pour un post d'essai : ce que l'analyste propose d'essayer. "" sinon."""
        m = self.memo()
        test = str((m or {}).get("test") or "").strip()
        return f"WHAT TO TRY IN THIS TEST POST (the analyst's idea, still within every rule above): {test}" if test else ""

    # ---- Ce que lit le propriétaire -----------------------------------
    def report_fr(self, memo: dict | None = None) -> str:
        """Le message Telegram d'une analyse : ce que les chiffres montrent, ce qui a été décidé."""
        m = memo if isinstance(memo, dict) else self.memo()
        if not m:
            return ""
        lines = [f"🧠 <b>Analyse automatique</b> ({m.get('posts', 0)} posts mesurés, tendances et algorithme de X)"]
        if m.get("summary_fr"):
            lines.append(esc(m["summary_fr"]))
        findings = (m.get("findings") or [])[:3]
        if findings:
            lines.append("📊 Ce que montrent les chiffres (pas encore des décisions) :")
        for f in findings:
            gap = f" ({esc(f['gap'])})" if f.get("gap") else ""
            lines.append(f"• {esc(f['what'])}{gap}")
        n = len(m.get("editor") or []) + len(m.get("writer") or [])
        if n:
            lines.append(f"✅ {n} décision(s) appliquée(s) dès le prochain post"
                         + (f" ({m['dropped']} autre(s) refusée(s) à la relecture, pas assez solides)"
                            if m.get("dropped") else "") + " :")
            lines += [f"→ {esc(d.get('fr') or d['text'])} <i>({BASIS_FR.get(d.get('basis'), d.get('basis'))})</i>"
                      for d in (m.get("editor") or []) + (m.get("writer") or [])]
        else:
            lines.append("Rien d'assez net dans les chiffres pour changer quoi que ce soit : on continue comme avant.")
        lim = m.get("limits")
        if isinstance(lim, dict):
            before = lim.get("before") or {}
            lines.append(f"📈 Nombre de posts, à partir de demain : {lim.get('scheduled')} prévus par jour (avant : "
                         f"{before.get('scheduled')}), {lim.get('reactive')} « à chaud » au plus (avant : "
                         f"{before.get('reactive')}). {esc(lim.get('fr') or '')}".rstrip())
        elif m.get("limits_note"):
            note = str(m["limits_note"])
            note = note[:1].upper() + note[1:]
            lines.append(("💰 " if "budget" in note else "📈 ") + esc(note) + ". Le nombre de posts ne change pas.")
        if self.algo is not None:
            changes = self.algo.changes_fr()
            if changes:
                lines.append("⚙️ " + esc(changes))
            brief_fr = getattr(self.algo, "brief_fr", None)
            if m.get("algo_brief_new") and callable(brief_fr) and brief_fr():
                lines.append("🔬 L'algorithme de X, relu par l'agent spécialiste : " + esc(brief_fr()))
        lines.append(f"Prochaine analyse dans {EVERY_DAYS} jours. Rien à faire de ton côté.")
        return "\n".join(lines)
