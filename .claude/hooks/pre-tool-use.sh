#!/usr/bin/env bash
# APEX V2 — Pre-Tool-Use Safety Hook
# Claude Code übergibt Hook-Daten als JSON auf stdin.

set -euo pipefail

# JSON von stdin lesen
INPUT="$(cat)"

# Felder extrahieren via python3 (immer verfügbar, kein jq nötig)
TOOL_NAME="$(echo "$INPUT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('tool_name', ''))
")"

# tool_input als flachen String für Grep-Checks
TOOL_INPUT_RAW="$(echo "$INPUT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
ti = d.get('tool_input', {})
# Alle String-Werte zusammenfassen (command, file_path, path, content, ...)
print(' '.join(str(v) for v in ti.values() if isinstance(v, str)))
")"

block() {
    echo "[APEX HOOK] BLOCKIERT: $1" >&2
    exit 2
}

# --- Check 1: apex_v2.db ohne heutiges Backup ---
if echo "$TOOL_INPUT_RAW" | grep -q "apex_v2\.db"; then
    TODAY="$(date +%Y-%m-%d)"
    BACKUP_DIR="/root/apex-v2/data/backups"
    if ! ls "$BACKUP_DIR"/*"${TODAY}"* 2>/dev/null | grep -q .; then
        block "apex_v2.db referenziert, aber kein Backup für heute (${TODAY}) in data/backups/. Backup zuerst erstellen."
    fi
fi

# --- Check 2: Geheimschlüssel als Klartext ---
if echo "$TOOL_INPUT_RAW" | grep -qiE "BITGET_API_KEY|BITGET_SECRET_KEY|TELEGRAM_BOT_TOKEN"; then
    block "Geheimschlüssel als Klartext erkannt (BITGET_API_KEY / BITGET_SECRET_KEY / TELEGRAM_BOT_TOKEN). Niemals in Code oder Commands!"
fi

exit 0
