import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from chengchuan.application.errors import MarketError
from chengchuan.application.market import MarketService
from chengchuan.application.sectors import rank_members
from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.sectors import SectorMember, SectorSnapshot
from chengchuan.infrastructure.dashboard_codec import decode_dashboard, encode_dashboard
from chengchuan.infrastructure.market_cache import SqliteMarketCache
from chengchuan.infrastructure.meoz_client import ApiResult
from chengchuan.infrastructure.sectors import fetch_sector

NOW = datetime(2026, 11, 4, 18, tzinfo=SHANGHAI)


class SectorTests(unittest.TestCase):
    def test_ranking_uses_change_then_signed_main_net_with_missing_last(self):
        rows = (
            SectorMember("sz002318", "a", 10, 5, -3),
            SectorMember("sh600519", "b", 10, 5, 20),
            SectorMember("sz000001", "c", 10, None, 1000),
            SectorMember("sz300001", "d", 10, 0, 0),
        )
        self.assertEqual([r.name for r in rank_members(rows)], ["b", "a", "d", "c"])

    def test_real_membership_is_joined_to_one_dated_full_market_snapshot(self):
        client = Mock()

        def query(api, params, fields="", **kwargs):
            if api == "theme_members":
                rows = (dict(theme_symbol="880491", symbols=["002318", "600519", "920001"]),)
            else:
                rows = tuple(
                    dict(
                        tradedate="20261104",
                        symbol=s,
                        name=s,
                        close=10,
                        pct_chg=5,
                        main_net_amount=n,
                    )
                    for s, n in [("002318", 0), ("600519", 100)]
                )
            return ApiResult(rows, NOW, True, "https://example.invalid")

        client.query.side_effect = query
        with patch(
            "chengchuan.infrastructure.sectors.cache_policy",
            return_value=(NOW, 86400, NOW.replace(hour=15, minute=45).timestamp()),
        ):
            result = fetch_sector(client, "hy", "880491", "半导体")
        self.assertEqual([r.symbol for r in result.members], ["sh600519", "sz002318"])
        self.assertEqual(result.members[1].main_net_yuan, 0)
        self.assertIn("其他市场1只", result.notice)
        self.assertEqual(client.query.call_count, 2)

    def test_missing_membership_is_not_guessed_by_similar_name(self):
        client = Mock()
        client.query.return_value = ApiResult(
            (dict(theme_symbol="1", symbols=["002318"]),), NOW, True, "https://example.invalid"
        )
        with self.assertRaises(MarketError):
            fetch_sector(client, "hy", "880491", "半导体")

    def test_sector_cache_roundtrip_and_offline_keeps_identity(self):
        snapshot = SectorSnapshot("hy", "880491", "半导体", NOW.date(), (), NOW, NOW)
        self.assertEqual(decode_dashboard(encode_dashboard(snapshot)), snapshot)
        with tempfile.TemporaryDirectory() as folder:
            cache = SqliteMarketCache(Path(folder) / "market.sqlite")
            cache.save_dashboard("sector", "hy:880491", snapshot)
            provider = Mock(intervals=("day",), supports_sector=True)
            service = MarketService(provider, cache, offline=True)
            self.assertTrue(service.load_sector("hy", "880491", "半导体").cached)
            provider.fetch_sector.assert_not_called()
