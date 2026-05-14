#!/usr/bin/env bash
# v7.2 Multi-Kombinations-Screening (sequenziell)
# Prio: AVAX/donchian, XRP/inside_bar, XRP/donchian, LINK/inside_bar, LINK/donchian, AVAX/inside_bar

set -euo pipefail

LOGDIR="/tmp/v72_screening"
mkdir -p "$LOGDIR"

N_TRIALS=50
export V72_RESEARCH_ENABLED=true

COMBOS=(
  "donchian_breakout:AVAX"
  "inside_bar_breakout:XRP"
  "donchian_breakout:XRP"
  "inside_bar_breakout:LINK"
  "donchian_breakout:LINK"
  "inside_bar_breakout:AVAX"
)

echo "[SCREENING] Start: $(date '+%Y-%m-%d %H:%M:%S')  n_trials=$N_TRIALS"
echo "[SCREENING] Kombis: ${#COMBOS[@]}"

for combo in "${COMBOS[@]}"; do
  strategy="${combo%%:*}"
  asset="${combo##*:}"
  logfile="$LOGDIR/${strategy}_${asset}.log"

  echo ""
  echo "[SCREENING] ===== $strategy / $asset ====="
  echo "[SCREENING] Log: $logfile"
  echo "[SCREENING] Start: $(date '+%H:%M:%S')"
  t_start=$(date +%s)

  V72_RESEARCH_ENABLED=true python3 scripts/run_v72_research.py \
    --strategy "$strategy" --asset "$asset" \
    --n-trials "$N_TRIALS" \
    > "$logfile" 2>&1

  t_end=$(date +%s)
  elapsed=$((t_end - t_start))
  echo "[SCREENING] Fertig: $(date '+%H:%M:%S')  Dauer: $((elapsed/60))m $((elapsed%60))s"
  grep "\[v72\] Trial\|\[v72\] Abgeschlossen" "$logfile" | tail -5
done

echo ""
echo "[SCREENING] === ALLE KOMBIS ABGESCHLOSSEN: $(date '+%Y-%m-%d %H:%M:%S') ==="
echo "[SCREENING] Starte Auswertung..."
python3 scripts/eval_v72_screening.py
echo "[SCREENING] Auswertung fertig."
