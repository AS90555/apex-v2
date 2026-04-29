# APEX V2 — Änderungsprotokoll

**Datum:** 2026-04-29  
**Scope:** Fixes A–G (Bugs, Sicherheit, Lifecycle Lab→Deploy)

---

## Durchgeführte Änderungen

### FIX A — NameError beim Daemon-Start (KRITISCH)
**Datei:** `research/auto_lab_daemon.py`  
**Funktion:** `main()`  
**Zeilen:** ca. 865–874  
**Was:** `MIN_AVG_R_TEST` und `MIN_TRADES_TEST` wurden nach dem Multi-Window-Refactor nicht mehr als Konstanten definiert, aber noch in der Telegram-Startnachricht referenziert. Der Daemon crashte mit `NameError` beim Start.  
**Fix:** Startnachricht ersetzt durch Inline-Werte ohne die nicht-existenten Konstanten. Text beschreibt jetzt das 3-Fenster-OOS-System korrekt.

---

### FIX B — PF-Drop-Filter in `_passes_window()` (Overfit-Schutz)
**Datei:** `research/auto_lab_daemon.py`  
**Funktion:** `_passes_window()`, `_REJECTION_CATEGORY`  
**Was:** Der bestehende Overfit-Filter prüfte nur den `avg_r`-Abfall zwischen Train und Test. Ein Setup mit PF=3.0 im Train und PF=1.21 im Test (60% Einbruch) konnte die Prüfung bestehen.  
**Fix:** Nach dem `avg_r`-Drop-Check wird jetzt auch der PF-Drop-Ratio geprüft:  
`pf_drop_ratio = (tr["pf"] - te["pf"]) / max(tr["pf"], 0.01) > 0.35 → rejected`  
Neue Rejection-Kategorie `"ueberfit_pf"` in `_REJECTION_CATEGORY` ergänzt.

---

### FIX C — Discovery-ID im Telegram-Highscore-Push
**Datei:** `research/auto_lab_daemon.py`  
**Funktion:** `_notify_highscore()`, `_run_one_target()`  
**Was:** Der Highscore-Push zeigte nur eine fortlaufende Zählung (`Discovery #N`), nicht die Datenbank-ID. Ein User konnte den `/deploy`-Befehl nicht direkt aus dem Push nutzen.  
**Fix:** `disc_id: int = 0` als Parameter zu `_notify_highscore()` hinzugefügt. Nachricht enthält jetzt `🆔 Deploy-ID: \`{disc_id}\`` direkt nach der ersten Zeile. Aufruf in `_run_one_target()` mit `disc_id=disc_id` aktualisiert.

---

### FIX D — `_send_telegram()` auf MarkdownV2 vereinheitlichen
**Datei:** `research/auto_lab_daemon.py`  
**Funktion:** `_send_telegram()`  
**Was:** `_send_telegram()` verwendete `parse_mode: "Markdown"` (V1), während `_notify_highscore()` bereits MarkdownV2-escapte Strings sendete. Das führte zu fehlerhaftem Rendering oder stillen Telegram-Fehlern.  
**Fix:** `parse_mode` auf `"MarkdownV2"` geändert. Hilfsfunktion `_escape_md(text)` hinzugefügt für zukünftige Aufrufe die plain text escapen müssen. Die einzige Startnachricht war bereits korrekt escaped.

---

### FIX E — 6-Stunden-Heartbeat im Lab-Daemon
**Datei:** `research/auto_lab_daemon.py`  
**Funktion:** `main()`  
**Was:** Kein Monitoring ob der Lab-Daemon noch läuft. Ausfall wurde erst bemerkt wenn keine neuen Discoveries kamen.  
**Fix:** Variable `_last_heartbeat` und `_HEARTBEAT_INTERVAL = 6 * 3600` vor dem `while`-Loop eingefügt. Am Ende jeder Iteration: wenn 6 Stunden seit letztem Heartbeat → Telegram-Push `✅ Lab alive | Iteration #N | Discoveries: X`.

---

