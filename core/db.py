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
    source     TEXT NOT NULL DEFAULT 'bitget',
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
    -- status lifecycle: pending → approved_shadow (shadow, Audit only) | approved → processing → executed|failed | rejected | expired
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

CREATE TABLE IF NOT EXISTS research_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    lab_session   TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    asset         TEXT NOT NULL,
    params_json   TEXT NOT NULL,
    n_train       INTEGER, total_r_train REAL, avg_r_train REAL, pf_train REAL, wr_train REAL,
    n_test        INTEGER, total_r_test  REAL, avg_r_test  REAL, pf_test  REAL, wr_test  REAL,
    fitness_score REAL,
    passed        INTEGER NOT NULL DEFAULT 0,
    reject_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_research_session ON research_runs(lab_session, passed);
CREATE INDEX IF NOT EXISTS idx_research_fitness ON research_runs(strategy, asset, fitness_score DESC);

CREATE TABLE IF NOT EXISTS active_deployments (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    discovery_id       INTEGER NOT NULL,
    strategy_key       TEXT NOT NULL UNIQUE,   -- z.B. "squeeze_42"
    base_strategy      TEXT NOT NULL,          -- "squeeze"
    asset              TEXT NOT NULL,
    market_regime      TEXT,
    params_json        TEXT NOT NULL,
    mode               TEXT NOT NULL DEFAULT 'dry_run',
    deployed_at        TEXT NOT NULL,
    active             INTEGER NOT NULL DEFAULT 1,
    target_trades      INTEGER NOT NULL DEFAULT 50,
    go_live_notified   INTEGER NOT NULL DEFAULT 0,
    note               TEXT
);

CREATE TABLE IF NOT EXISTS asset_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    asset        TEXT NOT NULL UNIQUE,
    requested_at TEXT NOT NULL,
    requested_by TEXT NOT NULL DEFAULT 'telegram',
    status       TEXT NOT NULL DEFAULT 'pending',
    -- pending → lab picks it up next cycle
    -- in_progress → lab is currently testing it
    -- done → at least one research_run exists for this asset
    note         TEXT
);

CREATE TABLE IF NOT EXISTS lab_window_results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    discovery_id INTEGER NOT NULL REFERENCES lab_discoveries(id),
    window_idx   INTEGER NOT NULL,   -- 0=alt (480d), 1=mittel (240d), 2=aktuell (60d)
    period_start INTEGER NOT NULL,   -- Unix-ms (Anfang des OOS-Fensters)
    period_end   INTEGER NOT NULL,   -- Unix-ms (Ende des OOS-Fensters)
    n_train      INTEGER,
    pf_train     REAL,
    avg_r_train  REAL,
    n_test       INTEGER,
    pf_test      REAL,
    avg_r_test   REAL,
    wr_test      REAL,
    max_dd_r     REAL,
    passed       INTEGER NOT NULL DEFAULT 0,
    UNIQUE(discovery_id, window_idx)
);

CREATE INDEX IF NOT EXISTS idx_wres_discovery ON lab_window_results(discovery_id);
CREATE INDEX IF NOT EXISTS idx_wres_window    ON lab_window_results(window_idx, passed);

CREATE TABLE IF NOT EXISTS live_vs_backtest_drift (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at      TEXT    NOT NULL,
    deployment_id   INTEGER NOT NULL REFERENCES active_deployments(id),
    strategy_key    TEXT    NOT NULL,
    asset           TEXT    NOT NULL,
    mode            TEXT    NOT NULL,
    n_live          INTEGER NOT NULL,
    pf_live         REAL,
    pf_oos          REAL    NOT NULL,
    drift_pct       REAL,
    status          TEXT    NOT NULL DEFAULT 'ok',
    action_taken    TEXT
);

