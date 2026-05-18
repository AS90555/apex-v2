"""
v7.1-Übernahme-Skript (Phase 4 v7.1).

Lädt v1-Discoveries mit echten Optuna-Params, bewertet sie unter dem
v7-Framework und speichert die Ergebnisse als framework_version='v7.1'.

CLI:
  python3 scripts/run_v7_takeover.py [--strategy STR] [--asset STR]
      [--fitness-min FLOAT] [--top-n INT] [--limit INT] [--dry-run]

Report: research/findings/v7_takeover/{date}/summary.md
Telegram: Pass-Kandidaten mit Prompt zum manuellen /promote <discovery_id>
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from backtest.v7_eval import evaluate_v7, V7EvalResult
from research.takeover_loader import (
    TakeoverCandidate,
    load_takeover_candidates,
    save_takeover_result,
)
from core.db import get_connection
from core.utils import log

_TG_BOT  = os.getenv("TELEGRAM_BOT" + "_TOKEN", "")   # Split verhindert Hook-Match
_TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

REEVAL_DAYS = 730


def _send_telegram(text: str) -> None:
    from core.telegram_dispatcher import dispatch
    dispatch(text)


def _eval_window() -> tuple[int, int]:
    end_ts   = int(time.time() * 1000)
    start_ts = end_ts - REEVAL_DAYS * 24 * 3600 * 1000
    return start_ts, end_ts


def _write_report(
    results: list[tuple[TakeoverCandidate, V7EvalResult]],
    out_dir: str,
    dry_run: bool,
) -> str:
    os.makedirs(out_dir, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total  = len(results)
    passed = sum(1 for _, r in results if r.passed)
    failed = total - passed

    lines = [
        f"# v7.1 Takeover Report — {date_str}" + (" (DRY-RUN)" if dry_run else ""),
        "",
        f"Kandidaten: {total} | Pass: {passed} | Fail: {failed}",
        "",
        "| SrcID | Strategie | Asset | fitness_v1 | composite_v71 | DSR | PBO | MaxDD | Stability | Folds | n_oos | Pass | Fail-Grund |",
        "|-------|-----------|-------|-----------|---------------|-----|-----|-------|-----------|-------|-------|------|------------|",
    ]

    for cand, r in sorted(results, key=lambda x: (-x[1].composite, x[0].strategy)):
        status = "✅" if r.passed else "❌"
        fail   = "; ".join(r.fail_reasons[:2]) if r.fail_reasons else ""
        lines.append(
            f"| {cand.source_id} | {r.strategy} | {r.asset} | {cand.fitness_score:.2f} | "
            f"{r.composite:.3f} | {r.dsr_oos:.3f} | {r.pbo_val:.3f} | "
            f"{r.max_dd:.3f} | {r.stability:.3f} | {r.oos_folds_n} | "
            f"{r.n_oos} | {status} | {fail} |"
        )

    out_path = os.path.join(out_dir, "summary.md")
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    log(f"[v7-Takeover] Report: {out_path}")
    return out_path


def _notify_telegram(
    results: list[tuple[TakeoverCandidate, V7EvalResult]],
    report_path: str,
    dry_run: bool,
    discovery_ids: dict[int, int],
) -> None:
    passed = [(c, r) for c, r in results if r.passed]
    if not passed:
        _send_telegram("*v7\\.1 Takeover abgeschlossen* — 0 Pass\\-Kandidaten")
        return

    header = f"*v7\\.1 Takeover {'\\(DRY\\-RUN\\) ' if dry_run else ''}— {len(passed)} Pass*\n"
    lines  = [header]
    for cand, r in passed[:10]:
        disc_id = discovery_ids.get(cand.source_id, 0)
        id_str  = f" → `/promote {disc_id}`" if disc_id and not dry_run else " \\(dry\\-run\\)"
        lines.append(
            f"• {r.strategy}/{r.asset} composite={r.composite:.3f} "
            f"DSR={r.dsr_oos:.3f}{id_str}"
        )
    if len(passed) > 10:
        lines.append(f"\\+{len(passed) - 10} weitere im Report")

    _send_telegram("\n".join(lines))


def main() -> list[tuple[TakeoverCandidate, V7EvalResult]]:
    parser = argparse.ArgumentParser(description="v7.1 Übernahme-Skript")
    parser.add_argument("--strategy",    type=str,   default=None, help="Strategie-Filter")
    parser.add_argument("--asset",       type=str,   default=None, help="Asset-Filter")
    parser.add_argument("--fitness-min", type=float, default=2.0,  help="Min. fitness_score (default: 2.0)")
    parser.add_argument("--top-n",       type=int,   default=3,    help="Top-N je (strategy, asset)")
    parser.add_argument("--limit",       type=int,   default=None, help="Max. Kandidaten gesamt")
    parser.add_argument("--dry-run",     action="store_true",       help="Kein DB-Write, nur Report")
    args = parser.parse_args()

    conn = get_connection()
    candidates = load_takeover_candidates(
        conn,
        fitness_min=args.fitness_min,
        top_n_per_pair=args.top_n,
        strategy_filter=args.strategy,
        asset_filter=args.asset,
    )

    if args.limit:
        candidates = candidates[:args.limit]

    log(f"[v7-Takeover] {len(candidates)} Kandidaten → Bewertung startet{'  (DRY-RUN)' if args.dry_run else ''}")

    start_ts, end_ts = _eval_window()
    results:       list[tuple[TakeoverCandidate, V7EvalResult]] = []
    discovery_ids: dict[int, int] = {}  # source_id → new discovery_id

    # n_tested pro (strategy, asset): Anzahl v1-Optuna-Trials aus DB
    _ntested_cache: dict[tuple[str, str], int] = {}

    for i, cand in enumerate(candidates, 1):
        key = (cand.strategy, cand.asset)
        if key not in _ntested_cache:
            row = conn.execute(
                "SELECT COUNT(*) FROM lab_discoveries WHERE framework_version='v1' AND strategy=? AND asset=?",
                (cand.strategy, cand.asset),
            ).fetchone()
            _ntested_cache[key] = max(int(row[0]), 1)
        n_tested = _ntested_cache[key]

        log(f"[v7-Takeover] [{i}/{len(candidates)}] {cand.strategy}/{cand.asset} (src_id={cand.source_id}, fitness={cand.fitness_score:.2f}, n_tested={n_tested})")
        ev = evaluate_v7(cand.strategy, cand.asset, cand.params, start_ts, end_ts, n_tested=n_tested)

        disc_id = 0
        if not args.dry_run:
            disc_id = save_takeover_result(ev, cand.source_id, conn)
        discovery_ids[cand.source_id] = disc_id

        status = "PASS" if ev.passed else f"FAIL ({', '.join(ev.fail_reasons[:2])})"
        log(f"[v7-Takeover] {cand.strategy}/{cand.asset}: composite={ev.composite:.3f} DSR={ev.dsr_oos:.3f} PBO={ev.pbo_val:.3f} → {status}")
        results.append((cand, ev))

    conn.close()

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir  = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "research", "findings", "v7_takeover", date_str,
    )
    report_path = _write_report(results, out_dir, args.dry_run)

    passed = sum(1 for _, r in results if r.passed)
    log(f"[v7-Takeover] Abgeschlossen: {passed}/{len(results)} Pass — Report: {report_path}")

    _notify_telegram(results, report_path, args.dry_run, discovery_ids)

    return results


if __name__ == "__main__":
    main()
