"""
Emergency Close All — wird NUR vom Dead Man's Switch aufgerufen.

KEINE core.db-Abhängigkeit — minimaler direkter HTTPS-Request.
Schließt alle offenen Positionen via Bitget closePosition.
Schreibt Audit-Log nach logs/emergency_${ts}.log.
Kein Auto-Restart — Admin muss manuell intervenieren.

Benötigte Umgebungsvariablen: APEX_KEY, APEX_SECRET, APEX_PASS
(entsprechen BITGET-API-Credentials aus config/.env)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(f, msg: str) -> None:
    line = f"[{_now_iso()}] {msg}"
    print(line, file=sys.stderr)
    f.write(line + "\n")
    f.flush()


def _sign(secret: str, ts: str, method: str, path: str, body: str = "") -> str:
    msg = ts + method + path + body
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def _bitget_request(
    api_key: str, secret: str, passphrase: str,
    method: str, path: str, body: dict | None = None,
) -> Any:
    import urllib.request

    base_url = "https://api.bitget.com"
    ts = str(int(time.time() * 1000))
    body_str = json.dumps(body) if body else ""
    sig = _sign(secret, ts, method.upper(), path, body_str)

    headers = {
        "ACCESS-KEY":        api_key,
        "ACCESS-SIGN":       sig,
        "ACCESS-TIMESTAMP":  ts,
        "ACCESS-PASSPHRASE": passphrase,
        "Content-Type":      "application/json",
        "locale":            "en-US",
    }

    url = base_url + path
    data = body_str.encode() if body_str else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())

    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def main() -> None:
    # Credentials aus Umgebung lesen — Namen absichtlich von Produktions-Vars getrennt
    # Cron muss diese vor Aufruf setzen (z. B. aus config/.env exportieren)
    api_key    = os.getenv("APEX_KEY", "")
    secret     = os.getenv("APEX_SECRET", "")
    passphrase = os.getenv("APEX_PASS", "")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"emergency_{ts}.log")

    with open(log_path, "w") as f:
        _log(f, "=== EMERGENCY CLOSE ALL STARTED ===")
        _log(f, f"Triggered at {_now_iso()}")

        if not api_key or not secret or not passphrase:
            _log(f, "FEHLER: API-Credentials fehlen. Setze APEX_KEY, APEX_SECRET, APEX_PASS.")
            sys.exit(2)

        # Hole offene Positionen
        try:
            result = _bitget_request(
                api_key, secret, passphrase,
                "GET", "/api/mix/v1/position/allPosition?productType=umcbl",
            )
            positions = result.get("data", [])
            _log(f, f"Offene Positionen gefunden: {len(positions)}")
        except Exception as e:
            _log(f, f"FEHLER beim Abrufen der Positionen: {e}")
            sys.exit(3)

        closed = 0
        failed = 0
        for pos in positions:
            total = float(pos.get("total", 0))
            if abs(total) < 1e-8:
                continue

            symbol      = pos.get("symbol", "")
            hold_side   = pos.get("holdSide", "long")
            margin_coin = pos.get("marginCoin", "USDT")

            _log(f, f"Schließe: {symbol} holdSide={hold_side} size={total}")

            try:
                close_body = {
                    "symbol":     symbol,
                    "holdSide":   hold_side,
                    "marginCoin": margin_coin,
                }
                resp = _bitget_request(
                    api_key, secret, passphrase,
                    "POST", "/api/mix/v1/order/close-position",
                    body=close_body,
                )
                _log(f, f"  → Response: {resp}")
                closed += 1
            except Exception as e:
                _log(f, f"  → FEHLER: {e}")
                failed += 1

        _log(f, f"=== FERTIG: closed={closed}, failed={failed} ===")
        _log(f, "Admin-Intervention erforderlich vor Neustart!")
        _log(f, f"Audit-Log: {log_path}")

    print(f"Emergency close abgeschlossen. Log: {log_path}", file=sys.stderr)
    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
