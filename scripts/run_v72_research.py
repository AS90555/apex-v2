"""
v7.2 Research-Entry-Point (Phase 5).

Optuna-Study mit OOS-Objective (evaluate_v7 pro Trial), Batch-Write in Staging,
Markdown-Report, Telegram-Zusammenfassung.

CLI:
  python3 scripts/run_v72_research.py --strategy donchian_breakout --asset BTC
      [--n-trials INT] [--batch-size INT] [--dry-run]

Erfordert V72_RESEARCH_ENABLED=true in .env — oder --dry-run.
Report: research/findings/v72_research/{date}/{strategy}_{asset}/summary.md
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import optuna
import requests

optuna.logging.set_verbosity(optuna.logging.WARNING)

from backtest.v7_eval import V7EvalResult
from config.settings import (
    OBJECTIVE_V72_VERSION,
    RANDOM_SEED,
    V72_RESEARCH_ENABLED,
)
from core.db import get_staging_connection
from core.utils import log
from research.lab_search_config import LAB_SEARCH_CFG
from research.v72_objective import V72TrialResult, compute_study_hash, objective_v72
from research.v72_staging_writer import batch_write_v72

_TG_BOT  = os.getenv("TELEGRAM_BOT" + "_TOKEN", "")
_TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

REEVAL_DAYS = 730


def _eval_window() -> tuple[int, int]:
    end_ts   = int(time.time() * 1000)
    start_ts = end_ts - REEVAL_DAYS * 24 * 3600 * 1000
    return start_ts, end_ts


def _send_telegram(text: str) -> None:
    if not _TG_BOT or not _TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_TG_BOT}/sendMessage",
            json={"chat_id": _TG_CHAT, "text": text, "parse_mode": "MarkdownV2"},
            timeout=10,
        )
    except Exception as e:
        log(f"[v72] Telegram-Fehler: {e}")


def _write_report(
    study: optuna.Study,
    study_hash: str,
    out_dir: str,
    dry_run: bool,
) -> str:
    os.makedirs(out_dir, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    trials = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
    passed = [t for t in trials if t.value is not None and t.value > 0
              and t.user_attrs.get("eval_result", {}).get("passed", False)]

    lines = [
        f"# v7.2 Research Report — {date_str}" + (" (DRY-RUN)" if dry_run else ""),
        "",
        f"study_hash: `{study_hash}`  objective_version: `{OBJECTIVE_V72_VERSION}`",
        f"Trials: {len(trials)} | Pass: {len(passed)} | Fail/PBO-pruned: {len(trials) - len(passed)}",
        "",
        "## Top-10 nach Composite",
        "",
        "| Trial | Composite | DSR | PBO | MaxDD | Stability | n_oos | Pass |",
        "|-------|-----------|-----|-----|-------|-----------|-------|------|",
    ]

    top_trials = sorted(
        [t for t in trials if t.value is not None],
        key=lambda t: t.value,
        reverse=True,
    )[:10]

    for t in top_trials:
        er = t.user_attrs.get("eval_result", {})
        status = "✅" if er.get("passed") else "❌"
        lines.append(
            f"| {t.number} | {t.value:.3f} | {er.get('dsr_oos', 0):.3f} | "
            f"{er.get('pbo_val', 1):.3f} | {er.get('max_dd', 0):.3f} | "
            f"{er.get('stability', 0):.3f} | {er.get('n_oos', 0)} | {status} |"
        )

    out_path = os.path.join(out_dir, "summary.md")
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    log(f"[v72] Report: {out_path}")
    return out_path


def _send_telegram_summary(
    study: optuna.Study,
    strategy: str,
    asset: str,
    study_hash: str,
    dry_run: bool,
) -> None:
    trials = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
    passed = [t for t in trials if t.user_attrs.get("eval_result", {}).get("passed", False)]

    if not passed:
        _send_telegram(
            f"*v7\\.2 Research ({'DRY\\-RUN ' if dry_run else ''}{strategy}/{asset})* — "
            f"0/{len(trials)} Pass"
        )
        return

    header = (
        f"*v7\\.2 Research {'\\(DRY\\-RUN\\) ' if dry_run else ''}"
        f"— {strategy}/{asset}: {len(passed)}/{len(trials)} Pass*\n"
    )
    lines = [header]
    for t in sorted(passed, key=lambda t: t.value or 0, reverse=True)[:5]:
        er = t.user_attrs.get("eval_result", {})
        lines.append(
            f"• Trial {t.number}: composite={t.value:.3f} DSR={er.get('dsr_oos', 0):.3f} "
            f"PBO={er.get('pbo_val', 1):.3f}"
        )
    _send_telegram("\n".join(lines))


def main() -> optuna.Study:
    parser = argparse.ArgumentParser(description="v7.2 Research — OOS-Optimierter Optuna-Run")
    parser.add_argument("--strategy",   type=str, required=True, help="Strategie-Name")
    parser.add_argument("--asset",      type=str, required=True, help="Asset (z.B. BTC)")
    parser.add_argument("--n-trials",   type=int, default=50,    help="Anzahl Optuna-Trials (default: 50)")
    parser.add_argument("--batch-size", type=int, default=10,    help="Staging-Batch-Größe (default: 10)")
    parser.add_argument("--dry-run",    action="store_true",      help="Kein DB-Write, nur Report")
    args = parser.parse_args()

    if not V72_RESEARCH_ENABLED and not args.dry_run:
        log("[v72] FEHLER: V72_RESEARCH_ENABLED=false — bitte .env setzen oder --dry-run nutzen")
        sys.exit(1)

    random.seed(RANDOM_SEED)
    try:
        import numpy as np
        np.random.seed(RANDOM_SEED)
    except ImportError:
        pass

    study_hash = compute_study_hash(args.strategy, args.asset)
    log(f"[v72] study_hash={study_hash} objective_version={OBJECTIVE_V72_VERSION}")
    log(f"[v72] Strategie={args.strategy} Asset={args.asset} n_trials={args.n_trials}"
        f"{'  (DRY-RUN)' if args.dry_run else ''}")

    study = optuna.create_study(
        study_name=f"v72_{args.strategy}_{args.asset}_{study_hash[:8]}",
        direction="maximize",
        sampler=LAB_SEARCH_CFG.build_sampler(),
        pruner=LAB_SEARCH_CFG.build_pruner(),
    )

    start_ts, end_ts = _eval_window()
    n_tested_hint = args.n_trials  # DSR-Multiple-Testing-Korrektur = Trial-Budget dieser Study

    batch_buffer: list[V72TrialResult] = []
    staging_conn = None if args.dry_run else get_staging_connection()

    def _flush_batch(buf: list[V72TrialResult]) -> None:
        if staging_conn is None:
            return
        ins, ign = batch_write_v72(staging_conn, buf)
        log(f"[v72] Staging-Flush: {ins} neu, {ign} ignoriert")

    def _objective(trial: optuna.Trial) -> float:
        score = objective_v72(
            trial, args.strategy, args.asset, start_ts, end_ts, n_tested_hint=n_tested_hint
        )
        er_dict = trial.user_attrs.get("eval_result", {})
        ev = V7EvalResult(
            strategy=args.strategy,
            asset=args.asset,
            params_json="",
            dsr_oos=er_dict.get("dsr_oos", 0.0),
            pbo_val=er_dict.get("pbo_val", 1.0),
            stability=er_dict.get("stability", 0.0),
            max_dd=er_dict.get("max_dd", 0.0),
            composite=er_dict.get("composite", 0.0),
            weights_hash=er_dict.get("weights_hash", ""),
            n_oos=er_dict.get("n_oos", 0),
            oos_folds_n=er_dict.get("oos_folds_n", 0),
            passed=er_dict.get("passed", False),
            fail_reasons=er_dict.get("fail_reasons", []),
        )
        # Params aus dem Trial rekonstruieren
        params = {k: v for k, v in trial.params.items()}
        tr = V72TrialResult(
            params=params,
            eval_result=ev,
            study_hash=study_hash,
            objective_version=OBJECTIVE_V72_VERSION,
            pruned=trial.user_attrs.get("pruned_pbo", False),
        )
        batch_buffer.append(tr)
        if len(batch_buffer) >= args.batch_size:
            _flush_batch(batch_buffer)
            batch_buffer.clear()

        status = "PASS" if ev.passed else "FAIL"
        log(f"[v72] Trial {trial.number}: composite={score:.3f} DSR={ev.dsr_oos:.3f} "
            f"PBO={ev.pbo_val:.3f} → {status}")
        return score

    study.optimize(_objective, n_trials=args.n_trials, gc_after_trial=True)

    if batch_buffer:
        _flush_batch(batch_buffer)
        batch_buffer.clear()

    if staging_conn is not None:
        staging_conn.close()

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "research", "findings", "v72_research", date_str,
        f"{args.strategy}_{args.asset}",
    )
    _write_report(study, study_hash, out_dir, args.dry_run)

    passed_count = sum(
        1 for t in study.trials
        if t.state == optuna.trial.TrialState.COMPLETE
        and t.user_attrs.get("eval_result", {}).get("passed", False)
    )
    log(f"[v72] Abgeschlossen: {passed_count}/{args.n_trials} Pass")

    _send_telegram_summary(study, args.strategy, args.asset, study_hash, args.dry_run)

    return study


if __name__ == "__main__":
    main()
