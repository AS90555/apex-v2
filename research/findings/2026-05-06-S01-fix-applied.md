# S-01 — Fix Applied: Telegram-Auth fail-CLOSED

**Datum:** 2026-05-06  
**Roadmap-ID:** S-01 → DONE  
**Datei:** `monitor/telegram_bot.py`  
**py_compile:** ✅ kein Syntaxfehler

---

## Patch 1 — `_is_authorized()` fail-CLOSED (Zeilen 51–60)

**Vorher:**
```python
def _is_authorized(update) -> bool:
    """Nur der konfigurierte Chat darf Commands ausführen."""
    allowed = str(os.getenv("TELEGRAM_CHAT_ID", ""))
    if not allowed:
        return True  # Kein Filter wenn nicht konfiguriert
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id) if update.effective_user else ""
    return chat_id == allowed or user_id == allowed
```

**Nachher:**
```python
def _is_authorized(update) -> bool:
    """Nur der konfigurierte Chat darf Commands ausführen.
    Fail-CLOSED: leere/fehlende CHAT_ID blockiert ALLE Zugriffe.
    """
    allowed = str(TELEGRAM_CHAT_ID).strip()
    if not allowed:
        return False  # fail-CLOSED — kein Zugriff ohne konfigurierte CHAT_ID
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    user_id = str(update.effective_user.id) if update.effective_user else ""
    return chat_id == allowed or user_id == allowed
```

**Änderungen:**
- `os.getenv("TELEGRAM_CHAT_ID", "")` → `TELEGRAM_CHAT_ID` (bereits importiert, konsistent)
- `return True` → `return False` (fail-CLOSED statt fail-OPEN)
- Defensive None-Guards für `effective_chat` ergänzt

---

## Patch 2 — `cmd_api_test` Auth-Guard (Zeile 2943)

**Vorher:** Handler öffnete direkt `BitgetClient(dry_run=False)` ohne Auth-Check.

**Nachher (eingefügt vor dem ersten `reply_text`):**
```python
    if not _is_authorized(update):
        await update.message.reply_text("⛔ Nicht autorisiert.")
        return
```

**Zeile:** 2943–2945

---

## Patch 3 — `button_callback` Auth-Guard (Zeile 3335)

**Vorher:** Kein Auth-Check — alle Inline-Button-Flows (inkl. zukünftiger Live-Confirmations) für jeden erreichbar.

**Nachher (eingefügt nach `query = update.callback_query`):**
```python
    if not _is_authorized(update):
        await query.answer(text="⛔ Nicht autorisiert.", show_alert=True)
        return
```

**Zeile:** 3335–3337  
**Besonderheit:** `query.answer()` mit `show_alert=True` — Telegram zeigt dem Angreifer ein Pop-up statt einer stillen Ablehnung (kein Callback-Timeout-Error auf Client-Seite).

---

## Ergänzende Patches (Defense-in-Depth, gleiche Session)

Zusätzlich wurden Auth-Checks in allen weiteren bisher ungeschützten Handlern ergänzt:

| Handler | Neue Check-Zeile | Risikostufe |
|---------|-----------------|-------------|
| `cmd_pnl` | 2685 | MITTEL (P&L-Daten) |
| `cmd_lab` | 2701 | MITTEL (CPU + interne Daten) |
| `cmd_fetch` | 2782 | MITTEL (externe API-Calls) |

Alle Handler prüfen jetzt `_is_authorized` — 11 von 11 registrierten Handlern gesichert.

---

## py_compile Ergebnis

```
python3 -m py_compile monitor/telegram_bot.py
→ OK — kein Syntaxfehler
```

---

## ⚠️ Bot-Neustart erforderlich

Der laufende Bot-Prozess lädt `monitor/telegram_bot.py` einmalig beim Start.  
**Der Fix ist erst nach einem Neustart aktiv.**

```bash
# Prozess finden und neu starten:
systemctl restart apex-telegram-bot
# oder falls via Cron/manuell:
pkill -f telegram_bot.py && python3 -m monitor.telegram_bot &
```

Ohne Neustart bleibt der alte `_is_authorized`-Code mit `return True` im Speicher aktiv.
