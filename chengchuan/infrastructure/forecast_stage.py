from chengchuan.domain.models import IndexRange
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine


class LegacyStageProvider:
    version = LegacyAnalysisEngine.version
    labels = ("承接／疑似吸筹", "拉升／趋势上行", "卖压／疑似派发", "回撤承接／洗盘候选")

    def scores(self, snapshot, window):
        if len(snapshot.series.bars) < window:
            return (None,) * 4
        count = len(snapshot.series.bars)
        report = LegacyAnalysisEngine().analyze(snapshot, IndexRange(count - window, count))
        scores = {s.label: s.evidence_score for s in report.findings}
        return tuple(scores.get(label) for label in self.labels)
