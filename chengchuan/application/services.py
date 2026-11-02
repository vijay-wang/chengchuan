from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock

from chengchuan.application.ports import AnalysisEngine
from chengchuan.domain.models import AnalysisReport, IndexRange, MarketSnapshot


class AnalysisService:
    def __init__(self, engine: AnalysisEngine, cache_size: int = 32):
        if cache_size < 1:
            raise ValueError("缓存容量必须为正数")
        self.engine = engine
        self._capacity = cache_size
        self._cache: OrderedDict[tuple, AnalysisReport] = OrderedDict()
        self._lock = Lock()

    def run(self, snapshot: MarketSnapshot, selection: IndexRange) -> AnalysisReport:
        if selection.stop > len(snapshot.series.bars):
            raise ValueError("选区超出数据范围")
        if selection.count < 2:
            raise ValueError("完整区间分析至少需要两条行情")
        key = (self.engine.version, snapshot.revision, selection.start, selection.stop)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        result = self.engine.analyze(snapshot, selection)
        if result.snapshot_revision != snapshot.revision or result.selection != selection:
            raise ValueError("分析结果的数据版本或选区不一致")
        with self._lock:
            self._cache[key] = result
            self._cache.move_to_end(key)
            while len(self._cache) > self._capacity:
                self._cache.popitem(last=False)
        return result


@dataclass(frozen=True)
class RequestToken:
    generation: int
    snapshot_revision: str
    selection: IndexRange | None


class RequestGate:
    """由UI线程持有；迟到结果不得覆盖新选区。"""

    def __init__(self):
        self._generation = 0
        self._current: RequestToken | None = None

    def begin(self, revision: str, selection: IndexRange | None = None) -> RequestToken:
        self._generation += 1
        self._current = RequestToken(self._generation, revision, selection)
        return self._current

    def is_current(self, token: RequestToken) -> bool:
        return token == self._current

    def invalidate(self):
        self._generation += 1
        self._current = None
