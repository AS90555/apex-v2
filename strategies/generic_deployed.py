"""
GenericDeployedStrategy — universeller Live-Signal-Generator für Lab-Discoveries.

Lädt jeden `base_strategy`-Typ aus active_deployments und nutzt die
Backtest-Engine-Funktionen (SIGNAL_FNS) direkt für Live-Signale.

Damit werden alle aktuellen und zukünftigen Lab-Strategien automatisch
unterstützt — keine separate Strategy-Datei pro Typ nötig.
"""

import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_connection
from core.models import Signal
from core.utils import log, now_iso
from strategies.base import BaseStrategy
from config.settings import RISK_USDT, SIZE_DECIMALS, PRICE_DECIMALS, V7_FUNDING_SIZING
from governance.sizing import get_latest_funding_rate


def _current_session() -> str:
    """Gibt aktuelle Trading-Session basierend auf UTC-Stunde zurück."""
    from datetime import datetime, timezone
    h = datetime.now(timezone.utc).hour
    if h < 8:
        return "asia"
    if h < 16:
        return "europe"
    return "us"


def _make_signal_key(strategy_key: str, asset: str, mode: str) -> str:
    from datetime import datetime, timezone
    bucket = datetime.now(timezone.utc).strftime("%Y%m%d")
    session = _current_session()
    return f"{strategy_key}__{asset}__{session}__{mode}__{bucket}"


