from pathlib import Path
from typing import Any, Protocol

from chengchuan.domain.models import AnalysisReport, IndexRange, MarketSnapshot


class SnapshotRepository(Protocol):
    def load(self, path: Path) -> MarketSnapshot: ...


class AnalysisEngine(Protocol):
    @property
    def version(self) -> str: ...

    def analyze(self, snapshot: MarketSnapshot, selection: IndexRange) -> AnalysisReport: ...


class WorkspaceRepository(Protocol):
    def load(self, name: str) -> dict[str, Any]: ...

    def save(self, name: str, state: dict[str, Any]) -> None: ...
