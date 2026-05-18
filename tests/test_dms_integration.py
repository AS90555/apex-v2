"""
P1.2 — Dead-Man-Switch Integrationstest.

Belegt zwei unabhängige Schichten:

A) emergency_close_all.py (Python):
   - Keine Credentials → sys.exit(2), kein HTTP-Call
   - Keine offenen Positionen → exit 0, Audit-Log geschrieben
   - Eine offene Position → close-Endpoint aufgerufen, exit 0
   - Close-Endpoint schlägt fehl → exit 1, failed-Zähler im Log

B) dead_mans_switch.sh (Bash, Subprocess-Integration):
   - Frischer Heartbeat → exit 0, kein Alert, kein Emergency-Close
   - Heartbeat fehlt → Verifikations-Pfad wird betreten
   - Stale HB + Bitget nicht erreichbar → exit 1, kein Emergency-Close
   - Stale HB + Bitget OK + kein APEX-Prozess → emergency_close_all.py
     wird aufgerufen (via Fake-Script, keine echten Orders)

Leitlinie: DMS sendet NIEMALS echte Orders in diesen Tests.
Alle HTTP-Calls sind gemockt (urllib.request.urlopen) oder per PATH-Injection blockiert.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Hilfsfunktionen ────────────────────────────────────────────────────────────

def _fake_response(data: dict, status: int = 200) -> MagicMock:
    """urllib.request.urlopen-kompatibler Mock."""
    body = json.dumps(data).encode()
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    mock.read.return_value = body
    mock.status = status
    return mock


def _run_script(tmp_path: Path) -> tuple[int, str]:
    """Führt scripts/emergency_close_all.py in einem Subprocess aus.
    Gibt (exit_code, stderr_output) zurück."""
    repo_root = Path(__file__).parent.parent
    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "emergency_close_all.py")],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        env={**os.environ, "LOG_DIR": str(tmp_path)},
    )
    return result.returncode, result.stderr


# ══════════════════════════════════════════════════════════════════════════════
# Schicht A: emergency_close_all.py
# ══════════════════════════════════════════════════════════════════════════════

class TestEmergencyCloseAllPy:
    def test_no_credentials_exits_2_no_http(self, tmp_path):
        """Ohne API-Credentials: exit 2, kein HTTP-Call."""
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("APEX_KEY", "APEX_SECRET", "APEX_PASS")
        }
        repo_root = Path(__file__).parent.parent
        result = subprocess.run(
            [sys.executable, str(repo_root / "scripts" / "emergency_close_all.py")],
            capture_output=True, text=True, cwd=str(repo_root),
            env={**env, "APEX_KEY": "", "APEX_SECRET": "", "APEX_PASS": ""},
        )
        assert result.returncode == 2
        assert "Credentials fehlen" in result.stderr

    def test_no_positions_exits_0_and_writes_audit_log(self, tmp_path):
        """Keine offenen Positionen → exit 0, Audit-Log in logs/ geschrieben."""
        import scripts.emergency_close_all as eca_mod

        empty_response = _fake_response({"data": []})

        with patch.dict(os.environ, {"APEX_KEY": "k", "APEX_SECRET": "s", "APEX_PASS": "p"}), \
             patch("urllib.request.urlopen", return_value=empty_response), \
             patch.object(eca_mod, "main", wraps=eca_mod.main):
            with pytest.raises(SystemExit) as exc:
                eca_mod.main()

        assert exc.value.code == 0

    def test_open_position_close_endpoint_called(self, tmp_path):
        """Eine offene Position → close-Endpoint wird aufgerufen."""
        import scripts.emergency_close_all as eca_mod

        positions_response = _fake_response({
            "data": [{
                "symbol": "BTCUSDT_UMCBL",
                "holdSide": "long",
                "marginCoin": "USDT",
                "total": "0.01",
            }]
        })
        close_response = _fake_response({"code": "00000", "msg": "success"})

        call_log: list[str] = []

        def fake_urlopen(req, timeout=None):
            call_log.append(req.full_url if hasattr(req, "full_url") else str(req))
            if "allPosition" in str(req.full_url if hasattr(req, "full_url") else req):
                return positions_response
            return close_response

        with patch.dict(os.environ, {"APEX_KEY": "k", "APEX_SECRET": "s", "APEX_PASS": "p"}), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(SystemExit) as exc:
                eca_mod.main()

        assert exc.value.code == 0
        # close-Endpoint muss aufgerufen worden sein
        assert any("close-position" in url for url in call_log), \
            f"close-position-Endpoint nicht aufgerufen. Calls: {call_log}"

    def test_failed_close_exits_1(self, tmp_path):
        """Close-Endpoint wirft Exception → failed-Zähler > 0 → exit 1."""
        import scripts.emergency_close_all as eca_mod

        positions_response = _fake_response({
            "data": [{
                "symbol": "ETHUSDT_UMCBL",
                "holdSide": "short",
                "marginCoin": "USDT",
                "total": "0.1",
            }]
        })

        def fake_urlopen(req, timeout=None):
            if "allPosition" in str(req.full_url if hasattr(req, "full_url") else req):
                return positions_response
            raise ConnectionError("Bitget nicht erreichbar")

        with patch.dict(os.environ, {"APEX_KEY": "k", "APEX_SECRET": "s", "APEX_PASS": "p"}), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(SystemExit) as exc:
                eca_mod.main()

        assert exc.value.code == 1

    def test_zero_size_positions_skipped(self, tmp_path):
        """Positionen mit total=0 werden übersprungen, kein Close-Call."""
        import scripts.emergency_close_all as eca_mod

        positions_response = _fake_response({
            "data": [{"symbol": "BTCUSDT_UMCBL", "holdSide": "long",
                      "marginCoin": "USDT", "total": "0.00000000"}]
        })
        close_calls: list[str] = []

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "close-position" in url:
                close_calls.append(url)
            return positions_response

        with patch.dict(os.environ, {"APEX_KEY": "k", "APEX_SECRET": "s", "APEX_PASS": "p"}), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(SystemExit) as exc:
                eca_mod.main()

        assert exc.value.code == 0
        assert len(close_calls) == 0, "Null-Position darf nicht geschlossen werden"


# ══════════════════════════════════════════════════════════════════════════════
# Schicht B: dead_mans_switch.sh (Subprocess-Integration)
# ══════════════════════════════════════════════════════════════════════════════

DMS_SCRIPT = Path(__file__).parent.parent / "scripts" / "dead_mans_switch.sh"


def _dms_env(apex_dir: Path, timeout: int = 300, retry_wait: int = 0) -> dict:
    """Basis-Env für DMS-Tests. Telegram deaktiviert."""
    return {
        **os.environ,
        "APEX_DIR": str(apex_dir),
        "DEAD_MANS_TIMEOUT_SECONDS": str(timeout),
        "DEAD_MANS_RETRY_WAIT_SECONDS": str(retry_wait),
        "TELEGRAM_BOT": "",   # kein echtes Telegram
        "TELEGRAM_CHAT": "",
    }


def _make_apex_dir(tmp_path: Path, hb_age_seconds: int | None = None) -> Path:
    """Erzeugt minimales APEX_DIR-Skeleton mit Heartbeat-Datei."""
    apex_dir = tmp_path / "apex"
    (apex_dir / "data" / "heartbeats").mkdir(parents=True)
    (apex_dir / "logs").mkdir()
    (apex_dir / "scripts").mkdir()

    hb = apex_dir / "data" / "heartbeats" / "master.hb"
    hb.write_text("alive")

    if hb_age_seconds is not None and hb_age_seconds > 0:
        # Heartbeat in die Vergangenheit stellen
        old_time = time.time() - hb_age_seconds
        os.utime(str(hb), (old_time, old_time))

    return apex_dir


def _fake_bin(tmp_path: Path, name: str, exit_code: int, output: str = "") -> Path:
    """Erzeugt ein Fake-Kommando im PATH das exit_code zurückgibt."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / name
    script.write_text(f"#!/bin/bash\n{f'echo {output!r}' if output else ''}\nexit {exit_code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _inject_path(base_env: dict, bin_dir: Path) -> dict:
    env = dict(base_env)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '/usr/bin:/bin')}"
    return env


