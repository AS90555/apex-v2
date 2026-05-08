# APEX V2 — Lab Integrity Check
> Datum: 2026-05-08 | Durchgeführt von: apex-lead (backtest-validator + lab-tuner)
> Anlass: DSR-Fix (veraltetes .pyc) + vollständiger System-Check

---

## Gesamturteil: **WARN** (7× PASS, 3× WARN, 0× FAIL)

Die drei WARNs hängen alle an einer gemeinsamen Ursache: Der Lab-Daemon
läuft erst seit 08:42 CEST nach dem .pyc-Fix, und Optuna-Pruning ist mit
1617 gePrunten Trials extrem selektiv — kein Trial hat bisher das vollständige
Gauntlet (Pruning → Costs → DSR → BH) durchlaufen. Der Fix ist korrekt
implementiert, aber noch unbewiesen durch Live-Daten.

---

## Ergebnisse pro Block

| Block | Name | Ergebnis | Kurznotiz |
|-------|------|----------|-----------|
| 1 | DSR-Fix Verifikation | **WARN** | 0 Einträge mit cost_model=1; alle Trials pruned |
| 2 | Kostenmodell-Statistik | **WARN** | 452 alte ohne Kosten, 0 neue — Daemon noch zu frisch |
| 3 | Benjamini-Hochberg Filter | **WARN** | Keine BH-Logs — BH tritt erst nach completed Trial auf |
| 4 | Ruin-Filter alle 3 Fenster | **PASS** | ruin_filter: True in WF_WINDOWS[0,1,2] ✓ |
| 5 | Optuna Pruning aktiv | **PASS** | 1617 PRUNED-Events — Pruning feuert intensiv |
| 6 | Auto-Promotion Pipeline | **PASS** | 0 promoted, kein Fehler; neues Gate-Blocking korrekt |
| 7 | Auto-Demotion Pipeline | **PASS** | 0 demoted, kein Fehler |
| 8 | Parity Test | **PASS** | 12 PASS, 0 FAIL, 1 SKIP (weekend_momo korrekt) |
| 9 | HMM Regime-Check | **PASS** | Alle 5 Assets liefern Regime — kein FEHLER |
| 10 | Systemd-Services | **PASS** | Alle 7 Dienste active |

---

## Detail-Befunde

### WARN — DSR-Fix (Blöcke 1–3)

**Root Cause:** Nach dem .pyc-Fix läuft der Daemon erst ~45 Min. Das Optuna-Pruning
eliminiert aktuell alle Trials frühzeitig (1617 PRUNED gesamt). Ohne einen
completed Trial wird weder DSR noch BH geschrieben. Der Code ist korrekt
(manuelle .pyc-Inspektion bestätigt `' DSR='` im Bytecode, `[3-Window]` nicht
mehr vorhanden), aber empirisch unbewiesen.

**Trigger für Eskalation auf FAIL:** Nach 4h Daemon-Laufzeit noch immer
`cost_model_applied=1 = 0` → Pruning-Threshold oder Discovery-Bedingungen
prüfen.

### PASS — Auto-Promotion Gate-Blocking (Block 6)

Das Promotion-Script blockt alle 445 Discoveries korrekt, weil:
- `cost_model_applied = 0` (alle Altdaten, pre-fix)
- `pf_test_netto IS NULL`
- `dsr IS NULL`

Das ist das **korrekte Verhalten** — die alten Discoveries ohne Kostenmodell
sollen nicht mehr promoviert werden können.

### INFO — 28 Discoveries `Duplikat active_deployments.id` (korrekt)

Block 6 zeigt: 28 lab_discoveries scheitern am Gate "Duplikat active_deployments.id".
Nach Verifikation: Das Gate prüft ob bereits eine aktive `dry_run`-Deployment für
dieselbe `base_strategy + asset` Kombination existiert. Da z.B. `donchian_breakout/SOL`
bereits live ist, werden alle weiteren donchian_breakout/SOL-Discoveries korrekt
geblockt. Kein Bug, kein Constraint-Fehler — erwartetes Verhalten.

### BUG BEHOBEN — Regime-Monitor sendet keine Telegram-Alerts (Block 9)

**Root Cause:** `scripts/run_regime_monitor.py` fehlte `load_dotenv()` — Telegram-Credentials
waren zur Laufzeit leer. Der Monitor erkannte Wechsel korrekt und schrieb Logs, sendete aber
stillschweigend keinen Alert (`_send_telegram()` pruft `if not _TG_BOT or not _TG_CHAT → return`).

**Zusatz-Bug:** Der erste Fix zeigte auf `.env` (Wurzel) statt `config/.env`. Die Datei
existiert nur unter `config/.env` — alle anderen Scripts nutzen diesen Pfad bereits korrekt.

**Fix:** `load_dotenv(os.path.join(..., "config", ".env"))` in Zeile 15-16 hinzugefügt.
Verifikation: Laufzeit 146ms (kein API-Call) → 705ms (HTTP-Request erfolgreich) → Alerts gesendet.

### PASS — HMM Regime-Check (Block 9)

**Regime-Wechsel erkannt!** Drei Assets haben ihr Regime gewechselt:
- SOL: SIDEWAYS (unverändert)
- XRP: **TREND** ← neu (vorher SIDEWAYS)
- AVAX: SIDEWAYS (unverändert)
- LINK: **TREND** ← neu (vorher SIDEWAYS)
- ADA: **TREND** ← neu (vorher SIDEWAYS)

Dies ist eine signifikante Änderung gegenüber dem Weekly Report (2026-05-07)
wo alle Assets SIDEWAYS zeigten. Der Regime-Monitor-Timer sollte diese Wechsel
via Telegram gemeldet haben.

---

## Empfehlungen

1. **In 4h: Block 1+2 wiederholen** — DSR-Fix als PASS bestätigen sobald
   erster completed Trial in DB erscheint.

2. **Regime-Wechsel kommunizieren** — XRP/LINK/ADA wechseln zu TREND.
   inside_bar_breakout (XRP, deployed) und donchian_breakout (LINK, dry_run) sind
   für TREND grundsätzlich attraktiv — Strategie-Allowed-Regimes prüfen.
