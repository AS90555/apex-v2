"""
HMM Regime-Detector Training — APEX V2 (P-02).

Trainiert einen 3-State Gaussian HMM auf 1h-Candles und speichert
Modell + Scaler als Pickle unter data/hmm_models/{asset}_hmm_py312.pkl.
"""
from __future__ import annotations

import os
import pickle
import sqlite3
import time

import numpy as np
from hmmlearn import hmm
from sklearn.preprocessing import StandardScaler

_MODEL_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "hmm_models")


# ── Feature-Berechnung ────────────────────────────────────────────────────────

def load_features(asset: str, conn: sqlite3.Connection, lookback_days: int = 180) -> np.ndarray:
    """
    Liest 1h-Candles für asset aus der DB und gibt Feature-Matrix Shape (T, 4) zurück.

    Features:
      [0] log_return_1h  — log(close / close_prev)
      [1] log_return_4h  — log(close_4h / close_4h_prev), via jede 4. Bar
      [2] atr_ratio      — ATR(14) / close
      [3] volume_ratio   — volume / volume.rolling(20).mean()
    """
    cutoff_ms = int((time.time() - lookback_days * 86400) * 1000)
    rows = conn.execute(
        """
        SELECT ts, open, high, low, close, volume
        FROM candles
        WHERE asset = ? AND interval = '1h' AND ts >= ?
        ORDER BY ts ASC
        """,
        (asset, cutoff_ms),
    ).fetchall()

    if len(rows) < 30:
        raise ValueError(f"Zu wenige 1h-Candles für {asset}: {len(rows)} < 30")

    ts    = np.array([r[0] for r in rows], dtype=np.float64)
    opens = np.array([r[1] for r in rows], dtype=np.float64)
    highs = np.array([r[2] for r in rows], dtype=np.float64)
    lows  = np.array([r[3] for r in rows], dtype=np.float64)
    close = np.array([r[4] for r in rows], dtype=np.float64)
    vol   = np.array([r[5] for r in rows], dtype=np.float64)

    # log_return_1h
    lr_1h = np.log(close[1:] / close[:-1])

    # log_return_4h — jede 4. Bar resamplen
    close_4h = close[::4]
    lr_4h_raw = np.log(close_4h[1:] / close_4h[:-1])
    # auf 1h-Raster expandieren: jede 4 Bars bekommt denselben 4h-Return
    lr_4h = np.repeat(lr_4h_raw, 4)[: len(lr_1h)]
    # Rest auffüllen falls nicht durch 4 teilbar
    if len(lr_4h) < len(lr_1h):
        lr_4h = np.pad(lr_4h, (0, len(lr_1h) - len(lr_4h)), mode="edge")

    # ATR(14)
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - close[:-1]),
            np.abs(lows[1:] - close[:-1]),
        ),
    )
    atr = np.full(len(tr), np.nan)
    atr[13] = tr[:14].mean()
    for i in range(14, len(tr)):
        atr[i] = (atr[i - 1] * 13 + tr[i]) / 14
    atr_ratio = atr / close[1:]

    # volume_ratio
    vol_1 = vol[1:]
    vol_mean20 = np.array([
        vol_1[max(0, i - 19) : i + 1].mean() for i in range(len(vol_1))
    ])
    vol_mean20 = np.where(vol_mean20 == 0, 1.0, vol_mean20)
    volume_ratio = vol_1 / vol_mean20

    # Alignment: erste 14 Bars für ATR ungültig → wegschneiden
    valid_start = 14
    X = np.column_stack([
        lr_1h[valid_start:],
        lr_4h[valid_start:],
        atr_ratio[valid_start:],
        volume_ratio[valid_start:],
    ])

    # NaN/Inf bereinigen
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    return X


# ── Training ──────────────────────────────────────────────────────────────────

