# S-01 — Telegram-Auth: fail-OPEN bei leerer CHAT_ID

**Datum:** 2026-05-06  
**Analyst:** executor-hardener  
**Roadmap-ID:** S-01  
**Severity:** P0 — Live-Sicherheit  
**Datei:** `monitor/telegram_bot.py`

---

## Befund

### Bug-Kern: Zeilen 51–58 (`_is_authorized`)

```python
# Zeile 51–58
def _is_authorized(update) -> bool:
    """Nur der konfigurierte Chat darf Commands ausführen."""
    allowed = str(os.getenv("TELEGRAM_CHAT_ID", ""))
    if not allowed:
        return True  # ← FAIL-OPEN: leere CHAT_ID = jeder darf alles
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id) if update.effective_user else ""
    return chat_id == allowed or user_id == allowed
```

**Das ist der zentrale Bug.** Wenn `TELEGRAM_CHAT_ID` leer oder nicht gesetzt ist,
gibt `_is_authorized()` immer `True` zurück — jeder Telegram-Nutzer, der den Bot-Token
kennt, hat vollen Zugriff auf alle Commands.

Zusätzlich: `_is_authorized` liest per `os.getenv()` neu aus der Umgebung, statt den
bereits importierten `TELEGRAM_CHAT_ID`-Wert (Zeile 48) zu verwenden. Inkonsistenz.

---

### Handler ohne `_is_authorized`-Check

Von 11 registrierten Handlern prüfen **5 keine Auth**:

| Handler | Zeile | Funktion | Risiko |
|---------|-------|----------|--------|
| `cmd_start` | 2629 | Keyboard + Menü-Ausgabe | NIEDRIG (nur Info) |
| `cmd_menu` | 2645 | Menü anzeigen | NIEDRIG (nur Info) |
| `cmd_pnl` | 2682 | P&L-Dashboard anzeigen | MITTEL (interne Daten) |
| `cmd_lab` | 2690 | On-Demand Backtest starten | MITTEL (CPU-Last, interne Daten) |
| `cmd_fetch` | 2786 | Historische Daten von Binance laden | MITTEL (externe API-Calls) |
| `cmd_api_test` | 2927 | Bitget-API testen (live, `dry_run=False`) | **HOCH** (echte API-Keys genutzt) |
| `button_callback` | 3319 | Alle Inline-Buttons | **HOCH** (inkl. Deploy-Flows via Callback) |

Handler **mit** Auth-Check (korrekt): `cmd_status` (2654), `cmd_help` (2774),
`cmd_lab_stats` (2892), `cmd_alpha` (2903), `cmd_portfolio` (2916), `cmd_deploy` (3002).

---

### `button_callback` (Zeile 3319): kein Auth-Check

`button_callback` verarbeitet alle `CallbackQueryHandler`-Events — darunter potenziell
auch Deploy-ähnliche Flows, die über Inline-Buttons ausgelöst werden. Es gibt keinen
`_is_authorized`-Check. Ein Angreifer mit Bot-Token kann Callbacks direkt per API senden.

---

### `main()` (Zeile 4237–4243): Startup-Check greift zu spät

```python
if not TELEGRAM_CHAT_ID:
    print("FEHLER: TELEGRAM_CHAT_ID nicht gesetzt.")
    sys.exit(1)
```

Dieser Check wird nur beim normalen Start via `main()` ausgeführt. Wenn der Bot-Prozess
direkt via `Application.run_polling()` oder in Tests gestartet wird, wird `main()` nicht
zwingend durchlaufen — der Check ist keine Garantie.

---

## Risk-Pfad

**Voraussetzung für Angreifer:** Kenntnis des `TELEGRAM_BOT_TOKEN` (z.B. durch
Leak in Logs, GitHub, oder kompromittierten Server).

