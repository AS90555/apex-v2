"""
Auswertung des v7.2 Multi-Kombinations-Screenings.

Liest Staging-DB + Log-Dateien und erstellt:
- Pro-Kombi-Summary in research/findings/v72_research/<datum>/<asset>_<strategy>/summary.md
- Ranking-Report in research/findings/v72_research/<datum>/screening_ranking.md
"""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_staging_connection
from core.utils import log

LOGDIR = "/tmp/v72_screening"
DATE_STR = datetime.now(timezone.utc).strftime("%Y-%m-%d")
OUT_BASE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "research", "findings", "v72_research", DATE_STR,
)

COMBOS = [
    ("donchian_breakout", "AVAX"),
    ("inside_bar_breakout", "XRP"),
    ("donchian_breakout", "XRP"),
    ("inside_bar_breakout", "LINK"),
    ("donchian_breakout", "LINK"),
    ("inside_bar_breakout", "AVAX"),
]


def _parse_log(strategy: str, asset: str) -> dict:
    """Parst timing und Trial-Ergebnisse aus dem Log."""
    logfile = os.path.join(LOGDIR, f"{strategy}_{asset}.log")
    if not os.path.exists(logfile):
        return {"exists": False}

    result = {"exists": True, "trials": [], "timing": []}
    with open(logfile) as f:
        lines = f.readlines()

    for line in lines:
        m = re.search(
            r"\[v72\] Trial (\d+): composite=([\d.\-]+) DSR=([\d.]+) PBO=([\d.]+) → (\w+)",
            line,
        )
        if m:
            result["trials"].append({
                "number": int(m.group(1)),
                "composite": float(m.group(2)),
                "dsr": float(m.group(3)),
                "pbo": float(m.group(4)),
                "passed": m.group(5) == "PASS",
            })
        if "Abgeschlossen:" in line:
            m2 = re.search(r"(\d+)/(\d+) Pass", line)
            if m2:
                result["pass_count"] = int(m2.group(1))
                result["total_count"] = int(m2.group(2))

    return result


def _get_staging_rows(strategy: str, asset: str) -> list:
    conn = get_staging_connection()
    rows = conn.execute("""
        SELECT params_hash, study_hash, objective_version, composite_score,
               dsr_value, pbo_value, max_drawdown, stability_score, oos_folds_n,
               sync_status, params_json
        FROM lab_discoveries
        WHERE framework_version='v7.2' AND strategy=? AND asset=?
        ORDER BY composite_score DESC NULLS LAST
    """, (strategy, asset)).fetchall()
    conn.close()
    return rows


def write_combo_report(strategy: str, asset: str) -> dict:
    """Erstellt Summary-Report für eine Kombi. Gibt Stats zurück."""
    log_data = _parse_log(strategy, asset)
    rows = _get_staging_rows(strategy, asset)

    if not log_data["exists"] or not rows:
        return {"strategy": strategy, "asset": asset, "status": "kein_log"}

    trials = log_data.get("trials", [])
    pass_count = log_data.get("pass_count", sum(1 for r in rows if (r["composite_score"] or 0) > 0 and r["dsr_value"] >= 0.5))
    total = log_data.get("total_count", len(rows))

    best = rows[0] if rows else None
    fail_reasons = {}
    for t in trials:
        if not t["passed"]:
            if t["dsr"] < 0.5:
                fail_reasons["DSR < 0.50"] = fail_reasons.get("DSR < 0.50", 0) + 1
            if t["pbo"] > 0.30:
                fail_reasons["PBO > 0.30"] = fail_reasons.get("PBO > 0.30", 0) + 1

    # Schreibe Report
    out_dir = os.path.join(OUT_BASE, f"{asset}_{strategy}")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "summary.md")

    lines = [
        f"# v7.2 Research Report — {DATE_STR}",
        "",
        f"**Kombination:** {strategy} / {asset}",
        f"**study_hash:** `{best['study_hash'] if best else 'n/a'}`  "
        f"**objective_version:** `{best['objective_version'] if best else 'n/a'}`",
        f"**Trials:** {total} | **Pass:** {pass_count} | **Fail:** {total - pass_count}",
        "",
        "## Bestes Trial",
        "",
    ]
    if best:
        lines += [
            f"| Feld | Wert |",
            f"|------|------|",
            f"| params_hash | `{best['params_hash']}` |",
            f"| composite | {best['composite_score']:.3f} |",
            f"| DSR | {best['dsr_value']:.3f} |",
            f"| PBO | {best['pbo_value']:.3f} |",
            f"| MaxDD | {best['max_drawdown']:.3f}R |",
            f"| Stability | {best['stability_score']:.3f} |",
        ]

    lines += [
        "",
        "## Alle Trials — Top-10",
        "",
        "| Trial | Composite | DSR | PBO | MaxDD | Stability | Pass |",
        "|-------|-----------|-----|-----|-------|-----------|------|",
    ]
    for t in trials[:10]:
        r = next((r for r in rows if abs((r["composite_score"] or 0) - t["composite"]) < 0.001), None)
        status = "✅" if t["passed"] else "❌"
        if r:
            lines.append(
                f"| {t['number']} | {t['composite']:.3f} | {t['dsr']:.3f} | {t['pbo']:.3f} | "
                f"{r['max_drawdown']:.3f} | {r['stability_score']:.3f} | {status} |"
            )

    lines += [
        "",
        "## Häufigste Fail-Reasons",
        "",
    ]
    for reason, count in sorted(fail_reasons.items(), key=lambda x: -x[1]):
        lines.append(f"- **{count}/{total}** {reason}")

    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    return {
        "strategy": strategy,
        "asset": asset,
        "pass_count": pass_count,
        "total": total,
        "best_composite": float(best["composite_score"]) if best else 0.0,
        "best_dsr": float(best["dsr_value"]) if best else 0.0,
        "best_pbo": float(best["pbo_value"]) if best else 1.0,
        "study_hash": best["study_hash"] if best else "",
        "report": out_path,
    }


