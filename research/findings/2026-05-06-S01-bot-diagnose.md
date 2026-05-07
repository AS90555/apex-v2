# S-01 — Bot-Diagnose nach Fix

**Datum:** 2026-05-06  
**Prozess-Start:** 18:37:44 UTC+2  
**Fix eingespielt:** 18:33:44 UTC+2 (4 Min vor Start → Fix ist geladen)

---

## Check 1 — TELEGRAM_CHAT_ID

**Ergebnis:** GESETZT — Wert vorhanden in `config/.env`

`load_dotenv` in `telegram_bot.py` Zeile 27 läuft vor `config.settings`-Import →
`TELEGRAM_CHAT_ID` wird korrekt geladen. Kein Mismatch-Risiko durch Patch 1.

---

## Check 2 — Prozesse

```
PID 2117440  python3 /root/apex-v2/monitor/telegram_bot.py
Status: Sl (sleeping, interruptible) — läuft seit 03:30h
stdout → /root/apex-v2/logs/telegram_bot.log
stderr → /root/apex-v2/logs/telegram_bot.log
```

**Nur ein Prozess aktiv** — kein `Conflict: terminated by other getUpdates request`.

---

## Check 3 — Log-Analyse

**Log-Datei:** `logs/telegram_bot.log` — 19549 Bytes, letzte Änderung: 18:37:45

**Letzter Eintrag:**
```
2026-05-05 07:23:52  [BOT] WARNING push_heartbeat_alert missed by 0:00:10
nohup: ignoring input          ← geschrieben von nohup, NICHT von Python
```

**Kritischer Befund:** Seit Prozess-Start (18:37) gibt es **null neue Log-Einträge**.  
Die Scheduled Jobs (`push_heartbeat_alert` alle 5 Min, `push_new_trades` alle 2 Min)
hätten in 3,5 Stunden ~42 bzw. ~105 Einträge erzeugen müssen.

**Ursache:** Stummer Startup-Fehler — der Bot-Prozess lebt (PID aktiv, Status Sl),
aber der APScheduler/JobQueue startet nicht oder die Logging-Initialisierung schlägt fehl.
Python's `print()`-Aufrufe in `main()` (Startup-Banner) fehlen ebenfalls im Log →
der Code nach `main()` wird entweder nicht erreicht oder stdout ist vollständig gebuffert.

---

## Wahrscheinlichste Ursache

**Python-Stdout-Buffering in nohup:** Wenn der Prozess via `nohup python3 ...` ohne
`-u`-Flag gestartet wurde, ist stdout vollständig gebuffert (8192 Bytes). Print-Aufrufe
akkumulieren im Buffer ohne zu flushen — erst wenn der Buffer voll ist oder der Prozess
sauber endet.

**Aber:** Der Python `logging`-Handler (der die `WARNING`-Einträge schreibt) verwendet
typischerweise einen `FileHandler`, der NICHT über stdout geht und sofort flusht.
Dass auch logging-Einträge fehlen, ist das stärkere Signal: der Scheduler läuft nicht.

**Hypothese:** Der Bot blockiert in einem langen `await` beim Start (z.B.
`Application.initialize()` oder ersten Polling-Call) und der `on_startup`-Callback /
JobQueue nie startet, weil eine Exception in einem asyncio-Task geschluckt wird.

---

## Empfohlene Diagnose-Schritte (User führt aus)

### Schritt 1 — Prozess beenden und mit sichtbarem Output neu starten

```bash
kill 2117440
sleep 2
cd /root/apex-v2
python3 -u monitor/telegram_bot.py 2>&1 | tee /tmp/bot_debug.log
```

`-u` deaktiviert stdout-Buffering. Jede `print()`-Zeile erscheint sofort.
Starte den Bot so und schicke sofort `/start` an den Bot — Fehler sind direkt sichtbar.

### Schritt 2 — Nach Fehler suchen

```bash
# Falls der Bot direkt crasht:
python3 -u monitor/telegram_bot.py 2>&1 | head -50

# Oder im Hintergrund mit sofortigem Flush:
python3 -u monitor/telegram_bot.py >> /tmp/bot_debug.log 2>&1 &
sleep 10 && cat /tmp/bot_debug.log
```

### Schritt 3 — Webhook-Konflikt ausschließen

Wenn Telegram ein Webhook gesetzt hat, ignoriert es `getUpdates`-Polling komplett.
Webhook löschen (im Terminal oder Browser):
```
https://api.telegram.org/bot<TOKEN>/deleteWebhook
```

---

## Falls TELEGRAM_CHAT_ID leer wäre (zur Vollständigkeit)

Wenn `TELEGRAM_CHAT_ID` nicht gesetzt wäre, würde der Bot durch den S-01 Fix
**alle** Commands mit `⛔ Nicht autorisiert` ablehnen.

Chat-ID herausfinden:
1. **@userinfobot** — Bot anschreiben, er antwortet mit deiner User-ID
2. **getUpdates API:** `https://api.telegram.org/bot<TOKEN>/getUpdates` aufrufen,
   nachdem du dem Bot eine Nachricht geschickt hast — `"chat":{"id": XXXXXX}` im JSON

Die Chat-ID in `config/.env` eintragen:
```
TELEGRAM_CHAT_ID=XXXXXX
```

---

## Nächste Aktion

**Direkt:** `kill 2117440` + Neustart mit `python3 -u` und Ausgabe beobachten.  
Den ersten sichtbaren Fehler hierher pasten — dann kann der Root Cause eingegrenzt werden.
