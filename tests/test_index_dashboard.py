import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from test_market import sample_frame

from chengchuan.application.errors import MarketError
from chengchuan.application.market import MarketService
from chengchuan.application.quote_metrics import (
    aggregate_breadth,
    base_metrics,
    details_from_frame,
    trading_minutes,
    volume_ratio,
)
from chengchuan.domain.market import Instrument, instrument_symbol, stock_symbol
from chengchuan.domain.market_dashboard import (
    IndexSummary,
    MarketOverview,
    QuoteDetails,
    QuoteMetric,
)
from chengchuan.domain.models import SHANGHAI, MarketBar
from chengchuan.infrastructure.dashboard_codec import decode_dashboard, encode_dashboard
from chengchuan.infrastructure.market_cache import SqliteMarketCache
from chengchuan.infrastructure.market_data import TencentMarketProvider, parse_quote_details
from chengchuan.infrastructure.meoz_dashboard import snapshot_time
from chengchuan.infrastructure.meoz_market import parse_rows

NOW = datetime(2026, 11, 4, 18, tzinfo=SHANGHAI)


def details(symbol="sz002318", as_of=None, complete=True):
    return QuoteDetails(
        symbol,
        date(2026, 11, 4),
        (QuoteMetric("volume_ratio", "量比", 0, "倍"),),
        NOW,
        as_of,
        complete=complete,
    )


