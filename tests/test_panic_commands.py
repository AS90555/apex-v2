"""
T1.B — Tests für /panic und /panic_clear Bot-Commands.

Prüft:
- /panic: nicht autorisiert → abgewiesen
- /panic: bereits aktiver Kill-Switch → Info-Nachricht
- /panic: zeigt Bestätigungs-Button
- panic_confirm_*: falscher User → abgewiesen
- panic_confirm_*: richtiger User → set_kill_mode('hard') aufgerufen
- panic_abort: Nachricht geändert, kein set_kill_mode
- /panic_clear: nicht autorisiert → abgewiesen
- /panic_clear: ohne Argument → Fehler
- /panic_clear: kein aktiver Kill-Switch → Info
- /panic_clear: korrekter Aufruf → clear_kill_mode aufgerufen
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitor.telegram_bot import cmd_panic, cmd_panic_clear, button_callback


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_update(user_id: int = 12345) -> MagicMock:
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    update.effective_user.id = user_id
    return update


def _make_ctx(*args) -> MagicMock:
    ctx = MagicMock()
    ctx.args = list(args)
    return ctx


def _make_query_update(action: str, user_id: int = 12345) -> MagicMock:
    update = MagicMock()
    update.callback_query.data = action
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.effective_user.id = user_id
    update.message = None
    return update


class TestCmdPanic:
    def test_unauthorized_rejected(self):
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=False):
            _run(cmd_panic(update, _make_ctx()))
        msg = update.message.reply_text.call_args[0][0]
        assert "autorisiert" in msg.lower()

    def test_already_active_shows_info(self):
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("governance.kill_switch.get_kill_mode", return_value="hard"):
                _run(cmd_panic(update, _make_ctx()))
        msg = update.message.reply_text.call_args[0][0]
        assert "bereits" in msg.lower() or "aktiv" in msg.lower()

    def test_shows_confirm_button(self):
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("governance.kill_switch.get_kill_mode", return_value=None):
                _run(cmd_panic(update, _make_ctx()))
        call_kwargs = update.message.reply_text.call_args[1]
        markup = call_kwargs.get("reply_markup")
        assert markup is not None
        buttons_flat = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert any("panic_confirm_" in b for b in buttons_flat)


class TestPanicCallback:
    def test_wrong_user_rejected(self):
        update = _make_query_update(action="panic_confirm_99999", user_id=12345)
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            _run(button_callback(update, _make_ctx()))
        update.callback_query.answer.assert_awaited()

    def test_correct_user_sets_kill_switch(self):
        update = _make_query_update(action="panic_confirm_12345", user_id=12345)
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("governance.kill_switch.set_kill_mode") as mock_set:
                _run(button_callback(update, _make_ctx()))
        mock_set.assert_called_once_with(mode="hard", reason="Telegram /panic", asset=None)

    def test_abort_does_not_set_kill_switch(self):
        update = _make_query_update(action="panic_abort", user_id=12345)
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("governance.kill_switch.set_kill_mode") as mock_set:
                _run(button_callback(update, _make_ctx()))
        mock_set.assert_not_called()
        update.callback_query.edit_message_text.assert_awaited()


class TestCmdPanicClear:
    def test_unauthorized_rejected(self):
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=False):
            _run(cmd_panic_clear(update, _make_ctx()))
        msg = update.message.reply_text.call_args[0][0]
        assert "autorisiert" in msg.lower()

    def test_no_reason_rejected(self):
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            _run(cmd_panic_clear(update, _make_ctx()))
        msg = update.message.reply_text.call_args[0][0]
        assert "grund" in msg.lower() or "erforderlich" in msg.lower()

    def test_no_active_kill_switch(self):
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("governance.kill_switch.get_kill_mode", return_value=None):
                _run(cmd_panic_clear(update, _make_ctx("Netz", "stabil")))
        msg = update.message.reply_text.call_args[0][0]
        assert "kein" in msg.lower() or "nichts" in msg.lower()

    def test_successful_clear(self):
        update = _make_update(user_id=42)
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("governance.kill_switch.get_kill_mode", return_value="hard"):
                with patch("governance.kill_switch.clear_kill_mode") as mock_clear:
                    _run(cmd_panic_clear(update, _make_ctx("Netz", "wieder", "stabil")))
        mock_clear.assert_called_once_with(
            reason="Netz wieder stabil",
            cleared_by="telegram:user=42",
        )

    def test_successful_clear_confirms(self):
        update = _make_update()
        with patch("monitor.telegram_bot._is_authorized", return_value=True):
            with patch("governance.kill_switch.get_kill_mode", return_value="hard"):
                with patch("governance.kill_switch.clear_kill_mode"):
                    _run(cmd_panic_clear(update, _make_ctx("test")))
        msg = update.message.reply_text.call_args[0][0]
        assert "zurückgesetzt" in msg.lower() or "cleared" in msg.lower() or "✅" in msg