### FIX F — DB-Schema: Deployment-Tracking-Spalten in `lab_discoveries`
**Datei:** `research/auto_lab_daemon.py`  
**Funktion:** `_ensure_schema()`  
**Was:** `lab_discoveries` hatte keine Spalten um den Deployment-Status einer Discovery zu tracken. Kein Audit-Trail für Lab→Deploy-Entscheidungen.  
**Fix:** Idempotente Migration für 4 neue Spalten (via `PRAGMA table_info` + `ALTER TABLE`):
- `deployment_status TEXT NOT NULL DEFAULT 'lab'`
- `deployed_at TEXT`
- `deployed_by TEXT`
- `deploy_notes TEXT`

Index `idx_disc_deployment ON lab_discoveries(deployment_status)` angelegt.  
Migration läuft beim nächsten Daemon-Start automatisch durch (bereits auf Produktion ausgeführt: alle 4 Spalten vorhanden).

---

### FIX G — Security-Guard in `monitor/telegram_bot.py`
**Datei:** `monitor/telegram_bot.py`  
**Funktion:** `_is_authorized()` neu; Guard in 6 Command-Handlern  
**Was:** Kein Chat-ID-Check in den Handlern. Jeder Telegram-User der den Bot-Token kennt konnte `/deploy live` ausführen.  
**Fix:** Funktion `_is_authorized(update)` hinzugefügt (prüft `effective_chat.id` und `effective_user.id` gegen `TELEGRAM_CHAT_ID`). Guard am Anfang folgender Handler eingefügt:
- `cmd_deploy`
- `cmd_portfolio`
- `cmd_status`
- `cmd_lab_stats`
- `cmd_alpha`
- `cmd_help`

Fail-open wenn `TELEGRAM_CHAT_ID` nicht konfiguriert (kein Lock-out ohne Config).

---

## Bewusst NICHT geändert

- **Walk-Forward 120-Tage-Gap** (Fenster 1 OOS endet −360, Fenster 2 beginnt −240): Akzeptiert. Die drei OOS-Fenster testen unterschiedliche, nicht-überlappende Marktphasen. Lückenlose OOS-Fenster wären nur für Rolling-Walk-Forward mit Positionsübertrag relevant — das ist hier nicht das Design.
- **Bucket-Pruning** (`MAX_DISCOVERIES_PER_BUCKET = 5_000`): Kein aktiver Bedarf, alle Buckets weit unter Limit.
- **`cmd_start`, `cmd_menu`, `cmd_pnl`, `cmd_lab`, `cmd_fetch`, `cmd_api_test`**: Kein Guard — diese Commands lesen nur und haben keinen Deployment-Effekt.
- **`backtest/engine.py`**, **`governance/`**, **`features/`**, **`monitor/position_monitor.py`**: Keine Bugs identifiziert, keine Änderungen.

---

## Offene Empfehlungen (niedriger Prio, nächste Sprints)

1. **Walk-Forward lückenlos machen**: Fenster 1/2/3 so anpassen dass kein Zeitraum ungeprüft bleibt (z.B. −480..−360, −360..−180, −180..0). Vorteil: vollständige Abdeckung, keine blinden Marktphasen.
2. **Bucket-Pruning**: Schwächste Discoveries (micro_score < Median) aus vollen Buckets entfernen um die Alpha-Library kompakt und aktuell zu halten.
3. **`deployment_status` in Bot-Anzeige nutzen**: `/alpha` und `/portfolio` könnten Discoveries mit `deployment_status='lab'` vs. `'deployed'` unterschiedlich markieren.
4. **`cmd_start` / `cmd_menu` absichern**: Aktuell offen — kein Risiko (nur lesen), aber für vollständige Isolation sinnvoll.

---

## Status nach Fixes

- Daemon startet ohne `NameError` ✅
- Bot ist gegen unautorisierte `/deploy`-Aufrufe abgesichert ✅
- Lifecycle Lab→Deploy vollständig: Discovery-ID im Push, Deployment-Status in DB ✅
- Overfit-Schutz erweitert: PF-Drop-Filter aktiv ✅
- Monitoring: 6h-Heartbeat aktiv ✅
- Parse-Mode konsistent: alle Daemon-Nachrichten in MarkdownV2 ✅
