"""
A.4 — Lab/Live-Import-Audit-Test.

AST-Walk über core/lab_*.py und research/*.py.
Verboten: Direkte Imports der immutablen Gate-Konstanten.
Erlaubt: Versionierungs-Konstanten (OBJECTIVE_V72_VERSION, RANGES_V72_VERSION).

Bekannte Pre-existing-Ausnahmen sind in KNOWN_EXCEPTIONS dokumentiert.
Diese müssen in einem separaten Cleanup-Ticket behoben werden.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

FORBIDDEN_NAMES = {
    "RISK_USDT",
    "MAX_LEVERAGE",
    "DRAWDOWN_KILL_PCT",
    "DSR_MIN_DRY_RUN",
    "PBO_MAX",
    "STABILITY_MIN",
    "MAX_DD_GATE",
    "OOS_FOLDS_MIN_V7",
    "DAILY_DD_HALF_R",
    "DAILY_DD_KILL_R",
}

# Pre-existing Ausnahmen — dokumentiert, müssen in Folge-Cleanup behoben werden.
# Format: "relativer/pfad.py:Konstantenname"
KNOWN_EXCEPTIONS: set[str] = {
    "research/v72_objective.py:PBO_MAX",  # Benutzt PBO_MAX als Scoring-Referenz im Objective
}


def _find_forbidden_imports(filepath: Path, root: Path | None = None) -> list[str]:
    """Gibt Liste aller verbotenen Imports in einer Datei zurück."""
    try:
        tree = ast.parse(filepath.read_text())
    except SyntaxError:
        return []

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "settings" in module:
                for alias in node.names:
                    name = alias.name
                    if name in FORBIDDEN_NAMES:
                        if root:
                            try:
                                rel = str(filepath.relative_to(root))
                            except ValueError:
                                rel = str(filepath)
                        else:
                            rel = str(filepath)
                        exception_key = f"{rel}:{name}"
                        if exception_key in KNOWN_EXCEPTIONS:
                            continue
                        violations.append(
                            f"{rel}:{node.lineno}: verbotener Import '{name}' from '{module}'"
                        )
    return violations


def _collect_files() -> list[Path]:
    files = list((ROOT / "core").glob("lab_*.py"))
    research_dir = ROOT / "research"
    if research_dir.exists():
        files.extend(research_dir.glob("*.py"))
    return files


class TestLabIsolation:
    def test_no_forbidden_gate_imports_in_lab_modules(self):
        """Kein core/lab_*.py oder research/*.py darf Gate-Konstanten importieren."""
        files = _collect_files()
        assert files, "Keine Lab-Dateien gefunden — Pfad falsch?"

        all_violations = []
        for f in files:
            all_violations.extend(_find_forbidden_imports(f, root=ROOT))

        assert all_violations == [], (
            "Verbotene Gate-Imports gefunden:\n" + "\n".join(all_violations)
        )

    def test_known_exceptions_documented(self):
        """Bekannte Ausnahmen existieren tatsächlich (kein Ghost-Entry in KNOWN_EXCEPTIONS)."""
        for key in KNOWN_EXCEPTIONS:
            rel_path, const = key.split(":")
            filepath = ROOT / rel_path
            assert filepath.exists(), f"KNOWN_EXCEPTIONS referenziert nicht-existente Datei: {rel_path}"
            content = filepath.read_text()
            assert const in content, (
                f"KNOWN_EXCEPTIONS: '{const}' nicht mehr in {rel_path} — bitte entfernen"
            )

    def test_allowed_version_constants_not_blocked(self, tmp_path):
        """OBJECTIVE_V72_VERSION und RANGES_V72_VERSION dürfen importiert werden."""
        fake_module = tmp_path / "fake_lab.py"
        fake_module.write_text(
            "from config.settings import OBJECTIVE_V72_VERSION, RANGES_V72_VERSION\n"
        )
        violations = _find_forbidden_imports(fake_module, root=ROOT)
        assert violations == [], "Versionierungs-Konstanten irrtümlich als verboten markiert"

    def test_forbidden_constant_detected(self, tmp_path):
        """Sanity-Check: Test erkennt verbotenen Import korrekt."""
        fake_module = tmp_path / "bad_lab.py"
        fake_module.write_text("from config.settings import RISK_USDT\n")
        violations = _find_forbidden_imports(fake_module, root=ROOT)
        assert len(violations) == 1
        assert "RISK_USDT" in violations[0]

    def test_file_count_reasonable(self):
        """Sichert ab, dass Glob nicht leer ist (Pfad-Regression-Schutz)."""
        lab_files = list((ROOT / "core").glob("lab_*.py"))
        assert len(lab_files) >= 8, (
            f"Erwartet ≥ 8 lab_*.py Dateien, gefunden: {len(lab_files)}"
        )