class IndexDashboardTests(unittest.TestCase):
    def test_explicit_index_identity_never_replaces_bank_for_bare_code(self):
        self.assertEqual(instrument_symbol("000001"), "sz000001")
        self.assertEqual(instrument_symbol("sh000001"), "index:sh000001")
        self.assertEqual(instrument_symbol("index:sz399001"), "index:sz399001")
        self.assertEqual(Instrument("index:sh000001", "上证指数").symbol, "index:sh000001")
        with self.assertRaises(ValueError):
            stock_symbol("sh000001")
        with self.assertRaises(ValueError):
            instrument_symbol("index:sh123456")

    def test_price_amplitude_units_and_missing_values_do_not_turn_into_zero(self):
        row = dict(close=18, pre_close=20, open=19, high=21, low=17, vol=0, amount=None)
        metrics = {m.key: m for m in base_metrics("sz002318", row, "test")}
        self.assertEqual(metrics["pct_chg"].value, -10)
        self.assertEqual(metrics["amplitude"].value, 20)
        self.assertEqual(metrics["volume"].value, 0)
        self.assertIsNone(metrics["amount"].value)
        index = {m.key: m for m in base_metrics("index:sh000001", dict(row, vol=2), "test")}
        self.assertEqual(index["close"].unit, "点")
        self.assertEqual(index["volume"].value, 200)
        unknown = {m.key: m for m in base_metrics("sz002318", dict(row, pre_close=None), "test")}
        self.assertIsNone(unknown["pct_chg"].value)
        invalid = {m.key: m for m in base_metrics("sz002318", dict(row, pre_close=0), "test")}
        self.assertIsNone(invalid["pre_close"].value)
        for changes, missing in (
            (dict(pre_close=-1), ("pre_close", "change", "pct_chg", "amplitude")),
            (dict(close=0), ("close", "change", "pct_chg")),
            (dict(high=16), ("amplitude",)),
            (dict(low=-1), ("low", "amplitude")),
        ):
            metrics = {
                m.key: m.value for m in base_metrics("sz002318", dict(row, **changes), "test")
            }
            for key in missing:
                self.assertIsNone(metrics[key])

    def test_volume_ratio_uses_preceding_five_sessions_and_excludes_lunch(self):
        for h, m, expected in (
            (9, 30, 0),
            (11, 30, 120),
            (12, 30, 120),
            (13, 0, 120),
            (14, 35, 215),
            (15, 30, 240),
        ):
            self.assertEqual(trading_minutes(NOW.replace(hour=h, minute=m)), expected)
        bars = tuple(MarketBar(NOW - timedelta(days=6 - i), 10, 2400) for i in range(6))
        bars = (*bars[:-1], replace(bars[-1], volume_shares=2150))
        self.assertAlmostEqual(volume_ratio(bars, NOW.replace(hour=14, minute=35)), 1)
        self.assertIsNone(volume_ratio(bars, None))
        self.assertAlmostEqual(volume_ratio(bars, None, True), 2150 / 2400)
        self.assertIsNone(volume_ratio(bars[:5], NOW))
        self.assertIsNone(volume_ratio((replace(bars[0], volume_shares=0), *bars[1:]), NOW))

    def test_weekly_bar_amount_and_high_are_not_presented_as_daily_metrics(self):
        frame = sample_frame()
        frame = replace(
            frame,
            snapshot=replace(
                frame.snapshot, series=replace(frame.snapshot.series, interval="week")
            ),
        )
        metrics = {m.key: m.value for m in details_from_frame(frame).metrics}
        self.assertIsNone(metrics["high"])
        self.assertIsNone(metrics["volume"])
        self.assertIsNone(metrics["amount"])
        self.assertIsNotNone(metrics["close"])

    def test_breadth_preserves_zero_unknowns_partial_money_and_board_scope(self):
        rows = [
            dict(tradedate="20261104", symbol=c[:6], code=c, pct_chg=p, main_net_amount=f)
            for c, p, f in (
                ("600001.SH", 1, 10),
                ("000001.SZ", -1, None),
                ("300001.SZ", 0, 0),
                ("688001.SH", None, -5),
            )
        ]
        scopes = {b.scope: b for b in aggregate_breadth(rows, NOW.date())}
        all_a = scopes["all"]
        self.assertEqual((all_a.up, all_a.down, all_a.flat, all_a.unknown), (1, 1, 1, 1))
        self.assertEqual((all_a.flow_covered, all_a.total, all_a.main_net_yuan), (3, 4, 5))
        self.assertEqual(scopes["growth"].flat, 1)
        self.assertEqual(scopes["star"].unknown, 1)
        with self.assertRaises(ValueError):
            aggregate_breadth(rows + [rows[0]], NOW.date())
        with self.assertRaises(ValueError):
            aggregate_breadth(rows, date(2026, 11, 3))

    def test_index_rows_validate_exchange_and_do_not_guess_historical_amount_units(self):
        row = dict(
            tradedate="20261104",
            symbol="000001.SH",
            open=10,
            high=11,
            low=9,
            close=10,
            vol=2,
            amount=1000,
        )
        bars, _ = parse_rows([row], "index:sh000001")
        self.assertEqual((bars[0].volume_shares, bars[0].amount_yuan), (200, 1000))
        bare, _ = parse_rows([dict(row, symbol="000001")], "index:sh000001")
        self.assertIsNone(bare[0].amount_yuan)
        with self.assertRaises(MarketError):
            parse_rows([dict(row, symbol="000001.SZ")], "index:sh000001")

    def test_source_times_accept_epoch_or_clock_but_never_invent_a_date(self):
        stamp = snapshot_time(
            dict(tradedate="20261104", servertime=str(int(NOW.timestamp() * 1000)))
        )
        self.assertEqual(stamp, NOW)
        self.assertEqual(
            snapshot_time(dict(tradedate="20261104", servertime="15:00:00.789")).microsecond, 789000
        )
        self.assertIsNone(
            snapshot_time(dict(tradedate="20261103", servertime=str(int(NOW.timestamp() * 1000))))
        )
        self.assertIsNone(snapshot_time(dict(tradedate="20261104", servertime="invalid")))

    def test_quote_detail_and_overview_cache_roundtrip_and_identity_isolation(self):
        index = IndexSummary("index:sh000001", "上证指数", NOW.date(), 100, 99, 1000, NOW)
        overview = MarketOverview((index,), (), NOW)
        self.assertEqual(decode_dashboard(encode_dashboard(overview)), overview)
        value = details()
        self.assertEqual(decode_dashboard(encode_dashboard(value)), value)
        with tempfile.TemporaryDirectory() as folder:
            cache = SqliteMarketCache(Path(folder) / "cache.sqlite")
            cache.save_dashboard("overview", "latest", overview)
            cache.save_dashboard("details", "sz000001@latest", details("sz000001"))
            cache.save_dashboard("details", "index:sh000001@latest", details("index:sh000001"))
            self.assertEqual(cache.load_dashboard("overview", "latest"), overview)
            self.assertEqual(cache.load_dashboard("details", "sz000001@latest").symbol, "sz000001")
            self.assertEqual(
                cache.load_dashboard("details", "index:sh000001@latest").symbol, "index:sh000001"
            )

    def test_incomplete_cache_is_retried_but_completed_postclose_cache_is_reused(self):
        provider = Mock(intervals=("day",), supports_details=True)
        cache = Mock()
        partial = replace(details(), retrieved_at=NOW - timedelta(minutes=20), complete=False)
        full = replace(partial, complete=True)
        cache.load_dashboard.return_value = partial
        provider.fetch_details.return_value = details()
        service = MarketService(provider, cache, ttl_seconds=180)
        with patch("chengchuan.application.market.datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            service.load_details("002318")
            provider.fetch_details.assert_called_once()
            provider.fetch_details.reset_mock()
            cache.load_dashboard.return_value = full
            self.assertTrue(service.load_details("002318").cached)
            provider.fetch_details.assert_not_called()

    def test_wrong_details_scope_and_future_requests_are_rejected(self):
        provider = Mock(intervals=("day",), supports_details=True)
        provider.fetch_details.return_value = details("sz000001")
        cache = Mock()
        cache.load_dashboard.return_value = None
        service = MarketService(provider, cache)
        with self.assertRaises(MarketError):
            service.load_details("002318")
        cache.save_dashboard.assert_not_called()
        with self.assertRaises(ValueError):
            service.load_details("002318", as_of=datetime.now(SHANGHAI).date() + timedelta(days=1))
        self.assertEqual(provider.fetch_details.call_count, 1)

    def test_network_failure_retains_original_snapshot_timestamp(self):
        provider = Mock(intervals=("day",), supports_details=True)
        old = replace(details(), retrieved_at=NOW - timedelta(days=1))
        provider.fetch_details.side_effect = OSError("offline")
        cache = Mock()
        cache.load_dashboard.return_value = old
        result = MarketService(provider, cache).load_details("002318", refresh=True)
        self.assertEqual(result.retrieved_at, old.retrieved_at)
        self.assertTrue(result.cached)

    def test_historical_details_and_numeric_contracts_reject_invalid_data(self):
        with self.assertRaises(ValueError):
            details(as_of=date(2026, 11, 3))
        with self.assertRaises(ValueError):
            QuoteMetric("x", "x", float("nan"))
        with self.assertRaises(ValueError):
            IndexSummary("index:sh000001", "上证", NOW.date(), 0, 10)

    @staticmethod
    def public_quote(symbol="sz002318"):
        fields = [""] * 88
        values = {
            1: "测试股票",
            2: symbol[-6:],
            3: "11",
            4: "10",
            5: "10.5",
            6: "2",
            30: "20261104150000",
            33: "12",
            34: "9",
            37: "1.23",
            38: "0",
            39: "20",
            43: "30",
            44: "1",
            45: "2",
            46: "1.5",
            49: "0.95",
            57: "1.23",
        }
        for k, value in values.items():
            fields[k] = value
        return "v_" + symbol + '="' + "~".join(fields) + '";'

    def test_public_quote_units_preserve_turnover_zero_and_yuan_scaling(self):
        result = parse_quote_details(self.public_quote(), "sz002318")
        values = {m.key: m.value for m in result.metrics}
        self.assertEqual((values["turnover_rate"], values["volume_ratio"]), (0, 0.95))
        self.assertEqual((values["volume"], values["amount"]), (200, 12300))
        self.assertEqual((values["total_mv"], values["circ_mv"]), (2e8, 1e8))

    def test_public_index_chart_requests_exchange_code_with_separate_identity(self):
        data = dict(data=dict(sh000001=dict(day=[["2026-11-04", 10.5, 11, 12, 9, 2]])))
        with patch(
            "chengchuan.infrastructure.market_data.http_text",
            side_effect=[self.public_quote("sh000001"), json.dumps(data)],
        ) as http:
            frame = TencentMarketProvider().fetch("sh000001", "day")
        self.assertEqual(frame.snapshot.series.symbol, "index:sh000001")
        self.assertEqual(frame.quote.instrument.symbol, "index:sh000001")
        self.assertNotIn("index:", str(http.call_args_list))

    def test_public_index_overview_is_one_batch_and_does_not_fake_breadth(self):
        from chengchuan.domain.market import INDEX_CATALOG, market_code

        raw = "\n".join(self.public_quote(market_code(s)) for s, _, _ in INDEX_CATALOG)
        with patch("chengchuan.infrastructure.market_data.http_text", return_value=raw) as http:
            result = TencentMarketProvider().fetch_overview()
        self.assertEqual(len(result.indices), 6)
        self.assertEqual(result.breadth, ())
        http.assert_called_once()
