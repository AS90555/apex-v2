"""
Primitive Indikator-Funktionen — zentralisiert aus V1 (vaa_bot, kdt_bot, autonomous_trade).
NumPy-Fast-Path verfügbar (v6 Phase 8): stdev, atr_wilder, bollinger_bands nutzen
numpy wenn vorhanden (>10× Speedup bei großen Serien), sonst Pure-Python-Fallback.
Alle Funktionen arbeiten auf rohen Candle-Listen: [{"time", "open", "high", "low", "close", "volume"}, ...]
"""

import math

try:
    import numpy as _np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


# ── Mittelwerte ───────────────────────────────────────────────────────────────

def sma(values: list[float], period: int) -> float:
    """Simple Moving Average der letzten `period` Werte."""
    if len(values) < period:
        return 0.0
    return sum(values[-period:]) / period


def ema(values: list[float], period: int) -> float:
    """
    Exponential Moving Average (Standard, k = 2/(period+1)).
    Warmup: SMA der ersten `period` Werte, dann EMA-Iteration.
    Benötigt mindestens `period` Werte.
    """
    if len(values) < period:
        return 0.0
    k   = 2 / (period + 1)
    val = sum(values[:period]) / period
    for v in values[period:]:
        val = v * k + val * (1 - k)
    return val


def stdev(values: list[float], period: int) -> float:
    """Standardabweichung (Bessel-Korrektur, ddof=1) der letzten `period` Werte."""
    if len(values) < period or period < 2:
        return 0.0
    if _HAS_NUMPY:
        return float(_np.std(values[-period:], ddof=1))
    subset   = values[-period:]
    mean_val = sum(subset) / period
    variance = sum((x - mean_val) ** 2 for x in subset) / (period - 1)
    return math.sqrt(variance)


# ── Volatilität ───────────────────────────────────────────────────────────────

def atr_wilder(candles: list[dict], period: int = 14) -> float:
    """
    Average True Range mit Wilder's Smoothing (RMA).
    TR(i) = max(H-L, |H-PC|, |L-PC|)
    Warmup: SMA der ersten `period` TRs, dann RMA-Iteration.
    Benötigt mindestens `period + 1` Candles.
    """
    if len(candles) < period + 1:
        return 0.0
    if _HAS_NUMPY:
        highs  = _np.array([c["high"]  for c in candles], dtype=float)
        lows   = _np.array([c["low"]   for c in candles], dtype=float)
        closes = _np.array([c["close"] for c in candles], dtype=float)
        tr = _np.maximum(
            highs[1:] - lows[1:],
            _np.maximum(
                _np.abs(highs[1:] - closes[:-1]),
                _np.abs(lows[1:]  - closes[:-1]),
            ),
        )
        if len(tr) < period:
            return 0.0
        atr_val = float(tr[:period].mean())
        for val in tr[period:]:
            atr_val = (atr_val * (period - 1) + float(val)) / period
        return atr_val
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return 0.0
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val


def atr_sma(candles: list[dict], atr_period: int = 14, sma_period: int = 20) -> float:
    """
    SMA über eine Serie von ATR-Werten (rolling ATR → dann SMA).
    Wird für VAA ATR-Expansion-Filter genutzt (atr14 / atr_sma20 > 1.2).
    Benötigt mindestens atr_period + sma_period + 1 Candles für saubere Werte.
    """
    atr_vals = []
    for i in range(atr_period + 1, len(candles) + 1):
        atr_vals.append(atr_wilder(candles[:i], atr_period))
    return sma(atr_vals, sma_period) if len(atr_vals) >= sma_period else (atr_vals[-1] if atr_vals else 0.0)


# ── Bollinger & Keltner (für Squeeze-Detektion) ───────────────────────────────

def bollinger_bands(closes: list[float], period: int = 20, mult: float = 2.0) -> tuple[float, float, float]:
    """Gibt (upper, mid, lower) zurück. mid = SMA(period)."""
    mid   = sma(closes, period)
    std   = stdev(closes, period)
    return mid + mult * std, mid, mid - mult * std


