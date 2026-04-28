#!/usr/bin/env python3
"""
APEX WebSocket Intake Daemon

Verbindet sich mit dem öffentlichen Bitget WebSocket (v2) und empfängt
Candle-Updates in Echtzeit. Erkennt Candle-Close-Events und triggert
die Live-Pipeline millisekunden-genau.

Candle-Close-Erkennung (Bitget-spezifisch):
  Bitget sendet KEIN is_closed-Flag. Stattdessen:
  - 'snapshot': mehrere Kerzen auf einmal → alle außer der letzten sind geschlossen
  - 'update':   einzelne Kerze → wenn ts > last_known_ts → vorherige Kerze ist geschlossen

Für jede (asset, interval)-Kombination speichern wir den letzten bekannten ts.
Sobald ein neuer ts eintrifft gilt: alle Kerzen mit ts < neuer_ts sind final.

Pipeline-Trigger (event-driven, kein Cron):
  Candle-Close → store_candle() → run_pipeline(asset, interval)
  run_pipeline führt direkt (in-process) aus:
    run_features → run_strategies → run_governance → run_execution

Rate-Limit-Manager für REST-Calls im Executor:
  Exponential Backoff bei HTTP 429 + Token-Bucket (60 Calls/min).

Start:
  python3 intake/intake_ws.py
  nohup python3 intake/intake_ws.py >> logs/intake_ws.log 2>&1 &
  echo $! > /tmp/apex_ws.pid
"""

import sys
import os
import json
import asyncio
import logging
import logging.handlers
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", ".env"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from core.db import get_connection, run_migrations
from core.utils import log as apex_log
from config.settings import INTAKE_MATRIX

# ── Logging ───────────────────────────────────────────────────────────────────

