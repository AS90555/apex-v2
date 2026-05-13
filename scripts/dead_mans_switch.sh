#!/usr/bin/env bash
# Dead Man's Switch — Cron alle 2 Minuten.
#
# Prüft ob APEX-V2 noch läuft. Stufen:
#   1. Heartbeat-Datei data/heartbeats/master.hb aktuell?
#   2a. Bitget-Erreichbarkeit (Netzwerk-Check)
#   2b. Prozess-Check via pgrep
#   3. Echter Ausfall → emergency_close_all.py aufrufen
#
# Konfiguration via Umgebungsvariablen:
#   DEAD_MANS_TIMEOUT_SECONDS  (default: 300)
#   DEAD_MANS_RETRY_WAIT_SECONDS (default: 120)
#   APEX_DIR  (default: /root/apex-v2)

set -euo pipefail

APEX_DIR="${APEX_DIR:-/root/apex-v2}"
HB_FILE="${APEX_DIR}/data/heartbeats/master.hb"
TIMEOUT="${DEAD_MANS_TIMEOUT_SECONDS:-300}"
RETRY_WAIT="${DEAD_MANS_RETRY_WAIT_SECONDS:-120}"
LOG_DIR="${APEX_DIR}/logs"
TS=$(date -u +"%Y%m%d_%H%M%S")
LOG_FILE="${LOG_DIR}/dms_${TS}.log"

mkdir -p "${LOG_DIR}"

log() {
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] $*" | tee -a "${LOG_FILE}" >&2
}

send_telegram_alert() {
    local msg="$1"
    if [[ -n "${TELEGRAM_BOT:-}" ]] && [[ -n "${TELEGRAM_CHAT:-}" ]]; then
        curl -s -X POST \
            "https://api.telegram.org/bot${TELEGRAM_BOT}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT}&text=${msg}" \
            --max-time 10 || true
    fi
}

# Stufe 1: Heartbeat-Datei prüfen
if [[ ! -f "${HB_FILE}" ]]; then
    log "WARNUNG: Heartbeat-Datei nicht gefunden: ${HB_FILE}"
    HEARTBEAT_AGE=999999
else
    HB_MTIME=$(stat -c %Y "${HB_FILE}" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    HEARTBEAT_AGE=$(( NOW - HB_MTIME ))
    log "Heartbeat-Alter: ${HEARTBEAT_AGE}s (Timeout: ${TIMEOUT}s)"
fi

if [[ "${HEARTBEAT_AGE}" -le "${TIMEOUT}" ]]; then
    log "OK — System läuft (Heartbeat ${HEARTBEAT_AGE}s alt)"
    exit 0
fi

log "WARNUNG: Heartbeat ${HEARTBEAT_AGE}s alt — starte Verifikation"
send_telegram_alert "⚠️ DMS: Heartbeat stale (${HEARTBEAT_AGE}s). Verifikation läuft..."

# Stufe 2a: Bitget-Netzwerk-Check
BITGET_OK=false
if curl -sf --max-time 5 "https://api.bitget.com/api/mix/v1/market/time" > /dev/null 2>&1; then
    BITGET_OK=true
    log "Bitget erreichbar"
else
    log "Bitget NICHT erreichbar — möglicherweise Netzwerkausfall"
fi

# Stufe 2b: Prozess-Check
PROCESS_OK=false
if pgrep -f "master_run.py\|run_execution.py\|run_strategies.py" > /dev/null 2>&1; then
    PROCESS_OK=true
    log "APEX-Prozess läuft"
else
    log "APEX-Prozess NICHT gefunden"
fi

if [[ "${BITGET_OK}" == "true" ]] && [[ "${PROCESS_OK}" == "true" ]]; then
    log "Beide Checks positiv — wahrscheinlich nur kurzer HB-Ausfall. Warte ${RETRY_WAIT}s..."
    send_telegram_alert "⚠️ DMS: HB stale aber System läuft. Warte ${RETRY_WAIT}s..."
    sleep "${RETRY_WAIT}"

    # Zweiter Check
    HB_MTIME2=$(stat -c %Y "${HB_FILE}" 2>/dev/null || echo 0)
    NOW2=$(date +%s)
    AGE2=$(( NOW2 - HB_MTIME2 ))
    if [[ "${AGE2}" -le "${TIMEOUT}" ]]; then
        log "OK nach Retry — Heartbeat erneuert (${AGE2}s)"
        exit 0
    fi
    log "KRITISCH: Heartbeat nach Retry immer noch stale (${AGE2}s)"
fi

if [[ "${BITGET_OK}" == "false" ]]; then
    log "Netzwerk-Ausfall erkannt — kein Emergency-Close möglich, Eskalation"
    send_telegram_alert "🚨 DMS: Netzwerkausfall! APEX down, kein Emergency-Close möglich. Manuell eingreifen!"
    exit 1
fi

# Stufe 3: Echter Ausfall — Emergency Close
log "KRITISCH: Echter Ausfall bestätigt — Emergency Close wird ausgeführt"
send_telegram_alert "🚨 DMS AKTIVIERT: System ausgefallen! Emergency Close läuft..."

cd "${APEX_DIR}"
python scripts/emergency_close_all.py 2>&1 | tee -a "${LOG_FILE}"
EXIT_CODE=$?

if [[ "${EXIT_CODE}" -eq 0 ]]; then
    log "Emergency Close ERFOLGREICH"
    send_telegram_alert "✅ DMS: Emergency Close abgeschlossen. Admin-Intervention nötig vor Neustart!"
else
    log "Emergency Close TEILWEISE FEHLGESCHLAGEN (exit_code=${EXIT_CODE})"
    send_telegram_alert "❌ DMS: Emergency Close mit Fehlern! Sofort manuell prüfen! (exit=${EXIT_CODE})"
fi

exit "${EXIT_CODE}"
