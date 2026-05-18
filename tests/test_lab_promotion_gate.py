"""
Tests für core/lab_promotion_gate.py (Phase E8).

Prüft:
- Variant unter Schwelle → kein Trigger
- Variant über Schwelle + NC-Block → kein Trigger
- Variant über Schwelle + kein NC → Trigger + _save_discovery aufgerufen
- get_promotion_candidates liest korrekt aus DB
- Evolution-Report enthält Promotion-Kandidaten-Sektion
"""
from __future__ import annotations
import pytest
from unittest.mock import patch, MagicMock
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.lab_state_db import (
    get_lab_state_connection, write_variant, update_variant_status,
    write_fitness_record,
)
from core.lab_families import sync_to_db
from core.lab_promotion_gate import (
    promote_if_eligible, get_promotion_candidates,
    FITNESS_PROMOTION_THRESHOLD,
)


@pytest.fixture
def conn(tmp_path):
    db = str(tmp_path / "test.db")
    c = get_lab_state_connection(db)
    sync_to_db(c)
    yield c
    c.close()


@pytest.fixture
def db_path(tmp_path):
    db = str(tmp_path / "test.db")
    c = get_lab_state_connection(db)
    sync_to_db(c)
    c.close()
    return db


def _make_evaluated_variant(conn, variant_id: str, strategy: str = "donchian_breakout",
                             asset: str = "BTC", fitness: float = 0.75) -> None:
    write_variant(conn, variant_id, "donchian", strategy, asset, '{}', "1.0", 1, "random_seed")
    update_variant_status(conn, variant_id, "queued")
    update_variant_status(conn, variant_id, "pre_scanning")
    update_variant_status(conn, variant_id, "running")
    update_variant_status(conn, variant_id, "evaluated", "completed")
    write_fitness_record(conn, variant_id, asset, composite=0.5, fitness=fitness)