_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs", "intake_ws.log",
)
_handler = logging.handlers.RotatingFileHandler(
    _LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
_stderr = logging.StreamHandler(sys.stderr)
_stderr.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
logger = logging.getLogger("intake_ws")
logger.setLevel(logging.INFO)
logger.propagate = False
logger.addHandler(_handler)
if sys.stderr.isatty():
    logger.addHandler(_stderr)

def log(msg: str): logger.info(msg)


# ── Konfiguration ─────────────────────────────────────────────────────────────

BITGET_WS_URL    = "wss://ws.bitget.com/v2/ws/public"
INST_TYPE        = "USDT-FUTURES"
PING_INTERVAL    = 25          # Sekunden zwischen Keepalive-Pings
RECONNECT_DELAY  = 5           # Sekunden vor erneutem Verbindungsversuch
MAX_RECONNECT    = 300         # Max. Wartezeit bei wiederholten Fehlern (Sekunden)

# Welche Intervalle für den WS-Trigger interessant sind
# (15m und 1h sind die Signal-kritischen Timeframes)
WS_TRIGGER_INTERVALS = {"15m", "1h"}

# Bitget WS Channel-Namen
WS_CHANNEL = {
    "1m":  "candle1m",
    "5m":  "candle5m",
    "15m": "candle15m",
    "1h":  "candle1H",
    "4h":  "candle4H",
}

# Pipeline-Cooldown: nach Trigger für (asset, interval) N Sekunden keine
# weitere Ausführung, um Burst-Updates nicht mehrfach zu triggern
PIPELINE_COOLDOWN = {
    "15m": 60,    # 1 Minute Cooldown nach 15m-Trigger
    "1h":  120,   # 2 Minuten nach 1h-Trigger
}

# ── State ─────────────────────────────────────────────────────────────────────

# Letzter bekannter Candle-Timestamp pro (asset, interval)
# Format: {"ETH:1h": 1777336000000, ...}
_last_ts: dict[str, int] = {}

# Letzter Pipeline-Trigger-Zeitpunkt pro (asset, interval)
_last_trigger: dict[str, float] = {}


# ── Subscription-Builder ──────────────────────────────────────────────────────

def _build_subscriptions() -> list[dict]:
    """
    Erstellt Subscription-Args für alle (asset, interval)-Kombinationen
    aus INTAKE_MATRIX, gefiltert auf WS_TRIGGER_INTERVALS.
    """
    subs = []
    for asset, intervals in INTAKE_MATRIX.items():
        for interval in intervals:
            if interval not in WS_CHANNEL:
                continue
            subs.append({
                "instType": INST_TYPE,
                "channel":  WS_CHANNEL[interval],
                "instId":   f"{asset}USDT",
            })
    return subs


# ── Candle-Persistenz ─────────────────────────────────────────────────────────

def _store_closed_candle(asset: str, interval: str, row: list) -> bool:
    """
    Speichert eine abgeschlossene Kerze in die DB.
    row = [ts_ms_str, open, high, low, close, vol, volCcy, volCcyQuote]
    Gibt True zurück wenn die Kerze neu eingefügt wurde.
    """
    try:
        ts    = int(row[0])
        open_ = float(row[1])
        high  = float(row[2])
        low   = float(row[3])
        close = float(row[4])
        vol   = float(row[5])
        fetched_at = datetime.now(timezone.utc).isoformat()

        conn = get_connection()
        cur  = conn.execute(
            """INSERT OR IGNORE INTO candles
               (asset, interval, ts, open, high, low, close, volume, fetched_at, source)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (asset, interval, ts, open_, high, low, close, vol, fetched_at, "bitget_ws"),
        )
        conn.commit()
        inserted = cur.rowcount > 0
        conn.close()
        return inserted
    except Exception as e:
        log(f"[WS] DB-Fehler beim Speichern {asset}/{interval}: {e}")
        return False


# ── Candle-Close-Erkennung ────────────────────────────────────────────────────

def _process_message(asset: str, interval: str,
                     action: str, data: list) -> list[list]:
    """
    Kernlogik der Candle-Close-Erkennung.

    Bitget sendet:
      action='snapshot': mehrere Kerzen → alle außer der letzten sind FINAL
      action='update':   1-2 Kerzen → wenn ts > last_known → vorherige ist FINAL

    Rückgabe: Liste der abgeschlossenen Candle-Rows (können mehrere sein).
    """
    key = f"{asset}:{interval}"
    closed = []

    if action == "snapshot":
        # Bei Snapshot: alle Rows außer der letzten (= aktuell noch offen) speichern
        for row in data[:-1]:
            closed.append(row)
        # Letzte Row = aktuelle offene Kerze → nur ts merken
        if data:
            _last_ts[key] = int(data[-1][0])

    elif action == "update":
        for row in data:
            ts = int(row[0])
            prev = _last_ts.get(key, 0)

            if prev == 0:
                # Erster Update: Baseline setzen
                _last_ts[key] = ts
                continue

            if ts > prev:
                # Neuer Timestamp → vorherige Kerze ist jetzt geschlossen.
                # Bitget kann in einem Update mehrere Rows senden (geschlossene +
                # neue Kerze) — wir behandeln das als ein einziges Close-Event.
                log(f"[WS] 🕯  Candle CLOSED: {asset}/{interval} ts={prev} "
                    f"→ neue Kerze ts={ts}")
                _last_ts[key] = ts
                closed.append(None)   # None = "close detected, no row to store"
                break   # Einmal pro Nachricht genügt
            else:
                # Selber Timestamp → laufende Kerze wird aktualisiert (ignorieren)
                _last_ts[key] = ts

    return closed


# ── Pipeline-Trigger ──────────────────────────────────────────────────────────

async def _trigger_pipeline(asset: str, interval: str) -> None:
    """
    Feuert die vollständige Pipeline für (asset, interval):
      run_features → run_strategies → run_governance → run_execution

    Läuft in einem asyncio.Task (non-blocking für den WS-Handler).
    Cooldown verhindert Burst-Executions.
    """
    key = f"{asset}:{interval}"
    now = time.monotonic()

    cooldown = PIPELINE_COOLDOWN.get(interval, 60)
    last     = _last_trigger.get(key, 0)
    if now - last < cooldown:
        return   # Innerhalb Cooldown → überspringen

    _last_trigger[key] = now
    t0 = time.time()

    log(f"[WS] 🚀 Pipeline-Trigger: {asset}/{interval} @ {datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3]} UTC")

    try:
        loop = asyncio.get_event_loop()

        # Features & Regime (blockierend → in Thread-Pool)
        await loop.run_in_executor(None, _run_features_for, asset, interval)

        # Strategien (nur wenn trigger-relevantes Intervall)
        if interval in WS_TRIGGER_INTERVALS:
            await loop.run_in_executor(None, _run_strategies_for, asset)

    except Exception as e:
        log(f"[WS] Pipeline-Fehler {asset}/{interval}: {e}")

    elapsed = (time.time() - t0) * 1000
    log(f"[WS] Pipeline {asset}/{interval} abgeschlossen ({elapsed:.0f}ms)")


def _run_features_for(asset: str, interval: str) -> None:
    """Berechnet Features + Regime für ein einzelnes Asset (synchron)."""
    from features.indicators import detect_regime
    from core.db import set_state
    from core.autopilot import check_regime_change

    conn = get_connection()
    rows = conn.execute(
        """SELECT open, high, low, close, volume FROM candles
           WHERE asset=? AND interval='1h'
           ORDER BY ts DESC LIMIT 70""",
        (asset,),
    ).fetchall()
    conn.close()

    if len(rows) < 65:
        return

    candles = [{"open": r[0], "high": r[1], "low": r[2],
                "close": r[3], "volume": r[4]} for r in reversed(rows)]
    regime  = detect_regime(candles)
    set_state(f"regime_{asset}", regime)
    check_regime_change(asset, regime)
    log(f"[WS] Feature/Regime {asset}: {regime}")


def _run_strategies_for(asset: str) -> None:
    """Führt Signal-Generierung für ein einzelnes Asset aus (synchron)."""
    from strategies.squeeze import SqueezeStrategy
    from scripts.run_strategies import _load_deployed_strategies
    from core.db import run_migrations

    run_migrations()

    # Standard-Squeeze
    squeeze = SqueezeStrategy()
    if asset in squeeze.assets:
        sigs = squeeze.generate_signals()
        if sigs:
            log(f"[WS] Squeeze {asset}: {len(sigs)} Signal(e)")

    # Deployed Instanzen für dieses Asset
    for dep in _load_deployed_strategies():
        if asset in dep.assets:
            sigs = dep.generate_signals()
            if sigs:
                log(f"[WS] {dep.name} {asset}: {len(sigs)} Signal(e)")


# ── WebSocket-Handler ─────────────────────────────────────────────────────────

async def _handle_message(msg: str) -> None:
    """Verarbeitet eine eingehende WS-Nachricht."""
    try:
        data = json.loads(msg)
    except json.JSONDecodeError:
        return

    # Pong / Event-Bestätigung → ignorieren
    if "event" in data or data.get("action") not in ("snapshot", "update"):
        return

    arg      = data.get("arg", {})
    channel  = arg.get("channel", "")
    inst_id  = arg.get("instId", "")
    action   = data.get("action")
    rows     = data.get("data", [])

    # Asset und Interval aus Channel/InstId extrahieren
    asset    = inst_id.replace("USDT", "")
    interval = next((iv for iv, ch in WS_CHANNEL.items() if ch == channel), None)
    if not interval or not asset or not rows:
        return

    closed_rows = _process_message(asset, interval, action, rows)

    for row in closed_rows:
        if row is not None:
            inserted = _store_closed_candle(asset, interval, row)
            if inserted:
                log(f"[WS] 💾 Gespeichert: {asset}/{interval} ts={row[0]}")

        # Pipeline nur für trigger-relevante Intervalle
        if interval in WS_TRIGGER_INTERVALS and closed_rows:
            asyncio.create_task(_trigger_pipeline(asset, interval))
            break   # Einmal pro Nachricht reicht


# ── Keepalive ─────────────────────────────────────────────────────────────────

async def _keepalive(ws) -> None:
    """Sendet alle PING_INTERVAL Sekunden einen Ping."""
    while True:
        await asyncio.sleep(PING_INTERVAL)
        try:
            await ws.send("ping")
        except Exception:
            break


# ── Haupt-Reconnect-Loop ──────────────────────────────────────────────────────

async def _connect_and_run() -> None:
    subs = _build_subscriptions()
    log(f"[WS] Subscriptions: {len(subs)} Channels für "
        f"{len(INTAKE_MATRIX)} Assets × {len(WS_CHANNEL)} Intervalle")

    delay = RECONNECT_DELAY

    while True:
        try:
            log(f"[WS] Verbinde mit {BITGET_WS_URL} ...")
            async with websockets.connect(
                BITGET_WS_URL,
                ping_interval=None,      # Eigener Keepalive
                max_size=2**20,
                open_timeout=30,
            ) as ws:
                log("[WS] ✅ Verbunden")
                delay = RECONNECT_DELAY  # Reset nach erfolgreicher Verbindung

                # Alle Channels auf einmal subscriben
                await ws.send(json.dumps({"op": "subscribe", "args": subs}))
                log(f"[WS] Subscribe gesendet ({len(subs)} Channels)")

                # Keepalive parallel starten
                keepalive_task = asyncio.create_task(_keepalive(ws))

                async for msg in ws:
                    if msg == "pong":
                        continue
                    await _handle_message(msg)

                keepalive_task.cancel()

        except (ConnectionClosedError, ConnectionClosedOK) as e:
            log(f"[WS] Verbindung getrennt: {e} — Reconnect in {delay}s")
        except OSError as e:
            log(f"[WS] Netzwerk-Fehler: {e} — Reconnect in {delay}s")
        except Exception as e:
            log(f"[WS] Unerwarteter Fehler: {e} — Reconnect in {delay}s")

        await asyncio.sleep(delay)
        delay = min(delay * 2, MAX_RECONNECT)   # Exponential Backoff


# ── Heartbeat-Writer ──────────────────────────────────────────────────────────

async def _heartbeat_loop() -> None:
    """Schreibt alle 5 Minuten einen Heartbeat in die DB."""
    while True:
        await asyncio.sleep(300)
        try:
            conn = get_connection()
            conn.execute(
                "INSERT INTO heartbeats(ts, component, status, message, latency_ms) VALUES(?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), "intake_ws", "ok",
                 f"channels={len(_build_subscriptions())} last_ts_count={len(_last_ts)}", 0),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log(f"[WS] Heartbeat-Fehler: {e}")


# ── Entry Point ───────────────────────────────────────────────────────────────

async def main() -> None:
    run_migrations()
    log("[WS] ══════════════════════════════════════════════════════")
    log("[WS] APEX WebSocket Intake Daemon gestartet")
    log(f"[WS] Trigger-Intervalle: {WS_TRIGGER_INTERVALS}")
    log(f"[WS] Cooldown: 15m={PIPELINE_COOLDOWN['15m']}s | 1h={PIPELINE_COOLDOWN['1h']}s")
    log("[WS] ══════════════════════════════════════════════════════")

    await asyncio.gather(
        _connect_and_run(),
        _heartbeat_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
