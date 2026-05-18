"""
P3.1 — Subprocess-Timeout in run_v7_reeval / promote_if_eligible.

Verifiziert:
- Hängender Re-Eval (simuliert via sleep > Timeout) → attempt.error='reeval_timeout'
- kein _save_discovery-Aufruf bei Timeout
- Dispatcher wird genau einmal aufgerufen mit Hinweis auf Strategie/Asset
- Dispatcher-Fehler blockiert nicht den Rückgabepfad (attempt wird trotzdem zurückgegeben)
- Erfolgreicher Re-Eval (kein Timeout) → Timeout-Pfad nie betreten, _save_discovery aufgerufen
- Timeout-Schwelle aus REEVAL_TIMEOUT_SEC — kein Hardcoding
"""
from __future__ import annotations

import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ══════════════════════════════════════════════════════════════════════════════
# Hilfsfunktionen
# ══════════════════════════════════════════════════════════════════════════════

def _make_reeval_result(passed: bool = True) -> MagicMock:
    r = MagicMock()
    r.passed = passed
    r.fail_reasons = [] if passed else ["dsr_below_min"]
    r.dsr_oos = 0.65 if passed else 0.30
    r.pbo_val = 0.15
    r.stability = 0.70
    r.max_dd = 2.0
    r.composite = 0.80
    r.weights_hash = "abc123"
    r.n_oos = 120
    r.oos_folds_n = 4
    r.params_json = "{}"
    r.strategy = "donchian_breakout"
    r.asset = "BTC"
    return r


def _run_promote(strategy: str = "donchian_breakout",
                 asset: str = "BTC",
                 fitness: float = 0.75,
                 variant_id: str = "variant-test-uuid-0001",
                 run_one_side_effect=None,
                 save_discovery_mock=None,
                 timeout_sec: int = 1):
    """
    Ruft promote_if_eligible() auf mit kurzem Timeout und gemocktem run_one.
    Gibt (attempt, dispatch_mock) zurück.
    """
    import core.lab_promotion_gate as gate_mod

    mock_conn = MagicMock()
    mock_conn.execute.return_value = MagicMock(fetchall=MagicMock(return_value=[]))

    if save_discovery_mock is None:
        save_discovery_mock = MagicMock(return_value=1)

    with patch.object(gate_mod, "REEVAL_TIMEOUT_SEC", timeout_sec), \
         patch("scripts.run_v7_reeval.run_one", side_effect=run_one_side_effect), \
         patch("scripts.run_v7_reeval._save_discovery", save_discovery_mock), \
         patch("core.lab_promotion_gate._log_promotion_event"), \
         patch("core.lab_negative_controls.check_negative_control",
               return_value=MagicMock(blocked=False)), \
         patch("core.db.get_connection", return_value=mock_conn), \
         patch("core.telegram_dispatcher.dispatch") as mock_dispatch:
        attempt = gate_mod.promote_if_eligible(
            conn=mock_conn,
            variant_id=variant_id,
            strategy=strategy,
            asset=asset,
            fitness_score=fitness,
        )

    return attempt, mock_dispatch, save_discovery_mock


