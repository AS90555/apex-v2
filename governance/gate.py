from abc import ABC, abstractmethod
from typing import Tuple, Dict
from core.models import Signal


class BaseGovernanceCheck(ABC):
    """Ein einzelner, isolierter Governance-Check."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def evaluate(self, signal: Signal) -> Tuple[bool, str]:
        """
        Gibt (passed: bool, reason: str) zurück.
        passed=False blockiert das Signal.
        """
        ...


class GovernanceGate:
    """
    Orchestriert alle registrierten Checks für ein Signal.
    Checks werden in der registrierten Reihenfolge ausgeführt
    (Kill-Switch zuerst, teure API-Checks zuletzt).
    """

    def __init__(self, checks: list[BaseGovernanceCheck]):
        self._checks = checks

    def evaluate(self, signal: Signal) -> Tuple[bool, str, Dict]:
        """
        Führt alle Checks aus.
        Gibt (approved, reason, checks_detail) zurück.
        Bei erstem Fehler wird abgebrochen (fail-fast).
        """
        results = {}
        for check in self._checks:
            passed, reason = check.evaluate(signal)
            results[check.name] = {"passed": passed, "reason": reason}
            if not passed:
                return False, reason, results
        return True, "all_checks_passed", results
