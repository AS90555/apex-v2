from abc import ABC, abstractmethod
from typing import List
from core.models import Signal


class BaseStrategy(ABC):
    """
    Jede Strategie erzeugt ausschließlich Signale — kein Order-Code hier.
    Die Signale werden in die `signals`-Tabelle geschrieben und von
    Governance + Executor weiterverarbeitet.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Eindeutiger Bezeichner: 'orb', 'vaa', 'kdt', 'weekend_momo'"""
        ...

    @property
    @abstractmethod
    def assets(self) -> List[str]:
        """Liste der Assets, die diese Strategie überwacht."""
        ...

    @abstractmethod
    def generate_signals(self) -> List[Signal]:
        """
        Liest Daten aus der `candles`- und `features`-Tabelle,
        wendet Strategie-Logik an und gibt eine Liste von Signal-Objekten zurück.
        Schreibt die Signale selbst in die DB (status='pending').
        Sendet keine Orders.
        """
        ...

    def run(self) -> List[Signal]:
        """Entry-Point für run_strategies.py — ergänzt Logging."""
        from core.utils import log
        log(f"[{self.name.upper()}] generate_signals() gestartet für {self.assets}")
        signals = self.generate_signals()
        log(f"[{self.name.upper()}] {len(signals)} Signal(e) erzeugt")
        return signals