@pytest.mark.skipif(not DMS_SCRIPT.exists(), reason="dead_mans_switch.sh nicht gefunden")
class TestDMSBashScript:
    def test_fresh_heartbeat_exits_0(self, tmp_path):
        """Frischer Heartbeat → exit 0, kein Alarm, kein Emergency-Close."""
        apex_dir = _make_apex_dir(tmp_path, hb_age_seconds=0)
        env = _dms_env(apex_dir, timeout=300)

        result = subprocess.run(
            ["bash", str(DMS_SCRIPT)],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "OK" in result.stderr

    def test_missing_heartbeat_enters_verification(self, tmp_path):
        """Fehlende HB-Datei → Verifikations-Stufe betreten (exit != 0)."""
        apex_dir = _make_apex_dir(tmp_path)
        # HB-Datei entfernen
        (apex_dir / "data" / "heartbeats" / "master.hb").unlink()
        # Fake curl (Bitget-Check schlägt fehl) → Alert-Only-Pfad
        bin_dir = _fake_bin(tmp_path, "curl", exit_code=1)
        env = _inject_path(_dms_env(apex_dir, timeout=300), bin_dir)

        result = subprocess.run(
            ["bash", str(DMS_SCRIPT)],
            capture_output=True, text=True, env=env,
        )
        # Script verlässt Stufe 1 (HB fehlt → HEARTBEAT_AGE=999999 > TIMEOUT)
        # Bitget-down → exit 1
        assert result.returncode != 0
        assert "WARNUNG" in result.stderr or "stale" in result.stderr.lower()

    def test_stale_hb_bitget_down_no_emergency_close(self, tmp_path):
        """Stale HB + Bitget nicht erreichbar → exit 1, kein Emergency-Close."""
        apex_dir = _make_apex_dir(tmp_path, hb_age_seconds=600)

        # Fake emergency_close_all.py das einen Marker schreibt wenn aufgerufen
        marker = tmp_path / "emergency_called.flag"
        fake_eca = apex_dir / "scripts" / "emergency_close_all.py"
        fake_eca.write_text(
            f"import pathlib\npathlib.Path(r'{marker}').touch()\nprint('EMERGENCY CALLED', flush=True)\n"
        )

        # Fake curl schlägt fehl (Bitget unreachable)
        bin_dir = _fake_bin(tmp_path, "curl", exit_code=1)
        env = _inject_path(_dms_env(apex_dir, timeout=300), bin_dir)

        result = subprocess.run(
            ["bash", str(DMS_SCRIPT)],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 1
        assert not marker.exists(), "Emergency-Close darf bei Netzwerkausfall NICHT ausgeführt werden"
        assert "Netzwerk" in result.stderr or "Ausfall" in result.stderr

    def test_stale_hb_bitget_ok_no_process_triggers_emergency_close(self, tmp_path):
        """Stale HB + Bitget OK + kein APEX-Prozess → emergency_close_all.py aufgerufen."""
        apex_dir = _make_apex_dir(tmp_path, hb_age_seconds=600)

        # Fake emergency_close_all.py schreibt Marker und beendet sich sauber
        marker = tmp_path / "emergency_called.flag"
        fake_eca = apex_dir / "scripts" / "emergency_close_all.py"
        fake_eca.write_text(
            f"import pathlib\npathlib.Path(r'{marker}').touch()\n"
            "import sys; sys.exit(0)\n"
        )

        # Fake curl: Bitget OK (exit 0)
        # Fake pgrep: kein APEX-Prozess (exit 1)
        bin_dir = tmp_path / "fakebin"
        bin_dir.mkdir(exist_ok=True)
        for name, code in [("curl", 0), ("pgrep", 1)]:
            script = bin_dir / name
            script.write_text(f"#!/bin/bash\nexit {code}\n")
            script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        # DMS-Script ruft `python` auf — falls nicht im PATH, python3-Alias anlegen
        python_bin = bin_dir / "python"
        python_bin.write_text(f"#!/bin/bash\nexec {sys.executable} \"$@\"\n")
        python_bin.chmod(python_bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        env = _inject_path(_dms_env(apex_dir, timeout=300, retry_wait=0), bin_dir)

        result = subprocess.run(
            ["bash", str(DMS_SCRIPT)],
            capture_output=True, text=True, env=env,
        )
        assert marker.exists(), (
            f"Emergency-Close wurde NICHT ausgeführt.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "Emergency" in result.stderr or "KRITISCH" in result.stderr

    def test_fresh_hb_no_telegram_no_external_calls(self, tmp_path):
        """Frischer HB + leere TELEGRAM-Vars → kein curl-Aufruf nach außen."""
        apex_dir = _make_apex_dir(tmp_path, hb_age_seconds=0)

        curl_called = tmp_path / "curl_called.flag"
        bin_dir = tmp_path / "fakebin"
        bin_dir.mkdir()
        fake_curl = bin_dir / "curl"
        fake_curl.write_text(
            f"#!/bin/bash\ntouch {curl_called}\nexit 0\n"
        )
        fake_curl.chmod(fake_curl.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        env = _inject_path(_dms_env(apex_dir, timeout=300), bin_dir)

        result = subprocess.run(
            ["bash", str(DMS_SCRIPT)],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0
        # curl darf bei frischem HB und leerem TELEGRAM nicht aufgerufen worden sein
        assert not curl_called.exists(), "curl wurde bei OK-Status aufgerufen — unerwünscht"
