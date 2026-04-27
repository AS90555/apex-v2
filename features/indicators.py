"""
Primitive Indikator-Funktionen — zentralisiert aus V1 (vaa_bot, kdt_bot, autonomous_trade).
Pure Python, kein numpy/pandas.
Alle Funktionen arbeiten auf rohen Candle-Listen: [{"time", "open", "high", "low", "close", "volume"}, ...]
"""

import math


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
    """Standardabweichung (Populationsformel) der letzten `period` Werte."""
    if len(values) < period:
        return 0.0
    subset   = values[-period:]
    mean_val = sum(subset) / period
    variance = sum((x - mean_val) ** 2 for x in subset) / period
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
