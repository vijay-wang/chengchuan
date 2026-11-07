import ast
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from chengchuan.application.errors import StorageError
from chengchuan.application.services import AnalysisService, RequestGate
from chengchuan.domain.models import (
    SHANGHAI,
    AnalysisReport,
    IndexRange,
    MarketBar,
    MarketSeries,
    MarketSnapshot,
    SeriesKind,
)
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine, LegacyJsonRepository
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository

ROOT = Path(__file__).resolve().parents[1]


def sample_snapshot():
    bars = tuple(
        MarketBar(datetime(2026, 11, i, 15, tzinfo=SHANGHAI), 10 + i, 100) for i in range(1, 5)
    )
    return MarketSnapshot(
        "revision-1", MarketSeries("sz002318", "样本", "minute", SeriesKind.SAMPLED_CLOSE, bars)
    )


class FoundationTests(unittest.TestCase):
    def test_selection_is_half_open_clamped_and_direction_independent(self):
        self.assertEqual(IndexRange.from_drag(6, 2, 10), IndexRange(2, 7))
        self.assertEqual(IndexRange.from_drag(-10, 200, 10), IndexRange(0, 10))
        self.assertEqual(IndexRange.from_drag(4, 4, 10).count, 1)
        for a, b in [(-1, 2), (2, 2), (3, 2)]:
            with self.assertRaises(ValueError):
                IndexRange(a, b)

    def test_domain_requires_timezone_and_real_ohlc(self):
        with self.assertRaises(ValueError):
            MarketBar(datetime(2026, 11, 2), 10, 100)
        snapshot = sample_snapshot()
        with self.assertRaises(ValueError):
            MarketSeries("x", "x", "day", SeriesKind.OHLC, snapshot.series.bars)
        candle = MarketBar(datetime(2026, 11, 2, tzinfo=SHANGHAI), 10, 100, open=9, high=11, low=8)
        with self.assertRaises(ValueError):
            MarketSeries("x", "x", "minute", SeriesKind.SAMPLED_CLOSE, (candle,))

    def test_duplicate_or_reversed_data_are_rejected(self):
        bars = sample_snapshot().series.bars
        for rows in [(bars[0], bars[0]), tuple(reversed(bars))]:
            with self.assertRaises(ValueError):
                MarketSeries("x", "x", "minute", SeriesKind.SAMPLED_CLOSE, rows)

    def test_source_list_mutation_cannot_change_snapshot(self):
        original = list(sample_snapshot().series.bars)
        series = MarketSeries("x", "x", "minute", SeriesKind.SAMPLED_CLOSE, original)
        original.clear()
        self.assertEqual(len(series.bars), 4)
        with self.assertRaises(ValueError):
            IndexRange(1.5, 3)

    def test_mismatched_engine_result_is_rejected(self):
        class Engine:
            version = "bad"

            def analyze(self, snapshot, selection):
                return AnalysisReport("wrong-data", selection, "test", (), (), (), self.version)

        with self.assertRaisesRegex(ValueError, "数据版本或选区"):
            AnalysisService(Engine()).run(sample_snapshot(), IndexRange(0, 2))

    def test_cache_is_bounded_and_engine_version_is_in_key(self):
        class Engine:
            version = "one"
            calls = 0

            def analyze(self, snapshot, selection):
                self.calls += 1
                return AnalysisReport(
                    snapshot.revision, selection, "test", (), (), (), self.version
                )

        engine = Engine()
        service = AnalysisService(engine, cache_size=1)
        snapshot = sample_snapshot()
        a, b = IndexRange(0, 2), IndexRange(1, 3)
        service.run(snapshot, a)
        service.run(snapshot, a)
        self.assertEqual(engine.calls, 1)
        service.run(snapshot, b)
        service.run(snapshot, a)
        self.assertEqual(engine.calls, 3)
        engine.version = "two"
        self.assertEqual(service.run(snapshot, a).model_version, "two")
        self.assertEqual(engine.calls, 4)

    def test_invalid_selection_never_reaches_engine(self):
        service = AnalysisService(LegacyAnalysisEngine())
        for selection in (IndexRange(0, 1), IndexRange(0, 99)):
            with self.assertRaises(ValueError):
                service.run(sample_snapshot(), selection)

    def test_late_results_are_invalid_after_new_selection_or_clear(self):
        gate = RequestGate()
        first = gate.begin("same", IndexRange(0, 2))
        second = gate.begin("same", IndexRange(0, 2))
        self.assertFalse(gate.is_current(first))
        self.assertTrue(gate.is_current(second))
        gate.invalidate()
        self.assertFalse(gate.is_current(second))

    def test_workspace_transaction_and_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workspace.sqlite"
            repo = SqliteWorkspaceRepository(path)
            name = "研究'; DROP TABLE workspaces; --"
            repo.save(name, {"selection": [1, 4], "title": "澄川"})
            with self.assertRaises(ValueError):
                repo.save(name, {"bad": float("nan")})
            restored = SqliteWorkspaceRepository(path).load(name)
            self.assertEqual(restored, {"selection": [1, 4], "title": "澄川"})
            self.assertEqual(repo.load("missing"), {})

    def test_corrupt_workspace_is_reported_and_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workspace.sqlite"
            path.write_bytes(b"invalid database")
            with self.assertRaises(StorageError):
                SqliteWorkspaceRepository(path)
            self.assertEqual(path.read_bytes(), b"invalid database")

    def test_legacy_minute_import_never_creates_fake_candles(self):
        path = ROOT / "reports/jiuli_20261102_full_day.dataset.json"
        snapshot = LegacyJsonRepository().load(path)
        self.assertEqual(snapshot.series.kind, SeriesKind.SAMPLED_CLOSE)
        self.assertTrue(all(b.open is None and b.high is None for b in snapshot.series.bars))
        self.assertEqual(len(snapshot.revision), 64)

    def test_legacy_adapter_keeps_saved_report_metrics(self):
        import json

        snapshot = LegacyJsonRepository().load(ROOT / "reports/jiuli_20261019_1102.dataset.json")
        selected = [
            i
            for i, b in enumerate(snapshot.series.bars)
            if b.timestamp.day >= 19 and b.timestamp.month == 10 or b.timestamp.month == 11
        ]
        selection = IndexRange(selected[0], selected[-1] + 1)
        with patch("flowradar.core.analyze") as classifier:
            report = AnalysisService(LegacyAnalysisEngine()).run(snapshot, selection)
        classifier.assert_not_called()
        prior = json.loads((ROOT / "reports/jiuli_20261019_1102.json").read_text())
        metrics = {m.key: m for m in report.metrics}
        for key, prior_key in (
            ("return", "return_pct"),
            ("amount", "amount_yuan"),
            ("volume", "volume_shares"),
            ("vwap", "vwap"),
            ("relative", "relative_return_pct"),
        ):
            self.assertAlmostEqual(metrics[key].value, prior["metrics"][prior_key])
        self.assertIsNone(metrics["institutional"].value)
        self.assertEqual(report.findings, ())
        self.assertIn("区间基础统计", report.summary)
        self.assertFalse(any("阶段" in note for note in report.notes))

    def test_interval_removal_keeps_legacy_forecast_stage_inputs(self):
        import json

        from chengchuan.infrastructure.forecast_stage import LegacyStageProvider

        snapshot = LegacyJsonRepository().load(ROOT / "reports/jiuli_20261019_1102.dataset.json")
        prior = json.loads((ROOT / "reports/jiuli_20261019_1102.json").read_text())
        provider = LegacyStageProvider()
        expected = {s["name"]: s["evidence_score"] for s in prior["stages"]}
        self.assertEqual(
            provider.scores(snapshot, prior["observations"]),
            tuple(expected[label] for label in provider.labels),
        )

    def test_dependency_boundaries(self):
        forbidden = {
            "domain": (
                "chengchuan.application",
                "chengchuan.infrastructure",
                "chengchuan.ui",
                "flowradar",
                "PySide6",
                "pyqtgraph",
                "sqlite3",
                "urllib",
            ),
            "application": (
                "chengchuan.infrastructure",
                "chengchuan.ui",
                "flowradar",
                "PySide6",
                "pyqtgraph",
                "sqlite3",
            ),
            "ui": ("chengchuan.infrastructure", "flowradar"),
        }
        for layer, prefixes in forbidden.items():
            for path in (ROOT / "chengchuan" / layer).glob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                names = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names.extend(n.name for n in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        names.append(node.module)
                for name in names:
                    self.assertFalse(
                        any(name == p or name.startswith(p + ".") for p in prefixes),
                        f"{path.name} violates {layer} boundary: {name}",
                    )


if __name__ == "__main__":
    unittest.main()
