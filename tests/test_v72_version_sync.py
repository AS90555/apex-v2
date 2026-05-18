"""
B.2 — RANGES_V72_VERSION-Sync-Test.

Prüft dass RANGES_V72_VERSION, ranges_v72_hash, OBJECTIVE_V72_VERSION und
lab_search_cfg_hash mit dem eingefrorenen Snapshot in tests/_snapshots/v72_version.json
übereinstimmen.

Schlägt fehl wenn:
  - RANGES_V72 (Search-Space-Ranges) geändert wurde ohne RANGES_V72_VERSION zu bumpen.
  - RANGES_V72_VERSION geändert wurde ohne den Snapshot zu aktualisieren.
  - OBJECTIVE_V72_VERSION oder LAB_SEARCH_CFG geändert wurde ohne Snapshot-Update.

Bei legitimem Bump:
  1. Version in research/v72_search_space.py oder config/settings.py erhöhen.
  2. Snapshot neu generieren:
       python3 -c "
         from research.v72_search_space import RANGES_V72_VERSION, ranges_v72_hash
         from config.settings import OBJECTIVE_V72_VERSION
         from research.lab_search_config import LAB_SEARCH_CFG
         import json
         print(json.dumps({
             'RANGES_V72_VERSION': RANGES_V72_VERSION,
             'ranges_v72_hash': ranges_v72_hash(),
             'OBJECTIVE_V72_VERSION': OBJECTIVE_V72_VERSION,
             'lab_search_cfg_hash': LAB_SEARCH_CFG.hash(),
         }, indent=2))
       "
  3. Ausgabe in tests/_snapshots/v72_version.json eintragen.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

SNAPSHOT_PATH = Path(__file__).parent / "_snapshots" / "v72_version.json"


def _load_snapshot() -> dict:
    assert SNAPSHOT_PATH.exists(), (
        f"Snapshot fehlt: {SNAPSHOT_PATH}. "
        "Erstelle ihn mit dem Befehl im Modul-Docstring."
    )
    return json.loads(SNAPSHOT_PATH.read_text())


class TestV72VersionSync:
    def test_ranges_version_matches_snapshot(self):
        """RANGES_V72_VERSION stimmt mit Snapshot überein."""
        from research.v72_search_space import RANGES_V72_VERSION
        snap = _load_snapshot()
        assert RANGES_V72_VERSION == snap["RANGES_V72_VERSION"], (
            f"RANGES_V72_VERSION ist '{RANGES_V72_VERSION}', "
            f"Snapshot erwartet '{snap['RANGES_V72_VERSION']}'. "
            "→ Snapshot neu generieren (siehe Modul-Docstring)."
        )

    def test_ranges_hash_matches_snapshot(self):
        """ranges_v72_hash() stimmt mit Snapshot überein — erkennt stille Range-Änderungen."""
        from research.v72_search_space import ranges_v72_hash, RANGES_V72_VERSION
        snap = _load_snapshot()
        current_hash = ranges_v72_hash()
        assert current_hash == snap["ranges_v72_hash"], (
            f"ranges_v72_hash hat sich geändert (aktuell: {current_hash[:16]}…, "
            f"Snapshot: {snap['ranges_v72_hash'][:16]}…). "
            f"RANGES_V72_VERSION ist '{RANGES_V72_VERSION}'. "
            "→ RANGES_V72_VERSION bumpen UND Snapshot neu generieren."
        )

    def test_objective_version_matches_snapshot(self):
        """OBJECTIVE_V72_VERSION stimmt mit Snapshot überein."""
        from config.settings import OBJECTIVE_V72_VERSION
        snap = _load_snapshot()
        assert OBJECTIVE_V72_VERSION == snap["OBJECTIVE_V72_VERSION"], (
            f"OBJECTIVE_V72_VERSION ist '{OBJECTIVE_V72_VERSION}', "
            f"Snapshot erwartet '{snap['OBJECTIVE_V72_VERSION']}'. "
            "→ Snapshot neu generieren (siehe Modul-Docstring)."
        )

    def test_lab_search_cfg_hash_matches_snapshot(self):
        """LAB_SEARCH_CFG.hash() stimmt mit Snapshot überein."""
        from research.lab_search_config import LAB_SEARCH_CFG
        snap = _load_snapshot()
        current_hash = LAB_SEARCH_CFG.hash()
        assert current_hash == snap["lab_search_cfg_hash"], (
            f"LAB_SEARCH_CFG.hash() hat sich geändert "
            f"(aktuell: {current_hash[:16]}…, Snapshot: {snap['lab_search_cfg_hash'][:16]}…). "
            "→ Snapshot neu generieren (siehe Modul-Docstring)."
        )

    def test_drift_detection_works(self, tmp_path):
        """Sanity-Check: manipulierter Snapshot wird als Drift erkannt."""
        fake_snap = tmp_path / "v72_version.json"
        fake_snap.write_text(json.dumps({
            "RANGES_V72_VERSION": "0.0",
            "ranges_v72_hash": "deadbeef",
            "OBJECTIVE_V72_VERSION": "v0.0",
            "lab_search_cfg_hash": "deadbeef",
        }))
        from research.v72_search_space import RANGES_V72_VERSION
        snap = json.loads(fake_snap.read_text())
        assert RANGES_V72_VERSION != snap["RANGES_V72_VERSION"], (
            "Test-Voraussetzung: aktuelle Version unterscheidet sich von manipuliertem Snapshot"
        )