def train_hmm(asset: str, conn: sqlite3.Connection, lookback_days: int = 180) -> tuple:
    """
    Trainiert GaussianHMM(n_components=3) auf den Features von load_features().
    Versucht covariance_type='full', Fallback auf 'diag' bei Nicht-Konvergenz.
    Gibt (model, scaler) zurück.
    Raises RuntimeError wenn auch 'diag' nicht konvergiert.
    """
    X_raw = load_features(asset, conn, lookback_days=lookback_days)

    scaler = StandardScaler()
    X = scaler.fit_transform(X_raw)

    lengths = [len(X)]  # ein zusammenhängender Datenstrom

    # n_init via manuelles Multi-Start (hmmlearn 0.3 hat kein n_init-Argument)
    for cov_type in ("full", "diag"):
        best_model: hmm.GaussianHMM | None = None
        best_score = -np.inf
        for seed in range(10):
            m = hmm.GaussianHMM(
                n_components=3,
                covariance_type=cov_type,
                n_iter=200,
                random_state=seed,
                tol=1e-4,
            )
            m.fit(X, lengths=lengths)
            if not m.monitor_.converged:
                continue
            try:
                score = m.score(X, lengths=lengths)
            except Exception:
                continue
            if score > best_score:
                best_score = score
                best_model = m
        if best_model is not None:
            return best_model, scaler

    raise RuntimeError(
        f"GaussianHMM konvergierte nicht für {asset} (weder 'full' noch 'diag', 10 Seeds)"
    )


# ── State-Labeling ────────────────────────────────────────────────────────────

def label_states(model: hmm.GaussianHMM) -> dict[int, str]:
    """
    Weist jedem der 3 HMM-States einen semantischen Namen zu.

    Logik (auf den rohen, nicht-skalierten Means — aber da Scaler
    monotone Transformation ist, bleibt die Rangordnung erhalten):
      HIGH_VOL → State mit höchstem atr_ratio-Mean (Feature-Index 2)
      TREND    → verbleibender State mit höchstem log_return_1h-Mean (Index 0)
      SIDEWAYS → verbleibender State
    """
    means = model.means_  # Shape (3, 4)
    states = list(range(3))

    high_vol_state = int(np.argmax(means[:, 2]))
    remaining = [s for s in states if s != high_vol_state]
    trend_state = remaining[int(np.argmax(means[remaining, 0]))]
    sideways_state = [s for s in remaining if s != trend_state][0]

    return {
        high_vol_state: "HIGH_VOL",
        trend_state:    "TREND",
        sideways_state: "SIDEWAYS",
    }


# ── Persistenz ────────────────────────────────────────────────────────────────

def save_model(
    asset: str,
    model: hmm.GaussianHMM,
    scaler: StandardScaler,
    path: str = _MODEL_DIR,
) -> str:
    """Speichert model + scaler als Pickle, gibt Dateipfad zurück."""
    os.makedirs(path, exist_ok=True)
    fpath = os.path.join(path, f"{asset}_hmm_py312.pkl")
    with open(fpath, "wb") as f:
        pickle.dump({"model": model, "scaler": scaler}, f)
    return fpath


def load_model(asset: str, path: str = _MODEL_DIR) -> dict:
    """Lädt und gibt das gespeicherte Pickle-Dict zurück."""
    fpath = os.path.join(path, f"{asset}_hmm_py312.pkl")
    with open(fpath, "rb") as f:
        return pickle.load(f)


# ── Inference ─────────────────────────────────────────────────────────────────

def get_current_regime(asset: str, conn: sqlite3.Connection) -> str:
    """
    Gibt das aktuelle Regime für asset zurück: TREND / SIDEWAYS / HIGH_VOL.
    Liest die letzten 5 Tage Features und wendet das gespeicherte Modell an.
    """
    obj = load_model(asset)
    X_raw = load_features(asset, conn, lookback_days=5)
    X = obj["scaler"].transform(X_raw)
    states = obj["model"].predict(X)
    labels = label_states(obj["model"])
    return labels[int(states[-1])]
