"""Réglages du bot, lus depuis l'environnement (config/.env) et config/*.json.

Un seul objet `Settings` construit au démarrage ; aucune autre partie du code
ne lit os.environ directement (sauf utils.tz pour le fuseau).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time as dtime

from .utils import CONFIG_DIR, load_json, parse_hhmm, parse_window

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
            "lun": 0, "mar": 1, "mer": 2, "jeu": 3, "ven": 4, "sam": 5, "dim": 6}

POST_KINDS = ("gm", "tip", "news")
DEFAULT_LEADS = {"gm": 660, "tip": 150, "news": 120, "thread": 240}

# Modèles qui acceptent la bascule serveur en cas de refus (fallbacks: "default").
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}


class ConfigError(ValueError):
    pass


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "oui")


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)) or default)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Slot:
    """Créneau de publication récurrent. `weekday` None = tous les jours."""
    kind: str            # gm | tip | news | thread
    at: dtime
    weekday: int | None = None

    @property
    def key(self) -> str:
        day = "" if self.weekday is None else f"{list(WEEKDAYS)[self.weekday]}-"
        return f"{self.kind}-{day}{self.at.strftime('%H%M')}"


def parse_post_slots(raw: str) -> list[Slot]:
    """'gm@08:00,tip@13:30,news@18:00'"""
    slots: list[Slot] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        kind, _, hhmm = part.partition("@")
        kind = kind.strip().lower()
        if kind not in POST_KINDS:
            raise ConfigError(f"POST_SLOTS : type inconnu {kind!r} (attendu : {', '.join(POST_KINDS)})")
        slots.append(Slot(kind, parse_hhmm(hhmm)))
    return slots


def parse_thread_slots(raw: str) -> list[Slot]:
    """'tue@15:00,sun@18:00'"""
    slots: list[Slot] = []
    for part in (raw or "").split(","):
        part = part.strip().lower()
        if not part:
            continue
        day, _, hhmm = part.partition("@")
        if day not in WEEKDAYS:
            raise ConfigError(f"THREAD_SLOTS : jour inconnu {day!r}")
        slots.append(Slot("thread", parse_hhmm(hhmm), WEEKDAYS[day]))
    return slots


def parse_leads(raw: str) -> dict[str, int]:
    leads = dict(DEFAULT_LEADS)
    for part in (raw or "").split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            try:
                leads[k.strip().lower()] = int(v)
            except ValueError:
                raise ConfigError(f"DRAFT_LEAD_MINUTES : valeur invalide {part!r}")
    return leads


@dataclass
class Settings:
    # --- Secrets / identité ---
    x_api_key: str = ""
    x_api_secret: str = ""
    x_access_token: str = ""
    x_access_token_secret: str = ""
    x_user_id: str = ""
    x_handle: str = ""
    anthropic_api_key: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    openai_api_key: str = ""

    # --- Comportement général ---
    dry_run: bool = True
    approval_mode: str = "telegram"      # telegram | auto
    automated_label_ack: bool = False
    publish_jitter_minutes: int = 4

    # --- Claude ---
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "medium"
    llm_effort_thread: str = "high"

    # --- Planning ---
    post_slots: list[Slot] = field(default_factory=list)
    thread_slots: list[Slot] = field(default_factory=list)
    draft_leads: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_LEADS))
    approval_grace_minutes: int = 120
    quiet_hours: tuple[dtime, dtime] | None = None
    health_report_at: dtime = dtime(8, 30)
    weekly_report: Slot | None = None
    thread_candidates: int = 3

    # --- Posts réactifs ---
    reactive_enabled: bool = True
    reactive_max_per_day: int = 2
    reactive_check_every_min: int = 150
    reactive_window: tuple[dtime, dtime] = (dtime(9, 30), dtime(22, 0))
    reactive_min_gap_min: int = 90

    # --- Commentaires sous mes posts ---
    comments_enabled: bool = True
    comments_check_every_min: int = 10
    comments_max_drafts_per_day: int = 40
    comments_window: tuple[dtime, dtime] = (dtime(8, 0), dtime(23, 30))

    # --- Radar (suggestions de réponses sous les gros comptes, publiées À LA MAIN) ---
    radar_enabled: bool = True
    radar_check_every_min: int = 30
    radar_accounts_per_check: int = 6
    radar_max_age_min: int = 45
    radar_max_per_day: int = 10
    radar_window: tuple[dtime, dtime] = (dtime(8, 0), dtime(22, 30))
    radar_min_likes_per_min: float = 1.0

    # --- Sources d'actu ---
    x_news_accounts_enabled: bool = True

    # --- Budget API X (dollars) ---
    x_budget_daily_usd: float = 1.50
    x_budget_monthly_usd: float = 30.0

    # --- Visuels ---
    screenshots_enabled: bool = True
    image_ai_enabled: bool = False
    image_model: str = "gpt-image-1"
    image_size: str = "1024x1024"
    image_quality: str = "medium"

    # --- Fichiers JSON ---
    radar_accounts: list[str] = field(default_factory=list)
    x_news_accounts: list[str] = field(default_factory=list)
    news_feeds: list[dict] = field(default_factory=list)
    blacklist_users: list[str] = field(default_factory=list)
    blacklist_keywords: list[str] = field(default_factory=list)
    fallback_captions: dict[str, list[str]] = field(default_factory=dict)
    evergreen_topics: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    @property
    def handle(self) -> str:
        return self.x_handle.lstrip("@")

    @property
    def use_server_fallback(self) -> bool:
        return self.llm_model in FALLBACK_MODELS

    def x_credentials_ok(self) -> bool:
        return all((self.x_api_key, self.x_api_secret, self.x_access_token, self.x_access_token_secret))

    def telegram_ok(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    def lead_for(self, kind: str) -> int:
        return int(self.draft_leads.get(kind, 120))

    def validate(self) -> list[str]:
        """Erreurs bloquantes (liste vide = OK). Appelé au démarrage."""
        errors: list[str] = []
        if self.approval_mode not in ("telegram", "auto"):
            errors.append("APPROVAL_MODE doit valoir 'telegram' ou 'auto'.")
        if self.approval_mode == "telegram" and not self.telegram_ok():
            errors.append("APPROVAL_MODE=telegram exige TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID.")
        if self.approval_mode == "auto" and not self.automated_label_ack:
            errors.append(
                "APPROVAL_MODE=auto publie sans validation humaine : X exige alors l'étiquette "
                "« Automated » sur le profil, la mention « bot » dans la bio et un accord préalable "
                "pour le contenu IA, et ces posts ne sont pas rémunérés (Original Content Rewards). "
                "Si c'est voulu, ajoute AUTOMATED_LABEL_ACK=yes après avoir activé l'étiquette."
            )
        if not self.dry_run and not self.x_credentials_ok():
            errors.append("Clés API X manquantes (X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, "
                          "X_ACCESS_TOKEN_SECRET) alors que DRY_RUN=false.")
        if not self.anthropic_api_key:
            errors.append("ANTHROPIC_API_KEY manquante.")
        if not self.handle:
            errors.append("X_HANDLE manquant.")
        if self.llm_effort not in ("low", "medium", "high", "xhigh", "max"):
            errors.append("LLM_EFFORT invalide.")
        return errors


def _json_config() -> tuple[dict, dict]:
    settings = load_json(CONFIG_DIR / "settings.json", default={})
    content = load_json(CONFIG_DIR / "content.json", default={})
    return settings, content


def _handles(raw) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for h in raw or []:
        if isinstance(h, dict):
            h = h.get("handle", "")
        h = str(h or "").strip().lstrip("@")
        if h and h.lower() not in seen:
            seen.add(h.lower())
            out.append(h)
    return out


def _quiet_hours(raw: str) -> tuple[dtime, dtime] | None:
    if not raw or raw.lower() in ("off", "none", "non", "0"):
        return None
    return parse_window(raw)


def load_settings() -> Settings:
    js, content = _json_config()
    s = Settings(
        x_api_key=_env("X_API_KEY"),
        x_api_secret=_env("X_API_SECRET"),
        x_access_token=_env("X_ACCESS_TOKEN"),
        x_access_token_secret=_env("X_ACCESS_TOKEN_SECRET"),
        x_user_id=_env("X_USER_ID"),
        x_handle=_env("X_HANDLE"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        telegram_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
        openai_api_key=_env("OPENAI_API_KEY"),
        dry_run=_env_bool("DRY_RUN", True),
        approval_mode=_env("APPROVAL_MODE", "telegram").lower() or "telegram",
        automated_label_ack=_env_bool("AUTOMATED_LABEL_ACK", False),
        publish_jitter_minutes=_env_int("PUBLISH_JITTER_MINUTES", 4),
        llm_model=_env("LLM_MODEL") or "claude-opus-5-5",
        llm_effort=_env("LLM_EFFORT", "medium").lower() or "medium",
        llm_effort_thread=_env("LLM_EFFORT_THREAD", "high").lower() or "high",
        post_slots=parse_post_slots(_env("POST_SLOTS", "gm@08:00,tip@13:30,news@18:00")),
        thread_slots=parse_thread_slots(_env("THREAD_SLOTS", "tue@15:00,sun@18:00")),
        draft_leads=parse_leads(_env("DRAFT_LEAD_MINUTES")),
        approval_grace_minutes=_env_int("APPROVAL_GRACE_MINUTES", 120),
        quiet_hours=_quiet_hours(_env("QUIET_HOURS", "23:30-07:30")),
        health_report_at=parse_hhmm(_env("HEALTH_REPORT_AT", "08:30")),
        thread_candidates=max(1, _env_int("THREAD_CANDIDATES", 3)),
        reactive_enabled=_env_bool("REACTIVE_ENABLED", True),
        reactive_max_per_day=_env_int("REACTIVE_MAX_PER_DAY", 2),
        reactive_check_every_min=_env_int("REACTIVE_CHECK_EVERY_MINUTES", 150),
        reactive_window=parse_window(_env("REACTIVE_WINDOW", "09:30-22:00")),
        reactive_min_gap_min=_env_int("REACTIVE_MIN_GAP_MINUTES", 90),
        comments_enabled=_env_bool("COMMENTS_ENABLED", True),
        comments_check_every_min=_env_int("COMMENTS_CHECK_EVERY_MINUTES", 10),
        comments_max_drafts_per_day=_env_int("COMMENTS_MAX_DRAFTS_PER_DAY", 40),
        comments_window=parse_window(_env("COMMENTS_WINDOW", "08:00-23:30")),
        radar_enabled=_env_bool("RADAR_ENABLED", True),
        radar_check_every_min=_env_int("RADAR_CHECK_EVERY_MINUTES", 30),
        radar_accounts_per_check=_env_int("RADAR_ACCOUNTS_PER_CHECK", 6),
        radar_max_age_min=_env_int("RADAR_MAX_AGE_MINUTES", 45),
        radar_max_per_day=_env_int("RADAR_MAX_PER_DAY", 10),
        radar_window=parse_window(_env("RADAR_WINDOW", "08:00-22:30")),
        radar_min_likes_per_min=_env_float("RADAR_MIN_LIKES_PER_MIN", 1.0),
        x_news_accounts_enabled=_env_bool("X_NEWS_ACCOUNTS_ENABLED", True),
        x_budget_daily_usd=_env_float("X_BUDGET_DAILY_USD", 1.50),
        x_budget_monthly_usd=_env_float("X_BUDGET_MONTHLY_USD", 30.0),
        screenshots_enabled=_env_bool("SCREENSHOTS_ENABLED", True),
        image_ai_enabled=_env_bool("IMAGE_AI_ENABLED", False),
        image_model=_env("IMAGE_MODEL", "gpt-image-1") or "gpt-image-1",
        image_size=_env("IMAGE_SIZE", "1024x1024") or "1024x1024",
        image_quality=_env("IMAGE_QUALITY", "medium") or "medium",
        radar_accounts=_handles(js.get("radar_accounts")),
        x_news_accounts=_handles(js.get("x_news_accounts")),
        news_feeds=[f for f in (js.get("news_feeds") or []) if isinstance(f, dict) and f.get("url")],
        blacklist_users=[u.lower().lstrip("@") for u in (js.get("blacklist", {}).get("users") or [])],
        blacklist_keywords=[k.lower() for k in (js.get("blacklist", {}).get("keywords") or [])],
        fallback_captions={k: list(v) for k, v in (content.get("fallback_captions") or {}).items()
                           if isinstance(v, list)},
        evergreen_topics=[t for t in (content.get("evergreen_topics") or []) if isinstance(t, str)],
    )
    weekly = _env("WEEKLY_REPORT_AT", "sun@20:30")
    if weekly:
        day, _, hhmm = weekly.lower().partition("@")
        if day not in WEEKDAYS:
            raise ConfigError(f"WEEKLY_REPORT_AT : jour inconnu {day!r}")
        s.weekly_report = Slot("weekly", parse_hhmm(hhmm), WEEKDAYS[day])
    return s