# ══════════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestReevalTimeout:
    def test_timeout_sets_error_reeval_timeout(self):
        """Hängender run_one → attempt.error='reeval_timeout'."""
        def _hang(*args, **kwargs):
            time.sleep(5)  # deutlich länger als timeout_sec=1
            return _make_reeval_result()

        attempt, _, _ = _run_promote(run_one_side_effect=_hang, timeout_sec=1)

        assert attempt.error == "reeval_timeout", \
            f"Erwartet 'reeval_timeout', erhalten: {attempt.error!r}"
        assert attempt.triggered is True  # wurde getriggert, aber timed out

    def test_timeout_no_save_discovery(self):
        """Bei Timeout: _save_discovery wird NICHT aufgerufen."""
        def _hang(*args, **kwargs):
            time.sleep(5)
            return _make_reeval_result()

        save_mock = MagicMock(return_value=1)
        attempt, _, save_mock = _run_promote(
            run_one_side_effect=_hang,
            save_discovery_mock=save_mock,
            timeout_sec=1,
        )

        assert attempt.error == "reeval_timeout"
        save_mock.assert_not_called()

    def test_timeout_dispatches_alert_once(self):
        """Bei Timeout: Dispatcher wird genau einmal aufgerufen."""
        def _hang(*args, **kwargs):
            time.sleep(5)
            return _make_reeval_result()

        attempt, mock_dispatch, _ = _run_promote(
            run_one_side_effect=_hang,
            timeout_sec=1,
        )

        assert attempt.error == "reeval_timeout"
        mock_dispatch.assert_called_once()

    def test_timeout_alert_contains_strategy_and_asset(self):
        """Timeout-Alert enthält strategy/asset zur Identifikation."""
        def _hang(*args, **kwargs):
            time.sleep(5)
            return _make_reeval_result()

        attempt, mock_dispatch, _ = _run_promote(
            strategy="donchian_breakout",
            asset="ETH",
            run_one_side_effect=_hang,
            timeout_sec=1,
        )

        assert attempt.error == "reeval_timeout"
        alert_text = mock_dispatch.call_args[0][0]
        assert "donchian_breakout" in alert_text
        assert "ETH" in alert_text
        assert "timeout" in alert_text.lower() or "TIMEOUT" in alert_text

    def test_dispatcher_failure_does_not_block_return(self):
        """Telegram-Fehler im Timeout-Pfad → attempt trotzdem zurückgegeben."""
        def _hang(*args, **kwargs):
            time.sleep(5)
            return _make_reeval_result()

        import core.lab_promotion_gate as gate_mod
        mock_conn = MagicMock()
        mock_conn.execute.return_value = MagicMock(fetchall=MagicMock(return_value=[]))

        with patch.object(gate_mod, "REEVAL_TIMEOUT_SEC", 1), \
             patch("scripts.run_v7_reeval.run_one", side_effect=_hang), \
             patch("scripts.run_v7_reeval._save_discovery", MagicMock()), \
             patch("core.lab_negative_controls.check_negative_control",
                   return_value=MagicMock(blocked=False)), \
             patch("core.db.get_connection", return_value=mock_conn), \
             patch("core.telegram_dispatcher.dispatch",
                   side_effect=RuntimeError("Telegram down")):
            attempt = gate_mod.promote_if_eligible(
                conn=mock_conn,
                variant_id="variant-disp-fail",
                strategy="donchian_breakout",
                asset="BTC",
                fitness_score=0.75,
            )

        assert attempt.error == "reeval_timeout", \
            "Dispatcher-Fehler darf Rückgabe nicht blockieren"

    def test_success_no_timeout_path_save_discovery_called(self):
        """Erfolgreicher Re-Eval → kein Timeout-Pfad, _save_discovery wird aufgerufen."""
        result = _make_reeval_result(passed=True)

        def _fast(*args, **kwargs):
            return result

        save_mock = MagicMock(return_value=1)
        attempt, mock_dispatch, save_mock = _run_promote(
            run_one_side_effect=_fast,
            save_discovery_mock=save_mock,
            timeout_sec=30,  # langer Timeout → kein Ablauf
        )

        assert attempt.error is None, f"Kein Fehler erwartet, erhalten: {attempt.error}"
        assert attempt.triggered is True
        save_mock.assert_called_once()
        mock_dispatch.assert_not_called()

    def test_below_fitness_threshold_no_reeval_no_dispatch(self):
        """Fitness unter Schwelle → kein Re-Eval, kein Dispatch, kein Error."""
        save_mock = MagicMock()
        attempt, mock_dispatch, save_mock = _run_promote(
            fitness=0.30,  # unter FITNESS_PROMOTION_THRESHOLD=0.60
            run_one_side_effect=lambda *a, **k: _make_reeval_result(),
            save_discovery_mock=save_mock,
            timeout_sec=30,
        )

        assert attempt.triggered is False
        assert attempt.error is None
        save_mock.assert_not_called()
        mock_dispatch.assert_not_called()

    def test_timeout_threshold_from_constant(self):
        """Timeout-Schwelle kommt aus REEVAL_TIMEOUT_SEC, kein Hardcoding."""
        import core.lab_promotion_gate as gate_mod
        assert hasattr(gate_mod, "REEVAL_TIMEOUT_SEC"), \
            "REEVAL_TIMEOUT_SEC muss als Modul-Konstante exportiert sein"
        assert gate_mod.REEVAL_TIMEOUT_SEC > 0
        assert isinstance(gate_mod.REEVAL_TIMEOUT_SEC, (int, float))
