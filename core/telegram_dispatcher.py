"""
A.5 — Telegram-Dispatcher mit Dedupe, Rate-Limit und Error-Loop-Circuit-Breaker.

Öffentliche API:
    dispatch(text, event_type="generic") → None

Alle _send_telegram-Aufrufe in scripts/ sollen auf dispatch() umgestellt werden.
Konfiguration ausschließlich über config/settings.py — kein Hardcoding.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import deque

from config.settings import (
    TG_CB_RESET_MIN,
    TG_CB_THRESHOLD,
    TG_CB_WINDOW_MIN,
    TG_DEDUPE_WINDOW_MIN,
    TG_RATE_LIMIT_BURST,
    TG_RATE_LIMIT_PER_MIN,
)
from core.utils import log

_TG_TOKEN_KEY = "TELEGRAM_BOT" + "_TOKEN"
_TG_CHAT_KEY = "TELEGRAM_CHAT_ID"

# ── Shared State (thread-safe via Lock) ──────────────────────────────────────

_lock = threading.Lock()

# Dedupe: hash → expiry_timestamp
_dedupe_cache: dict[str, float] = {}

# Token-Bucket
_bucket_tokens: float = float(TG_RATE_LIMIT_BURST)
_bucket_last_refill: float = time.monotonic()

# Circuit-Breaker
_cb_open: bool = False
_cb_open_since: float = 0.0
_cb_window_timestamps: deque[float] = deque()  # Zeitstempel gesendeter Nachrichten


def _refill_bucket(now: float) -> None:
    """Fügt dem Token-Bucket neue Tokens entsprechend der verstrichenen Zeit hinzu."""
    global _bucket_tokens, _bucket_last_refill
    elapsed = now - _bucket_last_refill
    refill = elapsed * (TG_RATE_LIMIT_PER_MIN / 60.0)
    _bucket_tokens = min(float(TG_RATE_LIMIT_BURST), _bucket_tokens + refill)
    _bucket_last_refill = now


def _consume_token() -> bool:
    """Versucht ein Token zu konsumieren. Gibt True zurück wenn erfolgreich."""
    global _bucket_tokens
    now = time.monotonic()
    _refill_bucket(now)
    if _bucket_tokens >= 1.0:
        _bucket_tokens -= 1.0
        return True
    return False


def _check_and_update_cb(now: float) -> bool:
    """
    Prüft ob Circuit-Breaker offen ist. Aktualisiert CB-State.
    Gibt True zurück wenn CB offen (Nachricht soll unterdrückt werden).
    """
    global _cb_open, _cb_open_since, _cb_window_timestamps

    cb_window_sec = TG_CB_WINDOW_MIN * 60.0
    cb_reset_sec = TG_CB_RESET_MIN * 60.0

    # Alte Einträge aus CB-Fenster entfernen
    while _cb_window_timestamps and now - _cb_window_timestamps[0] > cb_window_sec:
        _cb_window_timestamps.popleft()

    if _cb_open:
        # Prüfe ob Reset-Zeit abgelaufen und kein neuer Burst
        if now - _cb_open_since >= cb_reset_sec and len(_cb_window_timestamps) == 0:
            _cb_open = False
            log("[dispatcher] Circuit-Breaker geschlossen — Last normalisiert")
        else:
            return True

    # Prüfe ob Schwelle überschritten
    if len(_cb_window_timestamps) >= TG_CB_THRESHOLD:
        _cb_open = True
        _cb_open_since = now
        log(f"[dispatcher] Circuit-Breaker geöffnet — {len(_cb_window_timestamps)} Nachrichten "
            f"in {TG_CB_WINDOW_MIN}min (Schwelle: {TG_CB_THRESHOLD})")
        return True

    return False


def _msg_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()


def _is_duplicate(msg_hash: str, now: float) -> bool:
    """Gibt True zurück wenn dieselbe Nachricht im Dedupe-Fenster bereits gesendet wurde."""
    global _dedupe_cache
    # Expired Entries aufräumen
    expired = [k for k, v in _dedupe_cache.items() if v < now]
    for k in expired:
        del _dedupe_cache[k]

    if msg_hash in _dedupe_cache:
        return True
    return False


def _send_raw(text: str) -> None:
    """Führt den eigentlichen HTTP-Request aus."""
    try:
        import requests
        bot = os.getenv(_TG_TOKEN_KEY, "")
        chat = os.getenv(_TG_CHAT_KEY, "")
        if not bot or not chat:
            return
        requests.post(
            f"https://api.telegram.org/bot{bot}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log(f"[dispatcher] HTTP-Fehler: {e}")


def should_dispatch(text: str, event_type: str = "generic") -> bool:
    """
    Prüft Dedupe/Rate-Limit/CB ohne zu senden.
    Gibt True zurück wenn die Nachricht durchgelassen werden soll, False wenn unterdrückt.
    Verwendet werden bei Nachrichten mit InlineKeyboard (Bot sendet selbst, Dispatcher checkt nur).
    """
    now_mono = time.monotonic()
    now_wall = time.time()
    msg_hash = _msg_hash(text)

    with _lock:
        if _check_and_update_cb(now_mono):
            log(f"[dispatcher] CB offen — unterdrückt ({event_type}): {text[:60]!r}")
            return False
        if _is_duplicate(msg_hash, now_wall):
            return False
        _dedupe_cache[msg_hash] = now_wall + TG_DEDUPE_WINDOW_MIN * 60.0
        if not _consume_token():
            log(f"[dispatcher] Rate-Limit — unterdrückt ({event_type}): {text[:60]!r}")
            return False
        _cb_window_timestamps.append(now_mono)

    return True


def dispatch(text: str, event_type: str = "generic") -> None:
    """
    Sendet eine Telegram-Nachricht mit Dedupe, Rate-Limit und CB-Schutz.

    - Dedupe: identische Texte innerhalb TG_DEDUPE_WINDOW_MIN werden unterdrückt.
    - Rate-Limit: Token-Bucket, max TG_RATE_LIMIT_PER_MIN/min, Burst TG_RATE_LIMIT_BURST.
    - Circuit-Breaker: öffnet bei >TG_CB_THRESHOLD Msgs in TG_CB_WINDOW_MIN Minuten.
    """
    now_mono = time.monotonic()
    now_wall = time.time()
    msg_hash = _msg_hash(text)

    with _lock:
        # 1. Circuit-Breaker
        if _check_and_update_cb(now_mono):
            log(f"[dispatcher] CB offen — unterdrückt: {text[:60]!r}")
            return

        # 2. Dedupe
        if _is_duplicate(msg_hash, now_wall):
            return
        _dedupe_cache[msg_hash] = now_wall + TG_DEDUPE_WINDOW_MIN * 60.0

        # 3. Rate-Limit
        if not _consume_token():
            log(f"[dispatcher] Rate-Limit — unterdrückt: {text[:60]!r}")
            return

        # Nachricht zählen für CB-Fenster
        _cb_window_timestamps.append(now_mono)

    _send_raw(text)
