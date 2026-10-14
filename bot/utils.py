"""Utilitaires partagés : chemins, logs, watchdog, heure locale, JSON atomique."""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
LOGS_DIR = PROJECT_ROOT / "logs"
STATE_DIR = PROJECT_ROOT / "state"
MEDIA_DIR = PROJECT_ROOT / "media"

log = logging.getLogger("xbot")


def ensure_dirs() -> None:
    for d in (LOGS_DIR, STATE_DIR, MEDIA_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ----------------------------------------------------------------------
# Logs
# ----------------------------------------------------------------------
def setup_logging(level: str = "INFO") -> logging.Logger:
    from logging.handlers import RotatingFileHandler

    ensure_dirs()
    fmt = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    file_handler = RotatingFileHandler(
        LOGS_DIR / "bot.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        handlers=[file_handler, logging.StreamHandler(sys.stdout)],
        force=True,
    )
    # Les bibliothèques HTTP sont bavardes en DEBUG et peuvent logger des
    # en-têtes : on les garde au niveau WARNING quoi qu'il arrive.
    for noisy in ("urllib3", "requests_oauthlib", "oauthlib", "httpx", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log


# ----------------------------------------------------------------------
# Watchdog "no-log" : si plus aucun log pendant N secondes, le process se
# tue et systemd le relance. La boucle principale logge un battement à
# chaque tour, donc un silence = process figé (appel réseau bloqué…).
# ----------------------------------------------------------------------
_last_log_time = time.time()
_last_log_lock = threading.Lock()


class _HeartbeatFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        global _last_log_time
        with _last_log_lock:
            _last_log_time = time.time()
        return True


def _watchdog_loop(max_silence_s: int) -> None:
    while True:
        time.sleep(30)
        with _last_log_lock:
            silence = time.time() - _last_log_time
        if silence > max_silence_s:
            try:
                logging.getLogger("xbot.watchdog").critical(
                    "WATCHDOG : aucun log depuis %.0fs (> %ds), arrêt pour redémarrage systemd",
                    silence, max_silence_s,
                )
            except Exception:
                pass
            os.kill(os.getpid(), signal.SIGKILL)


def start_watchdog(max_silence_s: int = 420) -> None:
    root = logging.getLogger()
    flt = _HeartbeatFilter()
    if root.handlers:
        for h in root.handlers:
            h.addFilter(flt)
    else:
        root.addFilter(flt)
    threading.Thread(target=_watchdog_loop, args=(max_silence_s,), daemon=True,
                     name="watchdog").start()
    log.info("Watchdog démarré : redémarrage si aucun log pendant %ds", max_silence_s)


class heartbeat:
    """Pendant une opération longue mais légitime (appel Claude), un thread
    logge toutes les `every_s` secondes pour que le watchdog ne tue pas le
    bot. S'arrête seul après `max_total_s` : un vrai blocage reste détecté."""

    def __init__(self, label: str, every_s: float = 60.0, max_total_s: float = 300.0):
        self.label = label
        self.every_s = every_s
        self.max_total_s = max_total_s
        self._stop = threading.Event()

    def _run(self) -> None:
        started = time.time()
        lg = logging.getLogger("xbot.heartbeat")
        while not self._stop.wait(self.every_s):
            elapsed = time.time() - started
            if elapsed > self.max_total_s:
                lg.warning("heartbeat [%s] : %.0fs > %.0fs max, le watchdog jugera",
                           self.label, elapsed, self.max_total_s)
                return
            lg.info("heartbeat [%s] : en cours depuis %.0fs", self.label, elapsed)

    def __enter__(self):
        threading.Thread(target=self._run, daemon=True, name=f"hb-{self.label}").start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        return False


# ----------------------------------------------------------------------
# JSON
# ----------------------------------------------------------------------
def load_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def save_json(path: Path, data: Any) -> None:
    """Écriture atomique : .tmp + fsync + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ----------------------------------------------------------------------
# Heure locale (une seule source de vérité, remplaçable dans les tests)
# ----------------------------------------------------------------------
_clock_override: datetime | None = None


def tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("TIMEZONE", "Europe/Paris"))


def now_local() -> datetime:
    if _clock_override is not None:
        return _clock_override
    return datetime.now(tz())


def set_clock(dt: datetime | None) -> None:
    """Tests uniquement : fige l'horloge (datetime aware) ou la libère (None)."""
    global _clock_override
    _clock_override = dt


def parse_hhmm(value: str) -> dtime:
    h, m = value.strip().split(":")
    return dtime(int(h), int(m))


def parse_window(value: str) -> tuple[dtime, dtime]:
    """'08:00-22:30' -> (08:00, 22:30). Une fenêtre peut passer minuit."""
    start, end = value.split("-")
    return parse_hhmm(start), parse_hhmm(end)


def in_window(t: dtime, window: tuple[dtime, dtime]) -> bool:
    start, end = window
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end  # fenêtre de nuit, ex. 23:30-07:30


def at_local(day: datetime, t: dtime) -> datetime:
    """Datetime local du jour `day` à l'heure `t` (gère le changement d'heure)."""
    naive = datetime(day.year, day.month, day.day, t.hour, t.minute)
    return naive.replace(tzinfo=tz())


def minutes_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz())
    return dt


def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")


def days_ago(n: int) -> datetime:
    return now_local() - timedelta(days=n)
