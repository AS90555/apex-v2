#!/bin/bash
set -euo pipefail

export RESTIC_REPOSITORY=/root/apex-v2/data/backups/restic-repo
export RESTIC_PASSWORD=$(grep RESTIC_PASSWORD /root/apex-v2/config/.env | cut -d= -f2)

echo "[BACKUP] $(date -u +%Y-%m-%dT%H:%M:%SZ) — Starte restic Backup"

restic backup /root/apex-v2/data/apex_v2.db

echo "[BACKUP] Vergesse alte Snapshots (keep: 24h hourly, 7d daily, 4w weekly)"
restic forget --keep-hourly 24 --keep-daily 7 --keep-weekly 4

echo "[BACKUP] Prune unreferenzierter Daten"
restic prune

echo "[BACKUP] $(date -u +%Y-%m-%dT%H:%M:%SZ) — Fertig"

# ── Staleness-Alert ───────────────────────────────────────────────────────────
LAST_SNAP=$(restic snapshots --last --json 2>/dev/null \
  | python3 -c "import sys,json; snaps=json.load(sys.stdin); print(snaps[-1]['time'][:19] if snaps else '')" 2>/dev/null || echo "")
LAST_TS=$(date -d "$LAST_SNAP" +%s 2>/dev/null || echo 0)
NOW_TS=$(date +%s)
AGE_MIN=$(( (NOW_TS - LAST_TS) / 60 ))

if [ "$AGE_MIN" -gt 1500 ]; then
  TELEGRAM_BOT_TOKEN=$(grep TELEGRAM_BOT_TOKEN /root/apex-v2/.env | cut -d= -f2)
  TELEGRAM_CHAT_ID=$(grep TELEGRAM_CHAT_ID /root/apex-v2/.env | cut -d= -f2)
  if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
    MSG="⚠️ Backup-Alert: letzter Snapshot vor ${AGE_MIN} Min (Schwelle: 1500 Min)"
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      -d "chat_id=${TELEGRAM_CHAT_ID}" \
      -d "text=${MSG}" > /dev/null
    echo "[BACKUP] Staleness-Alert gesendet (${AGE_MIN} Min)"
  else
    echo "[BACKUP] WARNUNG: Staleness ${AGE_MIN} Min — Telegram-Tokens fehlen in .env" >&2
  fi
fi