def keltner_channels(candles: list[dict], period: int = 20, mult: float = 1.5) -> tuple[float, float, float]:
    """Gibt (upper, mid, lower) zurück. mid = SMA(close, period)."""
    closes = [c["close"] for c in candles]
    mid    = sma(closes, period)
    atr    = atr_wilder(candles, period)
    return mid + mult * atr, mid, mid - mult * atr


def is_squeeze(candles: list[dict], period: int = 20) -> bool:
    """
    TTM Squeeze: BB(20, 2σ) vollständig innerhalb KC(20, 1.5×ATR).
    True = Volatilitäts-Kontraktion (bevorstehender Ausbruch).
    """
    closes       = [c["close"] for c in candles]
    upper_bb, _, lower_bb = bollinger_bands(closes, period, 2.0)
    upper_kc, _, lower_kc = keltner_channels(candles, period, 1.5)
    return (lower_bb > lower_kc) and (upper_bb < upper_kc)


# ── Volume ────────────────────────────────────────────────────────────────────

def vol_sma(candles: list[dict], period: int) -> float:
    """SMA des Volumens über die letzten `period` Candles."""
    volumes = [c["volume"] for c in candles]
    return sma(volumes, period)


def body_sma(candles: list[dict], period: int) -> float:
    """SMA der Kerzenkörper (|close - open|) über `period` Candles."""
    bodies = [abs(c["close"] - c["open"]) for c in candles]
    return sma(bodies, period)


def rsi(candles: list[dict], period: int = 14) -> float:
    """
    Wilder's RSI. Braucht mindestens period+1 Candles.
    Gibt 50.0 zurück wenn zu wenige Daten vorhanden.
    """
    closes = [c["close"] for c in candles]
    if len(closes) < period + 1:
        return 50.0

    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ── Market-Regime-Detection ──────────────────────────────────────────────────
#
# Dual-Filter — identisch zur Lab-Logik (auto_lab_daemon.py):
#   1. EMA(50)-Slope über 10 Bars: (EMA_now - EMA_10ago) / EMA_10ago
#      > +0.3% → bullisch / < -0.3% → bearisch
#   2. Volatilitäts-Ratio: ATR(14) / SMA(Close, 50) > 1.5% → Markt bewegt sich
#
# Kombination:
#   slope > thresh  AND  vol_ratio > 0.015  → TREND_UP
#   slope < -thresh AND  vol_ratio > 0.015  → TREND_DOWN
#   sonst                                   → SIDEWAYS

REGIME_EMA_PERIOD   = 50
REGIME_SLOPE_PCT    = 0.003   # ±0.3%
REGIME_VOL_RATIO    = 0.015   # ATR/SMA > 1.5% = Trend gültig
_MIN_CANDLES        = REGIME_EMA_PERIOD + 15


def detect_regime(candles: list[dict]) -> str:
    """
    Berechnet das Markt-Regime aus 1h-Candles.
    Gibt 'TREND_UP', 'TREND_DOWN' oder 'SIDEWAYS' zurück.
    Mindestens 65 Candles erforderlich (EMA_50 + 15 Puffer).
    """
    if len(candles) < _MIN_CANDLES:
        return "UNKNOWN"

    closes    = [c["close"] for c in candles]
    ema_now   = ema(closes, REGIME_EMA_PERIOD)
    ema_10ago = ema(closes[:-10], REGIME_EMA_PERIOD)

    if ema_10ago == 0:
        return "UNKNOWN"

    slope     = (ema_now - ema_10ago) / ema_10ago
    atr_val   = atr_wilder(candles[-28:], 14)
    sma_close = sma(closes[-50:], 50)
    vol_ratio = atr_val / sma_close if sma_close > 0 else 0.0
    trending  = vol_ratio > REGIME_VOL_RATIO

    if slope > REGIME_SLOPE_PCT and trending:
        return "TREND_UP"
    if slope < -REGIME_SLOPE_PCT and trending:
        return "TREND_DOWN"
    return "SIDEWAYS"
