import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from chengchuan.application.errors import MarketError
from chengchuan.application.market import MarketService
from chengchuan.application.market_views import layout_tiles
from chengchuan.domain.market_views import HeatmapSnapshot, HeatTile
from chengchuan.domain.models import SHANGHAI
from chengchuan.infrastructure.dashboard_codec import decode_dashboard, encode_dashboard
from chengchuan.infrastructure.market_cache import SqliteMarketCache
from chengchuan.infrastructure.market_views import (
    fetch_heatmap,
    fetch_public_flow,
    parse_public_flow,
)
from chengchuan.infrastructure.meoz_client import ApiResult

NOW = datetime(2026, 11, 4, 20, tzinfo=SHANGHAI)


def flow_payload(code="002318", exchange=0):
    return dict(
        rc=0,
        data=dict(
            diff=[
                dict(
                    f12=code,
                    f13=exchange,
                    f14="测试证券",
                    f124=int(NOW.replace(hour=15).timestamp()),
                    f62=3,
                    f64=20,
                    f65=10,
                    f66=10,
                    f70=10,
                    f71=17,
                    f72=-7,
                    f76=40,
                    f77=45,
                    f78=-5,
                    f82=0,
                    f83=0,
                    f84=0,
                )
            ]
        ),
    )


def heat_snapshot(kind="hy"):
    return HeatmapSnapshot(
        kind,
        NOW.date(),
        tuple(
            HeatTile(
                str(i), f"行业{i}", i - 2, 100 * (i + 1), "sh600519" if kind == "stocks" else None
            )
            for i in range(5)
        ),
        NOW,
        "模拟源",
    )


class MarketViewsTests(unittest.TestCase):
    def test_flow_units_signs_zero_and_order_size_groups_are_kept_separate(self):
        result = parse_public_flow(flow_payload(), "002318", NOW)
        buckets = {b.key: b for b in result.buckets}
        self.assertEqual(
            (buckets["main"].inflow, buckets["main"].outflow, buckets["main"].net), (30, 27, 3)
        )
        self.assertEqual(
            (buckets["retail"].inflow, buckets["retail"].outflow, buckets["retail"].net),
            (40, 45, -5),
        )
        self.assertEqual(buckets["small"].inflow, 0)
        self.assertIn("非真实账户", result.notice)

    def test_missing_flow_never_becomes_zero_or_a_guessed_opposite_net(self):
        raw = flow_payload()
        raw["data"]["diff"][0]["f82"] = "-"
        result = parse_public_flow(raw, "002318", NOW)
        buckets = {b.key: b for b in result.buckets}
        self.assertIsNone(buckets["small"].inflow)
        self.assertIsNone(buckets["retail"].inflow)
        self.assertEqual(buckets["retail"].net, -5)

    def test_market_sum_requires_both_exchanges_and_the_same_date(self):
        raw = flow_payload("000001", 1)
        raw["data"]["diff"] += flow_payload("399001", 0)["data"]["diff"]
        result = parse_public_flow(raw, "market:shsz", NOW)
        self.assertEqual(result.buckets[0].inflow, 60)
        with self.assertRaises(MarketError):
            parse_public_flow(flow_payload("000001", 1), "market:shsz", NOW)
        raw["data"]["diff"][1]["f124"] -= 86400
        with self.assertRaises(MarketError):
            parse_public_flow(raw, "market:shsz", NOW)

    def test_public_request_uses_verified_ulist_contract_and_no_private_credential(self):
        with patch(
            "chengchuan.infrastructure.market_views.http_text",
            return_value=json.dumps(flow_payload()),
        ) as http:
            result = fetch_public_flow("002318")
        self.assertEqual(result.symbol, "sz002318")
        url = http.call_args.args[0]
        self.assertIn("/ulist.np/get?", url)
        self.assertIn("secids=0.002318", url)
        self.assertNotIn("apikey", url)
        with self.assertRaises(MarketError):
            parse_public_flow(flow_payload(), "600519", NOW)
        with self.assertRaises(MarketError):
            parse_public_flow([], "002318", NOW)

    def test_treemap_area_matches_amount_and_filtering_is_bounded(self):
        snapshot = heat_snapshot()
        rects = layout_tiles(snapshot.tiles, 800, 600)
        self.assertEqual(len(rects), 5)
        self.assertAlmostEqual(sum(r.width * r.height for r in rects), 800 * 600)
        total = sum(t.amount_yuan for t in snapshot.tiles)
        for rect in rects:
            self.assertAlmostEqual(
                rect.width * rect.height / (800 * 600), rect.tile.amount_yuan / total
            )
            self.assertGreaterEqual(rect.x, 0)
            self.assertLessEqual(rect.x + rect.width, 800.0001)
        self.assertEqual(len(layout_tiles(snapshot.tiles, 800, 600, 2)), 2)
        self.assertEqual(
            layout_tiles((replace(snapshot.tiles[0], amount_yuan=None),), 800, 600), ()
        )

    def test_heatmap_contract_and_stock_scope_reject_bad_batches(self):
        client = Mock()
        client.peek.return_value = None
        rows = (
            dict(
                tradedate="20261104", symbol="880301", name="煤炭", type="hy", pct_chg=0, amount=100
            ),
        )
        client.query.return_value = ApiResult(rows, NOW, False, "https://example.invalid")
        result = fetch_heatmap(client, "hy")
        self.assertEqual(result.tiles[0].change_pct, 0)
        self.assertEqual(client.query.call_args.args[0], "theme_daily")
        self.assertEqual(client.query.call_args.args[1], dict(type="hy"))
        client.query.return_value = ApiResult(
            (*rows, dict(rows[0], tradedate="20261103")), NOW, False, "https://example.invalid"
        )
        with self.assertRaises(MarketError):
            fetch_heatmap(client, "hy")
        client.query.return_value = ApiResult(
            tuple(rows * 6000), NOW, False, "https://example.invalid"
        )
        with self.assertRaises(MarketError):
            fetch_heatmap(client, "hy")

    def test_new_view_cache_roundtrip_identity_and_offline_replay(self):
        heat = heat_snapshot()
        flow = parse_public_flow(flow_payload(), "002318", NOW)
        for value in (heat, flow):
            self.assertEqual(decode_dashboard(encode_dashboard(value)), value)
        with tempfile.TemporaryDirectory() as directory:
            cache = SqliteMarketCache(Path(directory) / "market.sqlite")
            cache.save_dashboard("heatmap", "hy", heat)
            cache.save_dashboard("funds", "sz002318", flow)
            provider = Mock(intervals=("day",), supports_heatmap=True, supports_funds=True)
            service = MarketService(provider, cache, offline=True)
            self.assertTrue(service.load_heatmap().cached)
            self.assertTrue(service.load_funds("002318").cached)
            provider.fetch_funds.assert_not_called()
            provider.fetch_heatmap.assert_not_called()
            with self.assertRaises(MarketError):
                service.load_funds("600519")