def write_ranking_report(stats: list[dict]) -> str:
    """Erstellt Ranking-Report über alle Kombis."""
    ranked = sorted(
        [s for s in stats if s.get("status") != "kein_log"],
        key=lambda x: (-x["pass_count"], -x["best_composite"]),
    )

    out_path = os.path.join(OUT_BASE, "screening_ranking.md")
    os.makedirs(OUT_BASE, exist_ok=True)

    lines = [
        f"# v7.2 Screening Ranking — {DATE_STR}",
        "",
        f"Kombinationen: {len(stats)} | Pass-Kandidaten gesamt: {sum(s.get('pass_count', 0) for s in stats)}",
        "",
        "## Ranking",
        "",
        "| Rang | Strategie | Asset | Pass | Bester Composite | DSR | PBO |",
        "|------|-----------|-------|------|-----------------|-----|-----|",
    ]
    for i, s in enumerate(ranked, 1):
        lines.append(
            f"| {i} | {s['strategy']} | {s['asset']} | {s['pass_count']}/{s['total']} | "
            f"{s['best_composite']:.3f} | {s['best_dsr']:.3f} | {s['best_pbo']:.3f} |"
        )

    # Entscheidung
    top = ranked[0] if ranked else None
    total_passes = sum(s.get("pass_count", 0) for s in stats)

    lines += ["", "## Entscheidung", ""]
    if total_passes >= 2:
        verdict = "GO"
        lines.append(f"**{verdict}** — {total_passes} Pass-Kandidaten gefunden.")
    elif total_passes == 1:
        verdict = "GO-SCREEN"
        lines.append(f"**{verdict}** — 1 Pass-Kandidat, mehr Screening empfohlen.")
    else:
        verdict = "GO-SCREEN"
        lines.append("**GO-SCREEN** — 0 Passes bei 10 Trials (Mini) bzw. 50 Trials (Full).")
        lines.append("Empfehlung: andere Strategien oder Assets testen (vaa, squeeze).")

    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return out_path


def main():
    log("[eval] Starte Screening-Auswertung...")
    all_stats = []
    for strategy, asset in COMBOS:
        stats = write_combo_report(strategy, asset)
        all_stats.append(stats)
        log(f"[eval] {strategy}/{asset}: Pass={stats.get('pass_count', '?')}/{stats.get('total', '?')} "
            f"composite={stats.get('best_composite', 0):.3f}")

    ranking_path = write_ranking_report(all_stats)
    log(f"[eval] Ranking-Report: {ranking_path}")

    # Kompakt-Ausgabe
    print("\n" + "="*60)
    print("SCREENING RANKING")
    print("="*60)
    ranked = sorted(
        [s for s in all_stats if "pass_count" in s],
        key=lambda x: (-x["pass_count"], -x["best_composite"]),
    )
    for i, s in enumerate(ranked, 1):
        print(f"{i}. {s['strategy']:25s} / {s['asset']:6s}  "
              f"Pass={s['pass_count']}/{s['total']}  composite={s['best_composite']:.3f}  "
              f"DSR={s['best_dsr']:.3f}  PBO={s['best_pbo']:.3f}")
    print("="*60)
    total_passes = sum(s.get("pass_count", 0) for s in all_stats)
    print(f"Gesamt Pass-Kandidaten: {total_passes}")
    return all_stats


if __name__ == "__main__":
    main()
