from chengchuan.domain.market import is_index
from chengchuan.infrastructure.legacy import legacy_dataset
from flowradar.core import analyze


class LegacyStageProvider:
    version = "flowradar-heuristic-v0.1-unvalidated/adapter-2"
    labels = ("承接／疑似吸筹", "拉升／趋势上行", "卖压／疑似派发", "回撤承接／洗盘候选")

    def scores(self, snapshot, window):
        if (
            len(snapshot.series.bars) < window
            or is_index(snapshot.series.symbol)
            or snapshot.series.interval not in ("day", "minute")
        ):
            return (None,) * 4
        data = legacy_dataset(snapshot)
        report = analyze(data, data.bars[-window].timestamp, data.bars[-1].timestamp)
        scores = {s["name"]: s["evidence_score"] for s in report["stages"]}
        return tuple(scores.get(label) for label in self.labels)