```
1. Angreifer kennt TELEGRAM_BOT_TOKEN
   └─ TELEGRAM_CHAT_ID ist leer/nicht gesetzt (z.B. frische Instanz, falsches .env)
      └─ _is_authorized() → return True (Zeile 55)
         ├─ /api_test → BitgetClient(dry_run=False) → echte API-Keys werden genutzt
         │              Balance-Abfrage, potenziell Order-Infos sichtbar
         ├─ /deploy 42 → _db_deploy(42) → Discovery wird als dry_run aktiviert
         │              (Zeile 3002 hat Auth-Check — aber nur wenn CHAT_ID gesetzt!)
         ├─ /fetch BTC 365 → unkontrollierte externe API-Calls (Rate-Limits)
         └─ button_callback → alle Inline-Button-Flows ohne Auth, inkl. zukünftiger
                              /live-Confirmation-Flows die über Buttons laufen
```

**Worst-Case-Szenario:** Ein künftiger `/live`-Confirmation-Flow wird als Inline-Button
implementiert (typisches Pattern). Da `button_callback` keine Auth hat, kann ein
Angreifer den Mode-Wechsel `dry_run → live` triggern, sofern CHAT_ID leer ist.

---

## Vorgeschlagener Fix

### Fix 1: `_is_authorized` auf fail-CLOSED umstellen (Kern-Fix)

```python
# monitor/telegram_bot.py, Zeile 51–58 — ERSETZEN durch:

def _is_authorized(update) -> bool:
    """Nur der konfigurierte Chat darf Commands ausführen.
    Fail-CLOSED: leere/fehlende CHAT_ID blockiert ALLE Zugriffe.
    """
    allowed = str(TELEGRAM_CHAT_ID).strip()  # Import aus config.settings, nicht os.getenv
    if not allowed:
        return False  # ← fail-CLOSED statt fail-OPEN
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    user_id = str(update.effective_user.id) if update.effective_user else ""
    return chat_id == allowed or user_id == allowed
```

**Einzige Änderung mit maximaler Wirkung:** `return True` → `return False`.
Alle bestehenden Handler-Checks greifen damit korrekt.

---

### Fix 2: Auth-Check in `cmd_api_test` und `button_callback` ergänzen

```python
# Zeile 2927 — cmd_api_test, erste Zeile ergänzen:
async def cmd_api_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        await update.message.reply_text("⛔ Nicht autorisiert.")
        return
    # ... rest unverändert

# Zeile 3319 — button_callback, nach query = update.callback_query ergänzen:
async def button_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _is_authorized(update):
        await query.answer(text="⛔ Nicht autorisiert.", show_alert=True)
        return
    # ... rest unverändert
```

---

### Fix 3: `cmd_pnl`, `cmd_lab`, `cmd_fetch` ebenfalls sichern

```python
# Pattern für alle drei — erste Zeilen ergänzen:
async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        await update.message.reply_text("⛔ Nicht autorisiert.")
        return

async def cmd_lab(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        await update.message.reply_text("⛔ Nicht autorisiert.")
        return

async def cmd_fetch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        await update.message.reply_text("⛔ Nicht autorisiert.")
        return
```

---

## DoD (aus Roadmap S-01)

Test nach Implementierung:
```bash
# .env mit leerer CHAT_ID → Bot starten → Command senden → muss abgelehnt werden
TELEGRAM_CHAT_ID="" python -c "
from monitor.telegram_bot import _is_authorized
class FakeUpdate:
    effective_chat = type('C', (), {'id': '99999999'})()
    effective_user = type('U', (), {'id': '99999999'})()
assert _is_authorized(FakeUpdate()) == False, 'FAIL: fail-open!'
print('PASS: fail-closed korrekt')
"
```

## Priorisierung

Fix 1 (eine Zeile, maximale Wirkung) sollte sofort implementiert werden.
Fixes 2+3 sind Defense-in-Depth und können in derselben PR mitgehen.
**Freigabe durch User erforderlich** (execution-adjacent, Sicherheitsrelevanz P0).
