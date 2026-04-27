from abc import ABC, abstractmethod
from typing import Optional
from core.models import Signal, Trade


class BaseExecutor(ABC):
    @abstractmethod
    def execute(self, signal: Signal) -> Optional[Trade]:
        ...
