#!/bin/bash
export PYTHONPATH=/root/apex-v2
while true; do
    python3 /root/apex-v2/monitor/position_monitor.py
    echo "[$(date)] position_monitor beendet — Neustart in 5s" >> /root/apex-v2/logs/pm_restart.log
    sleep 5
done
