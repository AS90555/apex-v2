"""
Governance-Invarianten-Check für APEX V2.
Läuft auf Test-DB (nie Live-DB).
Exit 0 = alles ok | Exit 1 = Invariante verletzt.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys


TEST_DB = os.environ.get(
    "APEX_TEST_DB",
    os.path.join(os.path.dirname(__file__), "..", "data", "test_apex_v2.db"),
)


def _connect() -> sqlite3.Connection:
    if not os.path.exists(TEST_DB):
        print(f"[SKIP] Test-DB nicht gefunden: {TEST_DB} — erzeuge leere In-Memory-DB")
        return sqlite3.connect(":memory:")
    return sqlite3.connect(TEST_DB)


def check_approved_signals_have_governance_log(conn: sqlite3.Connection) -> list[str]:
    """Kein Signal mit status='approved' darf checks_json=NULL oder '' haben."""
    errors: list[str] = []
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "signals" not in tables or "governance_log" not in tables:
        return errors  # leere Test-DB
    rows = conn.execute(
        """
        SELECT s.id, s.strategy, s.asset, gl.checks_json
        FROM signals s
        LEFT JOIN governance_log gl ON gl.signal_id = s.id
        WHERE s.status = 'approved'
        """
    ).fetchall()
    for sig_id, strategy, asset, checks_json in rows:
        if not checks_json or checks_json.strip() in ("", "null", "{}"):
            errors.append(
                f"Signal id={sig_id} ({strategy}/{asset}) status=approved "
                f"hat leeres/fehlendes checks_json in governance_log"
            )
        else:
            try:
                parsed = json.loads(checks_json)
                if not isinstance(parsed, dict) or len(parsed) == 0:
                    errors.append(
                        f"Signal id={sig_id} ({strategy}/{asset}) checks_json ist "
                        f"kein befülltes Objekt: {checks_json!r}"
                    )
            except json.JSONDecodeError:
                errors.append(
                    f"Signal id={sig_id} ({strategy}/{asset}) checks_json ist kein "
                    f"gültiges JSON: {checks_json!r}"
                )
    return errors


def check_live_deployments_have_trades(conn: sqlite3.Connection) -> list[str]:
    """Kein active_deployment mit mode='live' ohne mindestens einen Trade."""
    errors: list[str] = []
    # Prüfe ob Tabelle existiert
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "active_deployments" not in tables or "trades" not in tables:
        return errors  # leere Test-DB: nichts zu prüfen

    rows = conn.execute(
        """
        SELECT ad.id, ad.strategy_key, ad.asset, ad.mode
        FROM active_deployments ad
        WHERE ad.mode = 'live' AND ad.active = 1
        """
    ).fetchall()
    for dep_id, strategy_key, asset, mode in rows:
        trade_count = conn.execute(
            """
            SELECT COUNT(*) FROM trades t
            JOIN signals s ON s.id = t.signal_id
            JOIN active_deployments ad ON ad.strategy_key = s.strategy
            WHERE ad.id = ? AND s.status IN ('approved', 'executed')
            """,
            (dep_id,),
        ).fetchone()[0]
        # Fallback: zähle direkt über strategy + asset
        if trade_count == 0:
            trade_count = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE strategy = ? AND asset = ? AND mode = 'live'",
                (strategy_key.rsplit("_", 1)[0], asset),
            ).fetchone()[0]
        if trade_count == 0:
            errors.append(
                f"active_deployment id={dep_id} ({strategy_key}/{asset}) mode=live "
                f"hat keine Trades in der trades-Tabelle"
            )
    return errors


def main() -> int:
    conn = _connect()
    all_errors: list[str] = []

    checks = [
        ("approved-Signals haben governance_log.checks_json", check_approved_signals_have_governance_log),
        ("live-Deployments haben mindestens einen Trade", check_live_deployments_have_trades),
    ]

    for label, fn in checks:
        errs = fn(conn)
        if errs:
            print(f"FAIL [{label}]")
            for e in errs:
                print(f"  ✗ {e}")
            all_errors.extend(errs)
        else:
            print(f"PASS [{label}]")

    conn.close()

    if all_errors:
        print(f"\nErgebnis: {len(all_errors)} Invariante(n) verletzt")
        return 1

    print("\nErgebnis: alle Governance-Invarianten OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
