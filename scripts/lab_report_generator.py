"""
Lab-Report-Generator für APEX V2 Research-Lab.

Reiner Consumer — schreibt in keine DB.
Liest alle Tabellen, aggregiert, formatiert und sendet via Telegram.

Aufruf:
    python3 scripts/lab_report_generator.py --mode weekly
    python3 scripts/lab_report_generator.py --mode borderline
    python3 scripts/lab_report_generator.py --mode health
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).parent.parent / "config" / ".env")
except ImportError:
    pass

from core.lab_state_db import (
    get_borderline_candidates_pending_review,
    get_config_value,
    get_lab_state_connection,
)
from core.utils import log

LAB_STATE_DB = Path(__file__).parent.parent / "data" / "lab_state.db"
STAGING_DB = Path(__file__).parent.parent / "data" / "research_staging.db"
_TG_TOKEN_KEY = "TELEGRAM_BOT" + "_TOKEN"
_TG_CHAT_KEY = "TELEGRAM_CHAT_ID"


def _send_telegram(text: str) -> None:
    from core.telegram_dispatcher import dispatch
    dispatch(text)


# ─── Weekly Report ───────────────────────────────────────────────────────────

def generate_weekly_report(db_path: str = str(LAB_STATE_DB)) -> str:
    conn = get_lab_state_connection(db_path)
    now = datetime.now(timezone.utc)

    # Letzter abgeschlossener Cycle
    last_cycle = conn.execute(
        "SELECT * FROM lab_cycles ORDER BY id DESC LIMIT 1"
    ).fetchone()

    # Queue-Statistiken
    queue_stats: dict[str, int] = {}
    if last_cycle:
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM lab_queue WHERE cycle_id=? GROUP BY status",
            (last_cycle["id"],),
        ).fetchall()
        queue_stats = {r["status"]: r["n"] for r in rows}

    # Negative Controls (letzte 7 Tage)
    new_ncs = conn.execute(
        """SELECT strategy, asset, no_go_reason, created_at
           FROM negative_controls
           WHERE created_at >= datetime('now', '-7 days')
           ORDER BY created_at DESC"""
    ).fetchall()

    # Offene Borderline-Kandidaten
    pending_bc = get_borderline_candidates_pending_review(conn)

    # Gesamt-NC-Anzahl
    total_ncs = conn.execute("SELECT COUNT(*) FROM negative_controls WHERE closed_at IS NULL").fetchone()[0]

    # Budget-Nutzung
    budget = get_config_value(conn, "weekly_trial_budget") or "200"

    conn.close()

    lines = [
        "📊 <b>APEX Lab — Weekly Report</b>",
        f"📅 {now.strftime('%Y-%m-%d %H:%M')} UTC",
        "",
    ]

    if last_cycle:
        lines += [
            f"🔬 <b>Letzter Cycle</b>: #{last_cycle['id']} [{last_cycle['status']}]",
            f"   Start: {last_cycle['cycle_start'][:10]}",
        ]
        if last_cycle["cycle_end"]:
            lines.append(f"   Ende:  {last_cycle['cycle_end'][:10]}")
        if last_cycle["paused_reason"]:
            lines.append(f"   ⚠️ Grund: {last_cycle['paused_reason']}")
        if queue_stats:
            lines.append(f"   Queue: {dict(queue_stats)}")
    else:
        lines.append("   Kein Cycle in der DB")

    lines += ["", f"📋 <b>Negative Controls</b>: {total_ncs} aktiv"]
    if new_ncs:
        lines.append(f"   Neu diese Woche: {len(new_ncs)}")
        for nc in new_ncs[:5]:
            lines.append(f"   • {nc['strategy']}/{nc['asset']} [{nc['no_go_reason']}]")
        if len(new_ncs) > 5:
            lines.append(f"   ... und {len(new_ncs) - 5} weitere")

    lines += ["", f"🔶 <b>Offene Borderline-Reviews</b>: {len(pending_bc)}"]
    if pending_bc:
        timeout_days = int(get_config_value(
            get_lab_state_connection(db_path), "borderline_review_timeout_days"
        ) or "7")
        for bc in pending_bc[:5]:
            created = datetime.fromisoformat(bc.created_at)
            age_days = (now - created).days
            overdue = " ⏰ ÜBERFÄLLIG" if age_days >= timeout_days else ""
            missing = json.loads(bc.missing_gates) if isinstance(bc.missing_gates, str) else bc.missing_gates
            lines.append(
                f"   • {bc.strategy}/{bc.asset} "
                f"composite={bc.composite:.3f} n_oos={bc.n_oos} "
                f"missing={missing} ({age_days}d){overdue}"
            )

    lines += [
        "",
        f"⚙️ Trial-Budget: {budget}/Woche",
    ]

    report = "\n".join(lines)
    return report


# ─── Borderline-Review-Report ────────────────────────────────────────────────

def generate_borderline_report(db_path: str = str(LAB_STATE_DB)) -> str:
    conn = get_lab_state_connection(db_path)
    pending = get_borderline_candidates_pending_review(conn)
    now = datetime.now(timezone.utc)
    conn.close()

    if not pending:
        return "✅ Keine offenen Borderline-Reviews"

    lines = [
        "🔶 <b>Borderline-Kandidaten — Review erforderlich</b>",
        f"Gesamt offen: {len(pending)}",
        "",
    ]
    for bc in pending:
        created = datetime.fromisoformat(bc.created_at)
        age_days = (now - created).days
        missing = json.loads(bc.missing_gates) if isinstance(bc.missing_gates, str) else bc.missing_gates
        gate_vals = json.loads(bc.gate_values) if isinstance(bc.gate_values, str) else bc.gate_values
        gate_thresh = json.loads(bc.gate_thresholds) if isinstance(bc.gate_thresholds, str) else bc.gate_thresholds

        lines += [
            f"<b>#{bc.id}</b> {bc.strategy}/{bc.asset} (Alter: {age_days}d)",
            f"  composite={bc.composite:.4f} n_oos={bc.n_oos}",
            f"  Fehlende Gates: {missing}",
        ]
        for gate in missing:
            val = gate_vals.get(gate, "?")
            thresh = gate_thresh.get(gate, "?")
            lines.append(f"  {gate}: {val} (Schwelle: {thresh})")
        lines += [
            f"  Entscheiden:",
            f"  /bc_decide {bc.id} accept_gate_discussion",
            f"  /bc_decide {bc.id} promote_to_discussion",
            f"  /bc_decide {bc.id} reject",
            "",
        ]

    return "\n".join(lines)


# ─── Health-Report ───────────────────────────────────────────────────────────

def generate_health_report(db_path: str = str(LAB_STATE_DB)) -> str:
    conn = get_lab_state_connection(db_path)
    now = datetime.now(timezone.utc)
    alerts = []
    ok_items = []

    # Borderline-Timeout
    timeout_days = int(get_config_value(conn, "borderline_review_timeout_days") or "7")
    pending = get_borderline_candidates_pending_review(conn)
    overdue = [
        bc for bc in pending
        if (now - datetime.fromisoformat(bc.created_at)).days >= timeout_days
    ]
    if overdue:
        alerts.append(f"⏰ {len(overdue)} überfällige Borderline-Reviews (>{timeout_days}d)")
    else:
        ok_items.append(f"Borderline-Reviews: {len(pending)} offen, keine überfällig")

    # NC-Wachstum
    new_ncs = conn.execute(
        "SELECT COUNT(*) FROM negative_controls WHERE created_at >= datetime('now', '-7 days')"
    ).fetchone()[0]
    if new_ncs > 5:
        alerts.append(f"⚠️ NC-Wachstum: {new_ncs} neue NCs diese Woche")
    else:
        ok_items.append(f"NC-Wachstum: {new_ncs} neue NCs diese Woche")

    # Circuit-Breaker
    cb = conn.execute(
        "SELECT id FROM lab_cycles WHERE status='circuit_broken' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if cb:
        alerts.append(f"🚨 Circuit-Breaker aktiv auf Cycle #{cb['id']}")
    else:
        ok_items.append("Kein Circuit-Breaker aktiv")

    # Paused-Inconclusive-Entries
    inconclusive = conn.execute(
        "SELECT COUNT(*) FROM lab_queue WHERE status='paused_inconclusive'"
    ).fetchone()[0]
    if inconclusive > 0:
        alerts.append(f"❓ {inconclusive} Queue-Entries warten auf User-Entscheidung (PAUSED_INCONCLUSIVE)")

    conn.close()

    lines = [
        "🔍 <b>Lab Health Check</b>",
        f"📅 {now.strftime('%Y-%m-%d %H:%M')} UTC",
        "",
    ]
    if alerts:
        lines.append("⚠️ <b>Alerts:</b>")
        lines.extend(f"  {a}" for a in alerts)
    if ok_items:
        lines.append("✅ <b>OK:</b>")
        lines.extend(f"  {o}" for o in ok_items)

    return "\n".join(lines)


# ─── Evolution-Report ─────────────────────────────────────────────────────────

def generate_evolution_report(db_path: str = str(LAB_STATE_DB)) -> str:
    """Familien-Übersicht, Top-Variants, Behavior-Space-Heatmap (ASCII)."""
    conn = get_lab_state_connection(db_path)
    now = datetime.now(timezone.utc)
    lines = [
        "🧬 <b>APEX Lab — Evolution-Report</b>",
        f"📅 {now.strftime('%Y-%m-%d %H:%M')} UTC",
        "",
    ]

    # Familien-Übersicht
    families = conn.execute(
        "SELECT family_id, display_name, member_strategies FROM strategy_families ORDER BY family_id"
    ).fetchall()

    if families:
        lines.append("📊 <b>Familien-Übersicht</b>")
        for fam in families:
            # Counts pro Familie
            counts = conn.execute(
                """SELECT status, COUNT(*) as n FROM strategy_variants
                   WHERE family_id=? GROUP BY status""",
                (fam["family_id"],),
            ).fetchall()
            status_map = {r["status"]: r["n"] for r in counts}
            total = sum(status_map.values())
            evaluated = status_map.get("evaluated", 0)
            proposed = status_map.get("proposed", 0)

            # Beste Fitness
            best_fitness = conn.execute(
                """SELECT MAX(f.fitness) FROM fitness_records f
                   JOIN strategy_variants v ON v.variant_id=f.variant_id
                   WHERE v.family_id=?""",
                (fam["family_id"],),
            ).fetchone()[0]

            fitness_str = f"{best_fitness:.3f}" if best_fitness is not None else "—"
            lines.append(
                f"  <b>{fam['family_id']}</b>: {total} Variants "
                f"({evaluated} eval, {proposed} proposed) | best fitness={fitness_str}"
            )
    else:
        lines.append("  Keine Familien synchronisiert (lab_families.sync_to_db() noch nicht aufgerufen)")

    lines.append("")

    # Top-10 Variants nach Fitness
    top_variants = conn.execute(
        """SELECT v.variant_id, v.family_id, v.strategy, v.asset,
                  v.proposed_by, v.generation, f.fitness, f.composite, f.dsr_oos
           FROM strategy_variants v
           JOIN fitness_records f ON f.variant_id=v.variant_id
           ORDER BY f.fitness DESC
           LIMIT 10""",
    ).fetchall()

    if top_variants:
        lines.append("🏆 <b>Top-10 Variants nach Fitness</b>")
        for i, v in enumerate(top_variants, 1):
            lines.append(
                f"  {i}. {v['strategy']}/{v['asset']} [{v['family_id']}] "
                f"fitness={v['fitness']:.3f} composite={v['composite']:.3f} "
                f"dsr={v['dsr_oos'] or 0:.3f} gen={v['generation']} "
                f"({v['proposed_by'][:12]}) vid={v['variant_id'][:8]}"
            )
    else:
        lines.append("🏆 <b>Top Variants</b>: Noch keine evaluierten Variants")

    lines.append("")

    # GO-Variants (Promotion-Kandidaten)
    from core.lab_promotion_gate import get_promotion_candidates, FITNESS_PROMOTION_THRESHOLD
    go_candidates = get_promotion_candidates(conn, threshold=FITNESS_PROMOTION_THRESHOLD)
    if go_candidates:
        lines.append(f"🚀 <b>Promotion-Kandidaten</b> (fitness ≥ {FITNESS_PROMOTION_THRESHOLD})")
        for c in go_candidates:
            lines.append(
                f"  • {c['strategy']}/{c['asset']} [{c['family_id']}] "
                f"fitness={c['fitness_score']:.3f} gen={c['generation']} "
                f"vid={c['variant_id'][:8]}"
            )
    else:
        lines.append(f"🚀 <b>Promotion-Kandidaten</b>: Keine (Schwelle: {FITNESS_PROMOTION_THRESHOLD})")
    lines.append("")

    # Behavior-Space-Heatmap (ASCII)
    lines.append("🗺️ <b>Behavior-Space-Coverage</b> (Regime × Familie)")
    regimes = ["HIGH_TREND", "SIDEWAYS", "MIXED"]
    fam_ids = [f["family_id"] for f in families] if families else []

    if fam_ids:
        header = "           | " + " | ".join(f"{r[:7]:7}" for r in regimes)
        lines.append(f"  <code>{header}</code>")
        for fid in fam_ids:
            row_parts = []
            for regime in regimes:
                count = conn.execute(
                    """SELECT COUNT(DISTINCT v.variant_id)
                       FROM strategy_variants v
                       JOIN asset_profiles p ON p.asset=v.asset
                       WHERE v.family_id=? AND p.regime=?
                       AND v.status IN ('evaluated', 'running')""",
                    (fid, regime),
                ).fetchone()[0]
                cell = f"  {count:2d}   " if count > 0 else "  --   "
                row_parts.append(cell)
            line = f"  {fid:12} | " + " | ".join(row_parts)
            lines.append(f"  <code>{line}</code>")

    lines.append("")

    # Regime-History (letzte Änderungen)
    drift_events = conn.execute(
        """SELECT asset, computed_at, regime, prev_regime, change_detected
           FROM regime_history
           WHERE change_detected=1
           ORDER BY computed_at DESC LIMIT 5"""
    ).fetchall()

    if drift_events:
        lines.append("🔄 <b>Letzte Regime-Wechsel</b>")
        for ev in drift_events:
            lines.append(
                f"  {ev['asset']}: {ev['prev_regime']} → {ev['regime']} "
                f"({ev['computed_at'][:10]})"
            )
    else:
        lines.append("🔄 <b>Regime-Wechsel</b>: Keine in Historie")

    conn.close()
    return "\n".join(lines)


def _send_regime_drift_telegram(
    asset: str,
    prev_regime: str,
    current_regime: str,
    confidence: float,
) -> None:
    text = (
        f"🔄 <b>Regime-Wechsel: {asset}</b>\n"
        f"{prev_regime} → {current_regime}\n"
        f"Confidence: {confidence:.0%}\n"
        f"Reopen-Prüfung wird beim nächsten Cycle ausgeführt"
    )
    _send_telegram(text)


def _send_family_exhausted_telegram(
    family_id: str,
    n_variants: int,
    last_4gp: str | None,
) -> None:
    text = (
        f"📊 <b>Familie '{family_id}' erschöpft</b>\n"
        f"{n_variants} Variants getestet\n"
        f"Letzter 4-Gate-Pass: {last_4gp or 'nie'}\n"
        f"Vorschlag: Familie pausieren oder neue Varianten manuell hinzufügen"
    )
    _send_telegram(text)


def _send_behavior_gap_telegram(
    regime: str,
    family_id: str,
) -> None:
    text = (
        f"🎯 <b>Diversitäts-Lücke erkannt</b>\n"
        f"Regime: {regime} × Familie: {family_id}\n"
        f"Nächster Cycle wird darauf fokussiert"
    )
    _send_telegram(text)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="APEX Lab Report Generator")
    parser.add_argument("--mode", choices=["weekly", "borderline", "health", "evolution"], default="weekly")
    parser.add_argument("--send", action="store_true", help="Via Telegram senden")
    parser.add_argument("--db-path", type=str, default=str(LAB_STATE_DB))
    args = parser.parse_args()

    if args.mode == "weekly":
        report = generate_weekly_report(args.db_path)
    elif args.mode == "borderline":
        report = generate_borderline_report(args.db_path)
    elif args.mode == "evolution":
        report = generate_evolution_report(args.db_path)
    else:
        report = generate_health_report(args.db_path)

    log(f"[report]\n{report}")
    if args.send:
        _send_telegram(report)


if __name__ == "__main__":
    main()
