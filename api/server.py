"""
Read-only Flask-API für das V2-Dashboard (Port 8890).
Bearer-Token-Auth aus config/settings.py.
SQLite im Read-only-Modus.
Nur GET-Endpoints, kein Schreiben.
"""

import json
import sqlite3
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, request, abort
from config.settings import API_PORT, API_BEARER_TOKEN, DATA_DIR

app = Flask(__name__)
DB_PATH = os.path.join(DATA_DIR, "apex_v2.db")


def _get_ro_conn():
    """SQLite Read-only-Verbindung."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _auth():
    if not API_BEARER_TOKEN:
        return  # kein Token konfiguriert → offen (nur lokal)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != API_BEARER_TOKEN:
        abort(401)


def _rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route("/api/v2/health")
def health():
    _auth()
    conn = _get_ro_conn()
    rows = conn.execute(
        """SELECT component, MAX(ts) as last_ts, status, message, latency_ms
           FROM heartbeats GROUP BY component ORDER BY component""",
    ).fetchall()
    conn.close()
    return jsonify({"ok": True, "components": _rows_to_list(rows)})


@app.route("/api/v2/signals")
def signals():
    _auth()
    limit = min(int(request.args.get("limit", 50)), 500)
    strategy = request.args.get("strategy")
    status   = request.args.get("status")

    where, params = [], []
    if strategy:
        where.append("strategy=?"); params.append(strategy)
    if status:
        where.append("status=?"); params.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    conn = _get_ro_conn()
    rows = conn.execute(
        f"""SELECT id, created_at, strategy, asset, direction, entry_price,
                   stop_loss, take_profit_1, take_profit_2, size, risk_usd,
                   session, status, mode, reject_reason, governance_ts, execution_ts, order_id
            FROM signals {clause}
            ORDER BY id DESC LIMIT ?""",
        params + [limit],
    ).fetchall()
    conn.close()
    return jsonify({"count": len(rows), "signals": _rows_to_list(rows)})


@app.route("/api/v2/trades")
def trades():
    _auth()
    limit = min(int(request.args.get("limit", 100)), 1000)
    conn = _get_ro_conn()
    rows = conn.execute(
        """SELECT id, signal_id, strategy, asset, direction, entry_price, entry_ts,
                  size, stop_loss, take_profit_1, take_profit_2, exit_price, exit_ts,
                  exit_reason, pnl_usd, pnl_r, be_applied, mode, order_id, session
           FROM trades ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return jsonify({"count": len(rows), "trades": _rows_to_list(rows)})


@app.route("/api/v2/pnl")
def pnl():
    _auth()
    conn = _get_ro_conn()
    row = conn.execute(
        """SELECT COUNT(*) as total_trades,
                  SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                  SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) as losses,
                  ROUND(SUM(pnl_usd), 4) as total_pnl_usd,
                  ROUND(SUM(pnl_r), 4) as total_pnl_r,
                  ROUND(AVG(pnl_r), 4) as avg_pnl_r
           FROM trades WHERE exit_ts IS NOT NULL""",
    ).fetchone()

    per_strategy = conn.execute(
        """SELECT strategy,
                  COUNT(*) as trades,
                  ROUND(SUM(pnl_usd), 4) as pnl_usd,
                  ROUND(SUM(pnl_r), 4) as pnl_r,
                  ROUND(AVG(pnl_r), 4) as avg_r
           FROM trades WHERE exit_ts IS NOT NULL
           GROUP BY strategy ORDER BY pnl_r DESC""",
    ).fetchall()
    conn.close()
    return jsonify({"summary": dict(row), "per_strategy": _rows_to_list(per_strategy)})


@app.route("/api/v2/governance")
def governance():
    _auth()
    signal_id = request.args.get("signal_id", type=int)
    limit     = min(int(request.args.get("limit", 50)), 500)

    conn = _get_ro_conn()
    if signal_id:
        rows = conn.execute(
            """SELECT id, signal_id, ts, decision, reason, checks_json
               FROM governance_log WHERE signal_id=? ORDER BY id DESC""",
            (signal_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, signal_id, ts, decision, reason, checks_json
               FROM governance_log ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    conn.close()

    result = []
    for r in rows:
        d = dict(r)
        if d.get("checks_json"):
            try:
                d["checks"] = json.loads(d["checks_json"])
                del d["checks_json"]
            except Exception:
                pass
        result.append(d)
    return jsonify({"count": len(result), "governance_log": result})


@app.route("/api/v2/state")
def state():
    _auth()
    conn = _get_ro_conn()
    rows = conn.execute("SELECT key, value, updated_at FROM system_state ORDER BY key").fetchall()
    conn.close()
    result = {}
    for r in rows:
        try:
            result[r[0]] = {"value": json.loads(r[1]), "updated_at": r[2]}
        except Exception:
            result[r[0]] = {"value": r[1], "updated_at": r[2]}
    return jsonify(result)


# ── POST/PUT/DELETE blockieren ─────────────────────────────────────────────────

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "read-only API — nur GET erlaubt"}), 405


if __name__ == "__main__":
    print(f"[API] APEX V2 Read-only API startet auf Port {API_PORT}")
    app.run(host="0.0.0.0", port=API_PORT, debug=False)