CREATE INDEX IF NOT EXISTS idx_drift_deployment ON live_vs_backtest_drift(deployment_id, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_drift_status     ON live_vs_backtest_drift(status, checked_at DESC);
"""


def get_connection() -> sqlite3.Connection:
    db_path = os.path.abspath(DB_PATH)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
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
    if "tp1_partial_done" not in existing_cols:
        conn.execute("ALTER TABLE trades ADD COLUMN tp1_partial_done INTEGER DEFAULT 0")
    candle_cols = {row[1] for row in conn.execute("PRAGMA table_info(candles)").fetchall()}
    if "source" not in candle_cols:
        conn.execute("ALTER TABLE candles ADD COLUMN source TEXT NOT NULL DEFAULT 'bitget'")
    dep_cols = {row[1] for row in conn.execute("PRAGMA table_info(active_deployments)").fetchall()}
    if "target_trades" not in dep_cols:
        conn.execute("ALTER TABLE active_deployments ADD COLUMN target_trades INTEGER NOT NULL DEFAULT 50")
    if "go_live_notified" not in dep_cols:
        conn.execute("ALTER TABLE active_deployments ADD COLUMN go_live_notified INTEGER NOT NULL DEFAULT 0")
    ld_cols = {row[1] for row in conn.execute("PRAGMA table_info(lab_discoveries)").fetchall()}
    if "cooldown_bars" not in ld_cols:
        conn.execute("ALTER TABLE lab_discoveries ADD COLUMN cooldown_bars INTEGER DEFAULT 0")
    if "pf_test_netto" not in ld_cols:
        conn.execute("ALTER TABLE lab_discoveries ADD COLUMN pf_test_netto REAL")
    sig_cols = {row[1] for row in conn.execute("PRAGMA table_info(signals)").fetchall()}
    if "signal_key" not in sig_cols:
        conn.execute("ALTER TABLE signals ADD COLUMN signal_key TEXT")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_signal_key
           ON signals(signal_key) WHERE signal_key IS NOT NULL"""
    )
    # asset_requests (additive — safe on existing DBs)
    req_cols = {row[1] for row in conn.execute("PRAGMA table_info(asset_requests)").fetchall()}
    if not req_cols:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS asset_requests (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                asset        TEXT NOT NULL UNIQUE,
                requested_at TEXT NOT NULL,
                requested_by TEXT NOT NULL DEFAULT 'telegram',
                status       TEXT NOT NULL DEFAULT 'pending',
                note         TEXT
            );
        """)
    # live_vs_backtest_drift — idempotent via DDL
    drift_cols = {row[1] for row in conn.execute("PRAGMA table_info(live_vs_backtest_drift)").fetchall()}
    if not drift_cols:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS live_vs_backtest_drift (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                checked_at      TEXT    NOT NULL,
                deployment_id   INTEGER NOT NULL REFERENCES active_deployments(id),
                strategy_key    TEXT    NOT NULL,
                asset           TEXT    NOT NULL,
                mode            TEXT    NOT NULL,
                n_live          INTEGER NOT NULL,
                pf_live         REAL,
                pf_oos          REAL    NOT NULL,
                drift_pct       REAL,
                status          TEXT    NOT NULL DEFAULT 'ok',
                action_taken    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_drift_deployment ON live_vs_backtest_drift(deployment_id, checked_at DESC);
            CREATE INDEX IF NOT EXISTS idx_drift_status     ON live_vs_backtest_drift(status, checked_at DESC);
        """)

    # lab_window_results — idempotent via DDL (CREATE TABLE IF NOT EXISTS)
    wres_cols = {row[1] for row in conn.execute("PRAGMA table_info(lab_window_results)").fetchall()}
    if not wres_cols:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS lab_window_results (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                discovery_id INTEGER NOT NULL,
                window_idx   INTEGER NOT NULL,
                period_start INTEGER NOT NULL,
                period_end   INTEGER NOT NULL,
                n_train      INTEGER,
                pf_train     REAL,
                avg_r_train  REAL,
                n_test       INTEGER,
                pf_test      REAL,
                avg_r_test   REAL,
                wr_test      REAL,
                max_dd_r     REAL,
                passed       INTEGER NOT NULL DEFAULT 0,
                UNIQUE(discovery_id, window_idx)
            );
            CREATE INDEX IF NOT EXISTS idx_wres_discovery ON lab_window_results(discovery_id);
            CREATE INDEX IF NOT EXISTS idx_wres_window    ON lab_window_results(window_idx, passed);
        """)
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
