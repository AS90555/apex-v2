"""
Lab-Search-Konfiguration (v7 Phase 3).

Versioniert alle Optuna-Parameter (TPE-Seed, Pruner, Suchräume) in einer
einzigen hashbaren Datenklasse. Gleiche Konfiguration → gleicher Hash →
reproduzierbare Studien. Jede Änderung an Suchraum oder Sampler erzeugt
automatisch einen neuen Hash, der in lab_discoveries.lab_config_hash
gespeichert wird.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LabSearchConfig:
    version:          str = "v7.0"
    tpe_seed:         int = 42
    tpe_n_startup:    int = 5
    pruner_type:      str = "MedianPruner"
    pruner_n_startup: int = 5
    pruner_n_warmup:  int = 1

    def hash(self) -> str:
        """SHA256-Hash über alle Felder (deterministisch, sorted keys)."""
        payload = {
            "version":          self.version,
            "tpe_seed":         self.tpe_seed,
            "tpe_n_startup":    self.tpe_n_startup,
            "pruner_type":      self.pruner_type,
            "pruner_n_startup": self.pruner_n_startup,
            "pruner_n_warmup":  self.pruner_n_warmup,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode()).hexdigest()

    def short_hash(self) -> str:
        """Erste 8 Zeichen des Hash — für Study-Namen."""
        return self.hash()[:8]

    def build_sampler(self):
        """Erzeugt einen konfigurierten TPE-Sampler."""
        import optuna
        return optuna.samplers.TPESampler(
            seed=self.tpe_seed,
            n_startup_trials=self.tpe_n_startup,
        )

    def build_pruner(self):
        """Erzeugt den konfigurierten Pruner."""
        import optuna
        if self.pruner_type == "MedianPruner":
            return optuna.pruners.MedianPruner(
                n_startup_trials=self.pruner_n_startup,
                n_warmup_steps=self.pruner_n_warmup,
            )
        if self.pruner_type == "HyperbandPruner":
            return optuna.pruners.HyperbandPruner()
        raise ValueError(f"Unbekannter Pruner-Typ: {self.pruner_type}")


# Einzige aktive Instanz — module-level Singleton.
# Änderungen hier erzeugen automatisch einen neuen Hash.
LAB_SEARCH_CFG = LabSearchConfig()