def _save_signal(conn, signal: Signal, strategy_key: str) -> int:
    signal_key = _make_signal_key(strategy_key, signal.asset, signal.mode)
    cur = conn.execute(
        """INSERT OR IGNORE INTO signals
           (created_at, strategy, asset, direction, entry_price, stop_loss,
            take_profit_1, take_profit_2, size, risk_usd, session, status, mode, signal_key)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (signal.created_at, signal.strategy, signal.asset, signal.direction,
         signal.entry_price, signal.stop_loss, signal.take_profit_1, signal.take_profit_2,
         signal.size, signal.risk_usd, signal.session, signal.status, signal.mode, signal_key),
    )
    conn.commit()
    return cur.lastrowid


def load_deployed_strategies() -> list["GenericDeployedStrategy"]:
    """
    Lädt alle aktiven Deployments aus active_deployments.
    Überspringt shadow-Mode und unbekannte base_strategy-Typen (kein SIGNAL_FN-Eintrag).
    Gibt eine Liste fertiger GenericDeployedStrategy-Instanzen zurück.
    """
    import json
    from backtest.engine import SIGNAL_FNS

    conn = get_connection()
    rows = conn.execute(
        """SELECT id, discovery_id, strategy_key, base_strategy, asset, params_json, mode
           FROM active_deployments WHERE active=1"""
    ).fetchall()
    conn.close()

    result = []
    for row in rows:
        mode = row["mode"]
        if mode not in ("live", "dry_run"):
            continue  # shadow-Deployments werden nicht live ausgeführt

        base = row["base_strategy"]
        if base not in SIGNAL_FNS:
            log(f"[GENERIC] base_strategy='{base}' nicht in SIGNAL_FNS — überspringe #{row['id']}")
            continue

        params = json.loads(row["params_json"] or "{}")
        result.append(GenericDeployedStrategy(
            base_strategy=base,
            strategy_key=row["strategy_key"],
            asset=row["asset"],
            params=params,
            mode=mode,
            discovery_id=row["discovery_id"],
        ))
    return result


class GenericDeployedStrategy(BaseStrategy):
    """
    Führt eine einzelne Lab-Discovery als Live-Signal-Generator aus.
    Nutzt SIGNAL_FNS[base_strategy] direkt — dieselbe Logik wie im Backtest.
    """

    def __init__(self, base_strategy: str, strategy_key: str, asset: str,
                 params: dict, mode: str, discovery_id: int):
        self._base      = base_strategy
        self._key       = strategy_key   # z.B. "donchian_breakout_1170"
        self._asset     = asset
        self._params    = params
        self._mode      = mode
        self._disc_id   = discovery_id

    @property
    def name(self) -> str:
        return self._key

    @property
    def assets(self) -> list[str]:
        return [self._asset]

    def generate_signals(self) -> list[Signal]:
        from backtest.engine import SIGNAL_FNS

        signal_fn = SIGNAL_FNS.get(self._base)
        if signal_fn is None:
            log(f"[{self._key}] Kein SIGNAL_FN für '{self._base}' — überspringe")
            return []

        # Immer auf der letzten GESCHLOSSENEN Kerze evaluieren.
        # Die aktuell formende Kerze hat Volume≈0 → vol_ok schlägt strukturell fehl.
        CANDLE_MS    = 3_600_000  # 1h in ms
        now_ms       = int(time.time() * 1000)
        candle_open  = (now_ms // CANDLE_MS) * CANDLE_MS   # Beginn der laufenden Kerze
        as_of_ts     = candle_open - 1                     # 1ms davor → letzte geschlossene Kerze

        conn = get_connection()
        try:
            bt_sig = signal_fn(conn, self._asset, as_of_ts, self._params)
        except Exception as e:
            log(f"[{self._key}] Signal-Fehler für {self._asset}: {e}")
            conn.close()
            return []

        if bt_sig is None:
            conn.close()
            return []

        dec_size  = SIZE_DECIMALS.get(self._asset, 2)
        dec_price = PRICE_DECIMALS.get(self._asset, 2)

        # Fix B: entry_price == Signal-Close — identisch zum Backtest, parity_test-kompatibel
        entry_price = bt_sig.entry_price

        # Fix A: Zeitfilter auf geschlossene Kerzen (ts < candle_open)
        # Fix C: Marktpreis nur für Drift-Monitoring, nicht als Entry-Override
        cur_row = conn.execute(
            "SELECT close FROM candles WHERE asset=? AND interval='1h' AND ts < ? ORDER BY ts DESC LIMIT 1",
            (self._asset, candle_open),
        ).fetchone()
        if cur_row and cur_row[0] and cur_row[0] > 0:
            market_price = round(cur_row[0], dec_price)
            drift_pct = abs(market_price - entry_price) / entry_price * 100
            if drift_pct > 0.5:
                log(f"[{self._key}] DRIFT {drift_pct:.2f}%: signal={entry_price} market={market_price}")

        # SL/TP-Distanzen relativ zum Signal-Close — bit-identisch zum Backtest
        sl_dist_orig = abs(bt_sig.entry_price - bt_sig.stop_loss)
        tp1_dist     = abs(bt_sig.entry_price - bt_sig.take_profit_1)
        tp2_dist     = abs(bt_sig.entry_price - bt_sig.take_profit_2)

        if sl_dist_orig <= 0:
            conn.close()
            return []

        direction = bt_sig.direction
        if direction == "long":
            stop_loss     = round(entry_price - sl_dist_orig, dec_price)
            take_profit_1 = round(entry_price + tp1_dist, dec_price)
            take_profit_2 = round(entry_price + tp2_dist, dec_price)
        else:
            stop_loss     = round(entry_price + sl_dist_orig, dec_price)
            take_profit_1 = round(entry_price - tp1_dist, dec_price)
            take_profit_2 = round(entry_price - tp2_dist, dec_price)

        sl_dist = abs(entry_price - stop_loss)

        if V7_FUNDING_SIZING and sl_dist > 0:
            from governance.sizing import compute_position_size
            funding_rate = get_latest_funding_rate(self._asset)
            size = round(
                compute_position_size(
                    asset=self._asset,
                    entry_price=entry_price,
                    sl_distance=sl_dist,
                    capital=RISK_USDT * 20,  # Proxy-Kapital; RISK_USDT bleibt Cap
                    expected_funding_8h=funding_rate,
                ),
                dec_size,
            )
        else:
            size = round(RISK_USDT / sl_dist, dec_size) if sl_dist > 0 else 0

        if size <= 0:
            conn.close()
            return []

        sig = Signal(
            created_at=now_iso(),
            strategy=self._key,
            asset=self._asset,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit_1=take_profit_1,
            take_profit_2=take_profit_2,
            size=size,
            risk_usd=round(RISK_USDT, 4),
            session=_current_session(),
            status="pending",
            mode=self._mode,
        )

        row_id = _save_signal(conn, sig, self._key)
        conn.close()

        if row_id == 0:
            log(f"[{self._key}] {self._asset}: Signal heute bereits vorhanden — überspringe (Dedup)")
            return []

        log(f"[{self._key}] {self._asset} {sig.direction.upper()} @ {sig.entry_price} "
            f"SL={sig.stop_loss} TP={sig.take_profit_1} size={size} mode={self._mode}")
        return [sig]