class TestPromoteIfEligible:
    def test_below_threshold_no_trigger(self, conn):
        """Fitness unter Schwelle → kein Re-Eval."""
        attempt = promote_if_eligible(
            conn, "v_low_001", "donchian_breakout", "BTC",
            fitness_score=FITNESS_PROMOTION_THRESHOLD - 0.01,
        )
        assert attempt.triggered is False

    def test_nc_block_prevents_trigger(self, conn):
        """NC-Block ohne Reopen → kein Re-Eval auch wenn Fitness hoch."""
        from core.lab_negative_controls import NegativeControlResult
        nc_result = NegativeControlResult(blocked=True, nc=MagicMock(), reopen_available=False)
        with patch("core.lab_negative_controls.check_negative_control", return_value=nc_result):
            attempt = promote_if_eligible(
                conn, "v_nc_001", "donchian_breakout", "BTC",
                fitness_score=FITNESS_PROMOTION_THRESHOLD + 0.2,
            )
        assert attempt.triggered is False

    def test_eligible_triggers_reeval(self, conn):
        """Fitness über Schwelle + kein NC → run_one + _save_discovery werden aufgerufen."""
        from core.lab_negative_controls import NegativeControlResult
        nc_result = NegativeControlResult(blocked=False, nc=None, reopen_available=False)

        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.fail_reasons = []
        mock_result.dsr_oos = 0.65
        mock_result.pbo_val = 0.20
        mock_result.stability = 0.55

        mock_staging_conn = MagicMock()
        with (
            patch("core.lab_negative_controls.check_negative_control", return_value=nc_result),
            patch("scripts.run_v7_reeval.run_one", return_value=mock_result) as mock_run,
            patch("scripts.run_v7_reeval._save_discovery") as mock_save,
            patch("core.db.get_connection", return_value=mock_staging_conn),
        ):
            attempt = promote_if_eligible(
                conn, "v_go_001", "donchian_breakout", "BTC",
                fitness_score=FITNESS_PROMOTION_THRESHOLD + 0.1,
            )

        assert attempt.triggered is True
        mock_run.assert_called_once_with("donchian_breakout", "BTC")
        mock_save.assert_called_once()
        assert attempt.result_passed is True

    def test_reeval_exception_does_not_raise(self, conn):
        """Fehler im Re-Eval → triggered=False, kein unbehandelter Exception."""
        from core.lab_negative_controls import NegativeControlResult
        nc_result = NegativeControlResult(blocked=False, nc=None, reopen_available=False)

        with (
            patch("core.lab_negative_controls.check_negative_control", return_value=nc_result),
            patch("scripts.run_v7_reeval.run_one", side_effect=RuntimeError("DB nicht erreichbar")),
        ):
            attempt = promote_if_eligible(
                conn, "v_err_001", "donchian_breakout", "SOL",
                fitness_score=FITNESS_PROMOTION_THRESHOLD + 0.1,
            )

        assert attempt.error is not None
        assert attempt.triggered is False

    def test_reeval_timeout_sets_error_and_returns(self, conn):
        """run_one hängt → Timeout → attempt.error='reeval_timeout', kein Exception."""
        import time
        from concurrent.futures import TimeoutError as FutureTimeoutError
        from core.lab_negative_controls import NegativeControlResult
        nc_result = NegativeControlResult(blocked=False, nc=None, reopen_available=False)

        def _slow_run_one(strategy, asset):
            time.sleep(9999)

        with (
            patch("core.lab_negative_controls.check_negative_control", return_value=nc_result),
            patch("scripts.run_v7_reeval.run_one", side_effect=_slow_run_one),
            patch("core.lab_promotion_gate.REEVAL_TIMEOUT_SEC", 0.05),
        ):
            attempt = promote_if_eligible(
                conn, "v_timeout_001", "donchian_breakout", "ETH",
                fitness_score=FITNESS_PROMOTION_THRESHOLD + 0.1,
            )

        assert attempt.error == "reeval_timeout"
        assert attempt.triggered is True        # war getriggert, aber Timeout
        assert attempt.result_passed is None    # kein Ergebnis

    def test_reeval_success_after_timeout_constant_respected(self, conn):
        """Bei normalem Lauf innerhalb Timeout → Ergebnis wie gehabt."""
        from core.lab_negative_controls import NegativeControlResult
        nc_result = NegativeControlResult(blocked=False, nc=None, reopen_available=False)

        mock_result = MagicMock()
        mock_result.passed = False
        mock_result.fail_reasons = ["pbo_too_high"]
        mock_result.dsr_oos = 0.45
        mock_result.pbo_val = 0.40
        mock_result.stability = 0.30

        with (
            patch("core.lab_negative_controls.check_negative_control", return_value=nc_result),
            patch("scripts.run_v7_reeval.run_one", return_value=mock_result),
            patch("scripts.run_v7_reeval._save_discovery"),
            patch("core.db.get_connection", return_value=MagicMock()),
        ):
            attempt = promote_if_eligible(
                conn, "v_fail_001", "donchian_breakout", "SOL",
                fitness_score=FITNESS_PROMOTION_THRESHOLD + 0.1,
            )

        assert attempt.error is None
        assert attempt.result_passed is False
        assert attempt.fail_reasons == ["pbo_too_high"]

    def test_get_promotion_candidates_filters_correctly(self, conn):
        """get_promotion_candidates gibt nur evaluated Variants über Schwelle zurück."""
        _make_evaluated_variant(conn, "v_above_001", fitness=FITNESS_PROMOTION_THRESHOLD + 0.1)
        _make_evaluated_variant(conn, "v_below_001", fitness=FITNESS_PROMOTION_THRESHOLD - 0.1)

        candidates = get_promotion_candidates(conn, threshold=FITNESS_PROMOTION_THRESHOLD)
        ids = [c["variant_id"] for c in candidates]
        assert "v_above_001" in ids
        assert "v_below_001" not in ids

    def test_get_promotion_candidates_ordered_by_fitness(self, conn):
        """Kandidaten sind nach fitness DESC sortiert."""
        _make_evaluated_variant(conn, "v_ord_low", fitness=0.65)
        _make_evaluated_variant(conn, "v_ord_high", strategy="squeeze_pro", fitness=0.90)

        candidates = get_promotion_candidates(conn, threshold=FITNESS_PROMOTION_THRESHOLD)
        assert candidates[0]["fitness_score"] >= candidates[-1]["fitness_score"]


class TestEvolutionReportPromotion:
    def test_report_contains_promotion_section(self, db_path):
        """Evolution-Report enthält Promotion-Kandidaten-Sektion."""
        from scripts.lab_report_generator import generate_evolution_report
        report = generate_evolution_report(db_path)
        assert "Promotion-Kandidaten" in report

    def test_report_shows_go_variant(self, db_path):
        """GO-Variant erscheint namentlich im Evolution-Report."""
        conn = get_lab_state_connection(db_path)
        _make_evaluated_variant(conn, "v_rpt_001", fitness=FITNESS_PROMOTION_THRESHOLD + 0.1)
        conn.close()

        from scripts.lab_report_generator import generate_evolution_report
        report = generate_evolution_report(db_path)
        assert "v_rpt_001"[:8] in report
