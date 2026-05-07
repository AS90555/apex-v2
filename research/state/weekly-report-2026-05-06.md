# APEX V2 — Weekly Report 2026-05-06

> Erstellt von: apex-lead | Session: 2026-05-06

---

## Executive Summary

Die heutige Session schloss alle 4 priorisierten P0-Sicherheits-Items ab: der Telegram-Bot
wurde von fail-open auf fail-closed umgestellt (S-01), die entry_price/SL-TP-Parity zwischen
Live und Backtest wiederhergestellt (S-02), der zweistufige Daily-DD-Breaker mit Half-Size
bei -1.5R implementiert (S-03) sowie ein vollständiges Drift-Monitoring-System mit Auto-Pause
bei Live-PF-Einbruch > 50% gegenüber OOS-PF aufgebaut (S-04). Das System ist damit erstmals
in einem Zustand, in dem Live-Sicherheitsregeln und Backtest-Parity konsistent greifen — die
5 aktiven Deployments laufen alle im ok-Status, n < 30 (kein Drift-Trigger aktiv). Offen bleibt
S-05 (parity_test.py), das die DoD von S-02 vollständig automatisierbar macht, sowie die
gesamte P1-Schicht (statistische Validität des Labs), die das nächst-kritische Risiko darstellt.

---

## P0-Status

| ID | Titel | Status | Datum | Kritischer Befund |
|----|-------|--------|-------|-------------------|
| S-01 | Telegram-Auth fail-CLOSED | ✅ DONE | 2026-05-06 | `_is_authorized()` gab bei leerer CHAT_ID `True` zurück — jeder mit Bot-Token hatte Vollzugriff. 11/11 Handler jetzt gesichert. |
| S-02 | entry_price/SL-TP Konsistenz | ✅ DONE | 2026-05-06 | Live überschrieb entry_price mit aktuellem Marktpreis → SL systematisch verschoben gegenüber Backtest. Fix: entry_price = Signal-Close (parity-identisch). |
| S-03 | DAILY_DD_HALF_R implementieren | ✅ DONE | 2026-05-06 | Setting war definiert aber nirgendwo importiert. Zone -1.5R bis -2.0R wurde mit voller Größe gehandelt. Jetzt: zweistufig, Half-Size-Flag via governance_log. |
| S-04 | Live-vs-Backtest Drift Auto-Pause | ✅ DONE | 2026-05-06 | Tabelle und Cron-Job neu. Bei drift < -50% + n ≥ 30 → mode='shadow' automatisch. Baseline: 5 Deployments, alle ok. |
| S-05 | parity_test.py erstellen | 🔴 OPEN | — | tests/parity_test.py existiert nicht. S-02-DoD nur manuell verifizierbar. Höchste verbleibende P0-Lücke. |

---

## Aktive Risiken (noch nicht adressiert)

### P1-V-01 — Slippage + Fees + Funding fehlen im Backtest ⚠️ HÖCHSTE PRIORITÄT

**Risiko:** Alle OOS-PF-Werte (2.37–2.85) sind Brutto-Zahlen ohne reale Kosten.
Slippage (0.05%) + Fees (0.12% round-trip) + Funding (0.01%/8h) können den
tatsächlichen Netto-PF um 15–30% senken. Die Drift-Schwelle (-50%) ist damit gegen
einen überhöhten Basiswert gemessen — das Auto-Pause-System schlägt zu spät an.

**Konsequenz:** Ein Deployment mit Brutto-PF 2.40 und Netto-PF 1.68 erscheint im
Drift-Monitor erst als Problem wenn Live-PF unter 1.20 fällt (50% von 2.40) —
tatsächlich wäre der Edge schon bei 1.68 aufgebraucht.

### P1-V-04 — n_test ≥ 40 ist statistisch zu schwach

**Risiko:** Das Lab genehmigt Discoveries ab n_oos=40. Bei einer typischen Hit-Rate
von 30% falsch positiven Ergebnissen und 70 getesteten Kombinationen sind davon
~20 statistisch zufällig. Die aktuellen 5 Deployments stammen aus diesem Pool.

**Konsequenz:** Ohne FDR-Control (V-02) und ohne DSR (V-03) ist die Wahrscheinlichkeit
hoch, dass mehrere der aktiven Deployments keinen echten Edge haben. Das Drift-Monitoring
(S-04) fängt das auf — aber erst nach 30 Live-Trades, also mit Verzögerung.

### P1-V-02 — FDR-Control fehlt im Auto-Lab

