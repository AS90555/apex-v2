"""
DDL für die Research-Staging-Sidecar-DB (research_staging.db).

Spiegelt die Lab-Tabellen der Haupt-DB. Research-Daemons (auto_lab_daemon)
schreiben Discoveries hier hinein. run_staging_sync.py promotet sie atomar
mit Integritätsprüfung in die Haupt-DB.
"""

STAGING_DDL = """
CREATE TABLE IF NOT EXISTS lab_discoveries (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    discovered_at               TEXT NOT NULL,
    params_hash                 TEXT NOT NULL UNIQUE,
    strategy                    TEXT NOT NULL,
    asset                       TEXT NOT NULL,
    params_json                 TEXT NOT NULL,
    n_train                     INTEGER,
    pf_train                    REAL,
    avg_r_train                 REAL,
    n_test                      INTEGER,
    pf_test                     REAL,
    avg_r_test                  REAL,
    wr_test                     REAL,
    fitness_score               REAL,
    notified                    INTEGER DEFAULT 0,
    market_regime               TEXT,
    max_dd_r                    REAL,
    micro_score                 REAL,
    deployment_status           TEXT DEFAULT 'lab',
    deployed_at                 TEXT,
    deployed_by                 TEXT,
    deploy_notes                TEXT,
    cooldown_bars               INTEGER DEFAULT 0,
    signals_per_week            REAL,
    cost_model_applied          INTEGER DEFAULT 0,
    pf_test_netto               REAL,
    dsr                         REAL,
    -- V6-Spalten
    framework_version           TEXT DEFAULT 'v1',
    dsr_value                   REAL,
    pbo_value                   REAL,
    max_drawdown                REAL,
    calmar_ratio                REAL,
    stability_score             REAL,
    composite_score             REAL,
    oos_folds_n                 INTEGER,
    re_evaluated_at             TEXT,
    backtest_slippage_assumption REAL,
    backtest_funding_model      TEXT DEFAULT 'static',
    intrabar_model              TEXT DEFAULT 'static',
    -- Staging-Status
    sync_status                 TEXT DEFAULT 'pending',
    -- pending → synced | rejected_integrity
    sync_attempted_at           TEXT,
    sync_reject_reason          TEXT
);

CREATE INDEX IF NOT EXISTS idx_staging_disc_strategy ON lab_discoveries(strategy, asset);
CREATE INDEX IF NOT EXISTS idx_staging_disc_sync      ON lab_discoveries(sync_status, discovered_at);

CREATE TABLE IF NOT EXISTS lab_window_results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    discovery_id INTEGER NOT NULL REFERENCES lab_discoveries(id),
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

CREATE INDEX IF NOT EXISTS idx_staging_wres_discovery ON lab_window_results(discovery_id);
"""
