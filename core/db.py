import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "apex_v2.db")

DDL = """
CREATE TABLE IF NOT EXISTS candles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    asset      TEXT    NOT NULL,
    interval   TEXT    NOT NULL,
    ts         INTEGER NOT NULL,
    open       REAL, high REAL, low REAL, close REAL, volume REAL,
    fetched_at TEXT NOT NULL,
    UNIQUE(asset, interval, ts)
);

CREATE TABLE IF NOT EXISTS features (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    asset        TEXT NOT NULL,
    interval     TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    feature_name TEXT NOT NULL,
    value        REAL,
    UNIQUE(asset, interval, ts, feature_name)
);

CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    asset         TEXT NOT NULL,
    direction     TEXT NOT NULL,
    entry_price   REAL,
    stop_loss     REAL,
    take_profit_1 REAL,
    take_profit_2 REAL,
    size          REAL,
    risk_usd      REAL,
    session       TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    reject_reason TEXT,
    governance_ts TEXT,
    execution_ts  TEXT,
    order_id      TEXT,
    mode          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id     INTEGER REFERENCES signals(id),
    strategy      TEXT NOT NULL,
    asset         TEXT NOT NULL,
    direction     TEXT NOT NULL,
    entry_price   REAL,
    entry_ts      TEXT,
    size          REAL,
    stop_loss     REAL,
    take_profit_1 REAL,
    take_profit_2 REAL,
    exit_price    REAL,
    exit_ts       TEXT,
    exit_reason   TEXT,
    pnl_usd       REAL,
    pnl_r         REAL,
    be_applied    INTEGER DEFAULT 0,
    order_id      TEXT,
    mode          TEXT NOT NULL,
    session       TEXT,
    context_json  TEXT
);

CREATE TABLE IF NOT EXISTS governance_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id   INTEGER REFERENCES signals(id),
    ts          TEXT NOT NULL,
    decision    TEXT NOT NULL,
    reason      TEXT,
    checks_json TEXT
);

CREATE TABLE IF NOT EXISTS heartbeats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    component   TEXT NOT NULL,
    status      TEXT NOT NULL,
    message     TEXT,
    latency_ms  REAL
);

CREATE TABLE IF NOT EXISTS opening_ranges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    asset      TEXT NOT NULL,
    session    TEXT NOT NULL,
    date       TEXT NOT NULL,
    high       REAL NOT NULL,
    low        REAL NOT NULL,
    open       REAL,
    close      REAL,
    ts         INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(asset, session, date)
);

CREATE TABLE IF NOT EXISTS system_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def get_connection() -> sqlite3.Connection:
    db_path = os.path.abspath(DB_PATH)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    return conn


def run_migrations():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = get_connection()
    conn.executescript(DDL)
    # Additive column migrations (ALTER TABLE IF NOT EXISTS not supported in older SQLite)
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    if "session" not in existing_cols:
        conn.execute("ALTER TABLE trades ADD COLUMN session TEXT")
    if "order_id" not in existing_cols:
        conn.execute("ALTER TABLE trades ADD COLUMN order_id TEXT")
    conn.commit()
    conn.close()


def get_state(key: str, default=None):
    conn = get_connection()
    row = conn.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_state(key: str, value: str):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    conn.execute(
        "INSERT INTO system_state(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now),
    )
    conn.commit()
    conn.close()
