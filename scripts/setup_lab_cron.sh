#!/usr/bin/env bash
# APEX V2 Lab-Cron-Setup
# Richtet Cron-Jobs für das autonome Research-Lab ein.
# Aufruf: bash scripts/setup_lab_cron.sh

set -euo pipefail

APEX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="python3"
CTRL="${APEX_DIR}/scripts/lab_controller.py"
LOG_DIR="${APEX_DIR}/logs"

mkdir -p "${LOG_DIR}"

# Bestehende APEX-Lab-Cron-Jobs entfernen
crontab -l 2>/dev/null | grep -v "# APEX-LAB" | crontab - 2>/dev/null || true

CRON_JOBS=$(cat <<EOF
# APEX-LAB: Wöchentlicher Asset-Profiler (Mo 02:00)
0 2 * * 1 cd ${APEX_DIR} && ${PYTHON} ${CTRL} --mode asset-profile-update >> ${LOG_DIR}/lab_profiler.log 2>&1  # APEX-LAB

# APEX-LAB: Queue-Build (Mo 03:00, nach Profiler)
0 3 * * 1 cd ${APEX_DIR} && ${PYTHON} ${CTRL} --mode build-queue >> ${LOG_DIR}/lab_queue.log 2>&1  # APEX-LAB

# APEX-LAB: Cycle starten (Mo 04:00)
0 4 * * 1 cd ${APEX_DIR} && ${PYTHON} ${CTRL} --mode run-cycle >> ${LOG_DIR}/lab_cycle.log 2>&1  # APEX-LAB

# APEX-LAB: Weekly Report (Fr 20:00)
0 20 * * 5 cd ${APEX_DIR} && ${PYTHON} ${CTRL} --mode generate-report >> ${LOG_DIR}/lab_report.log 2>&1  # APEX-LAB

# APEX-LAB: DB-Backup (täglich 01:50 UTC — vor Lab-Cycle)
50 1 * * * cd ${APEX_DIR} && ${PYTHON} scripts/db_backup.py >> ${LOG_DIR}/db_backup.log 2>&1  # APEX-LAB

# APEX-LAB: Regime-Detector (täglich 06:00 UTC — muss vor Health-Check laufen)
0 6 * * * cd ${APEX_DIR} && ${PYTHON} scripts/lab_regime_daily_check.py --assets BTC ETH SOL XRP LINK --send-telegram >> ${LOG_DIR}/regime_daily.log 2>&1  # APEX-LAB

# APEX-LAB: Daily Health Check (täglich 06:10)
10 6 * * * cd ${APEX_DIR} && ${PYTHON} ${CTRL} --mode health-check >> ${LOG_DIR}/lab_health.log 2>&1  # APEX-LAB

# APEX-LAB: Master-Watchdog (alle 5 Minuten — D.1 produktiv)
*/5 * * * * cd ${APEX_DIR} && ${PYTHON} scripts/master_watchdog.py >> ${LOG_DIR}/master_watchdog.log 2>&1  # APEX-LAB

# APEX-LAB: Gate-Immutabilitäts-Watchdog (täglich 06:05)
5 6 * * * cd ${APEX_DIR} && ${PYTHON} -m pytest tests/test_v72_gates_immutable.py -q >> ${LOG_DIR}/lab_gates_watchdog.log 2>&1  # APEX-LAB

# APEX-LAB: Heartbeat (alle 30 Minuten)
*/30 * * * * cd ${APEX_DIR} && ${PYTHON} ${CTRL} --mode heartbeat >> /dev/null 2>&1  # APEX-LAB
EOF
)

# Zu bestehenden Cron-Jobs hinzufügen
(crontab -l 2>/dev/null; echo "${CRON_JOBS}") | crontab -

echo "[setup-cron] APEX-Lab-Cron-Jobs eingerichtet:"
crontab -l | grep "APEX-LAB"
