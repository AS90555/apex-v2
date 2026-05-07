#!/bin/bash
cd /root/apex-v2
LOG=/root/apex-v2/logs/lab_daemon.log
while true; do
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Lab-Run gestartet" >> $LOG
    nice -n 19 python3 research/auto_lab_daemon.py --single-pass \
        >> $LOG 2>&1
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Pause 30 Min" >> $LOG
    sleep 1800
done