**Risiko:** 70-Kombinations-Suche ohne Benjamini-Hochberg → ~20 "Funde" statt ~7.
Jeder zusätzliche False-Discovery erhöht das Kapitalrisiko im Dry-Run-Portfolio.

### P1-V-05 — Ruin-Filter nur auf jüngstem WF-Fenster

**Risiko:** Eine Strategie mit MaxDD 40% in Fenster 1 (alt) und MaxDD 5% in
Fenster 3 (aktuell) besteht den Filter. Live-Deployment ist möglich, obwohl
historisch ruinöse Drawdown-Phasen existieren.

### P1-V-03 — Deflated Sharpe Ratio fehlt

**Risiko:** Naker PF-Filter übersieht multiple-testing-Inflation. Ergänzt V-02 und V-04
— alle drei zusammen bilden die statistische Qualitätssicherung des Labs.

---

## Top-3 nächste Aktionen

### 1. S-05 — parity_test.py erstellen
**Owner:** executor-hardener
**Warum jetzt:** S-02 ist implementiert aber nicht automatisch verifizierbar.
Jede zukünftige Änderung an `generic_deployed.py` oder `backtest/engine.py`
könnte unbemerkt eine Parity-Lücke einführen. S-05 schließt die Sicherheitslücke
in der Test-Infrastruktur.
**DoD:** `python3 tests/parity_test.py` läuft grün für alle SIGNAL_FNS,
prüft entry_price/SL/TP Live == Backtest für dieselbe Bar.

### 2. V-01 — Slippage + Fees + Funding ins Backtest
**Owner:** backtest-validator
**Warum jetzt:** Höchste P1-Priorität. Verfälscht OOS-PF als Basiswert für den
Drift-Monitor (S-04), alle aktuellen Lab-Discoveries, und damit die Go-Live-Entscheidungen.
Solange V-01 offen ist, sind alle OOS-Zahlen in der Roadmap geschönt.
**DoD:** Backtest-PF sinkt nach Kosten um 15–30%, parity_test bleibt grün.

### 3. V-04 + V-02 — n_test ≥ 100 + FDR-Control im Lab
**Owner:** lab-tuner
**Warum jetzt:** Beide Fixes sind eng verwandt (Lab-Filter-Logik) und können
zusammen implementiert werden. Direkte Wirkung: saubere Discovery-Pipeline,
weniger False Positives im Dry-Run-Portfolio.
**DoD:** n_test ≥ 100 als Mindestfilter; Benjamini-Hochberg mit q=0.10
reduziert Discoveries von ~20 auf ~7 bei 70 Kombinationen.

---

## Drift-Monitor Baseline

Erster Check: 2026-05-06 18:57 UTC. Alle 5 Deployments — Status: **ok**.

| Deployment | Asset | Modus | n_live | pf_live | pf_oos | drift | Status | Auto-Pause-Trigger |
|---|---|---|---|---|---|---|---|---|
| donchian_breakout_551 | SOL | **live** | 1 | n/a | 2.37 | n/a | ✅ ok | n ≥ 30 + drift < -50% |
| inside_bar_breakout_334 | XRP | dry_run | 3 | n/a | 2.40 | n/a | ✅ ok | n ≥ 30 + drift < -50% |
| donchian_breakout_571 | AVAX | dry_run | 0 | n/a | 2.60 | n/a | ✅ ok | n ≥ 30 + drift < -50% |
| donchian_breakout_1157 | LINK | dry_run | 2 | n/a | 2.51 | n/a | ✅ ok | n ≥ 30 + drift < -50% |
| donchian_breakout_916 | ADA | dry_run | 1 | n/a | 2.85 | n/a | ✅ ok | n ≥ 30 + drift < -50% |

**Hinweis:** `pf_live = n/a` ist korrekt — alle aktuellen Trades sind Gewinn-Trades
(kein Verlust-Trade → gross_loss = 0 → PF nicht berechenbar). Drift wird messbar
sobald der erste Verlust-Trade gebucht wird. Nächster automatischer Check: tägl. 06:00 UTC.

**Kritische Schwellen (absolut):**

| Deployment | pf_oos | Warning-Level (-30%) | Critical-Level (-50%) |
|---|---|---|---|
| donchian_breakout_551 (SOL live) | 2.37 | PF-live < 1.66 | PF-live < 1.19 |
| inside_bar_breakout_334 (XRP) | 2.40 | PF-live < 1.68 | PF-live < 1.20 |
| donchian_breakout_916 (ADA) | 2.85 | PF-live < 2.00 | PF-live < 1.43 |

---

*Nächster Report: 2026-05-13 (automatisch via apex-lead Montag 06:00)*
