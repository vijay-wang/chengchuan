import json
import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock, patch

from test_meoz import DAYS, NOW, minute_rows

from chengchuan.application.errors import MarketError
from chengchuan.domain.market import INDEX_CATALOG, is_index
from chengchuan.domain.models import SeriesKind
from chengchuan.domain.periods import PERIODS
from chengchuan.infrastructure.meoz_client import ApiResult
from chengchuan.infrastructure.meoz_market import INDEX_BATCH_SYMBOLS, MeozMarketProvider


class DashboardClient:
    """Contract fixture, with an in-memory response cache and no credential/network access."""

    offline = False

    def __init__(self):
        self.cache = {}
        self.requests = []
        self.http_calls = []
        self.policies = []
        self.errors = {}
        self.overrides = {}
        self.store = Mock()
        self.store.daily_history.side_effect = self.daily_history
        self.store.usage.return_value = dict(total=0, interfaces={})

    def daily_history(self, symbol):
        index = is_index(symbol)
        raw = symbol[-6:] + (".SH" if index and "sh" in symbol else ".SZ")
        days = ("20261028", *DAYS)
        rows = tuple(
            dict(
                tradedate=day,
                symbol=raw,
                name="测试指数" if index else "测试股票",
                open=10,
                high=11,
                low=9,
                close=10,
                pre_close=10,
                vol=200 if day == DAYS[-1] else 100,
                amount=20000,
                servertime="15:00:00" if index and day == DAYS[-1] else None,
            )
            for day in days
        )
        return rows, NOW.timestamp(), DAYS[-1]

    def peek(self, *args, **kwargs):
        return None

    def query(self, api, params, fields="", service="live", **kwargs):
        self.requests.append((api, dict(params), fields, service))
        self.policies.append((api, dict(kwargs)))
        if api in self.errors:
            raise self.errors[api]
        key = json.dumps([api, params, fields, service], sort_keys=True)
        cached = key in self.cache
        if not cached:
            if api in self.overrides:
                rows = self.overrides[api]
            elif api == "valuation":
                rows = [
                    dict(
                        tradedate=params["tradedate"],
                        symbol="002318",
                        code="002318.SZ",
                        volume_ratio=0.95,
                        turnover_rate=0,
                        turnover_rate_f=1.5,
                        pe_ttm=None,
                        pb=2,
                        total_mv=100000,
                        circ_mv=90000,
                    )
                ]
            elif api == "fundflow_kp":
                rows = [
                    dict(
                        tradedate=params["tradedate"],
                        symbol="002318",
                        main_net_amount=-5,
                        main_buy_amount=10,
                        main_sell_amount=-15,
                    )
                ]
            elif api == "screening":
                rows = [
                    dict(
                        tradedate=params["tradedate"],
                        symbol=c[:6],
                        code=c,
                        name="测试",
                        pct_chg=p,
                        main_net_amount=f,
                    )
                    for c, p, f in (
                        ("600001.SH", 1, 10),
                        ("000001.SZ", -1, None),
                        ("300001.SZ", 0, 0),
                        ("688001.SH", None, -5),
                    )
                ]
            elif api == "indexdaily":
                rows = [
                    dict(
                        tradedate=DAYS[-1],
                        symbol=s[-6:] + (".SH" if ":sh" in s else ".SZ"),
                        name=n,
                        open=3000,
                        high=3100,
                        low=2900,
                        close=3000,
                        pre_close=2990,
                        vol=100,
                        amount=300000,
                        servertime="15:00:00",
                    )
                    for s, n, _ in INDEX_CATALOG
                ]
            elif api == "tradecal":
                rows = [dict(cal_date=d, is_open="1") for d in DAYS]
            elif api == "index_minute":
                rows = [
                    dict(r, symbol=s[-6:] + (".SH" if ":sh" in s else ".SZ"))
                    for s, _, _ in INDEX_CATALOG
                    for r in minute_rows(params.get("tradedate", DAYS[-1]))
                ]
            else:
                raise AssertionError("unexpected API " + api)
            if fields:
                rows = [{k: r.get(k) for k in fields.split(",")} for r in rows]
            self.cache[key] = tuple(rows)
            self.http_calls.append((api, dict(params)))
        return ApiResult(self.cache[key], NOW, cached, "https://example.invalid/data")


class MeozDashboardTests(unittest.TestCase):
    def setUp(self):
        self.client = DashboardClient()
        self.provider = MeozMarketProvider(self.client)
        for module in (
            "chengchuan.infrastructure.meoz_market.datetime",
            "chengchuan.infrastructure.meoz_dashboard.datetime",
        ):
            clock = patch(module, wraps=datetime)
            mocked = clock.start()
            mocked.now.return_value = NOW
            self.addCleanup(clock.stop)

    def test_stock_fields_keep_zero_and_distinguish_float_turnover_and_funds(self):
        result = self.provider.fetch_details("002318")
        values = {m.key: m.value for m in result.metrics}
        self.assertEqual(
            (values["volume_ratio"], values["turnover_rate"], values["turnover_rate_f"]),
            (0.95, 0, 1.5),
        )
        self.assertEqual((values["main_net"], values["main_in"], values["main_out"]), (-5, 10, 15))
        self.assertIsNone(values["pe_ttm"])
        self.assertTrue(result.complete)
        self.assertEqual({c[0] for c in self.client.http_calls}, {"valuation", "fundflow_kp"})

    def test_optional_permission_failure_keeps_base_quote_and_can_recover(self):
        self.client.errors["valuation"] = MarketError("403")
        result = self.provider.fetch_details("002318")
        values = {m.key: m.value for m in result.metrics}
        self.assertEqual(values["high"], 11)
        self.assertEqual(values["main_net"], -5)
        self.assertIsNone(values["volume_ratio"])
        self.assertFalse(result.complete)
        self.client.errors.clear()
        self.assertTrue(self.provider.fetch_details("002318").complete)

    def test_wrong_optional_security_is_not_attached_to_the_selected_stock(self):
        self.client.overrides["valuation"] = [
            dict(tradedate=DAYS[-1], symbol="000001", code="000001.SZ", volume_ratio=99)
        ]
        result = self.provider.fetch_details("002318")
        self.assertIsNone(next(m.value for m in result.metrics if m.key == "volume_ratio"))
        self.assertFalse(result.complete)
        past = self.provider.fetch_details("002318", datetime(2026, 11, 3).date())
        self.assertEqual(past.day, past.as_of)
        self.assertTrue(all(c[1].get("tradedate") == "20261103" for c in self.client.requests[-2:]))

    def test_overview_uses_one_batch_and_breadth_scope_is_market_not_constituents(self):
        result = self.provider.fetch_overview()
        self.assertEqual(len(result.indices), 6)
        self.assertEqual({b.scope for b in result.breadth}, {"all", "sh", "sz", "growth", "star"})
        index_request = next(c for c in self.client.requests if c[0] == "indexdaily")
        self.assertEqual(index_request[1]["symbols"], INDEX_BATCH_SYMBOLS)
        self.assertTrue(result.complete)
        details = self.provider.fetch_details("index:sh000001")
        self.assertEqual(next(m.value for m in details.metrics if m.key == "volume_ratio"), 2)
        self.assertEqual(details.breadth[0].total, 4)

    def test_all_six_indices_and_twelve_periods_share_five_minute_batches(self):
        for symbol, _, _ in INDEX_CATALOG:
            for period in PERIODS:
                frame = self.provider.fetch(symbol, period.code)
                self.assertEqual(frame.snapshot.series.symbol, symbol)
                if period.code in ("minute", "five_day"):
                    self.assertEqual(frame.snapshot.series.kind, SeriesKind.SAMPLED_CLOSE)
                    self.assertEqual(frame.vwap_points, ())
                else:
                    self.assertEqual(frame.snapshot.series.kind, SeriesKind.OHLC)
        minute_calls = [c for c in self.client.http_calls if c[0] == "index_minute"]
        self.assertEqual(len(minute_calls), 5)
        self.assertTrue(all(c[1]["symbols"] == INDEX_BATCH_SYMBOLS for c in minute_calls))
        self.assertTrue(
            all(
                c[3] == "history"
                for c in self.client.requests
                if c[0] == "index_minute" and "tradedate" in c[1]
            )
        )

    def test_index_batch_rejects_unknown_symbol_instead_of_ignoring_it(self):
        row = dict(minute_rows(DAYS[-1])[0], symbol="999999.SH")
        self.client.overrides["index_minute"] = [row]
        with self.assertRaisesRegex(MarketError, "未知证券"):
            self.provider.fetch("index:sh000001", "minute")

    def test_historical_optional_fields_require_a_postclose_cache(self):
        self.provider.fetch_details("002318", datetime(2026, 11, 3).date())
        for api, options in self.client.policies:
            if api in ("valuation", "fundflow_kp"):
                self.assertEqual(
                    options["min_fetched"], NOW.replace(day=3, hour=15, minute=15).timestamp()
                )

    def test_one_fresh_component_does_not_confirm_an_old_fund_response(self):
        query = self.client.query

        def with_old_funds(api, *args, **kwargs):
            result = query(api, *args, **kwargs)
            return (
                replace(result, fetched_at=NOW.replace(hour=14)) if api == "fundflow_kp" else result
            )

        self.client.query = with_old_funds
        details = self.provider.fetch_details("002318")
        self.assertFalse(details.complete)
        self.assertEqual(details.retrieved_at, NOW)

    def test_previous_session_statistics_keep_the_stricter_latest_confirmation(self):
        from chengchuan.infrastructure.meoz_dashboard import MeozDashboard

        minimum = NOW.replace(hour=15, minute=45).timestamp()
        with patch("chengchuan.infrastructure.meoz_dashboard.datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW.replace(day=5, hour=8)
            MeozDashboard(self.provider)._breadth(NOW.date(), 86400, minimum)
        options = next(options for api, options in self.client.policies if api == "screening")
        self.assertEqual(options["min_fetched"], minimum)

    def test_fresh_calendar_cannot_disguise_an_old_current_minute_response(self):
        query = self.client.query
        old = NOW.replace(hour=14)

        def with_old_minutes(api, params, *args, **kwargs):
            result = query(api, params, *args, **kwargs)
            if api == "index_minute" and "tradedate" not in params:
                result = replace(result, fetched_at=old)
            return result

        self.client.query = with_old_minutes
        frame = self.provider.fetch("index:sh000001", "min5")
        self.assertEqual(frame.retrieved_at, old)

    def test_late_previous_day_overview_does_not_reuse_a_day_long_raw_cache(self):
        from chengchuan.infrastructure.meoz_dashboard import (
            INDEX_DAILY_FIELDS,
            INDEX_SYMBOLS,
            latest_query_age,
        )

        self.client.peek = lambda *args, **kwargs: ApiResult(
            (dict(tradedate="20261103"),), NOW, True, "https://example.invalid"
        )
        age = latest_query_age(
            self.client,
            "indexdaily",
            dict(symbols=INDEX_SYMBOLS),
            INDEX_DAILY_FIELDS,
            NOW,
            86400,
            NOW.replace(hour=15, minute=45).timestamp(),
        )
        self.assertEqual(age, 180)
        minute = dict(tradedate="20261103", symbol="000001.SH", trademin="1130")
        self.client.peek = lambda *args, **kwargs: ApiResult(
            (minute,), NOW, True, "https://example.invalid"
        )
        options = dict(
            expected="20261103", min_clock="1500", symbol="index:sh000001", service="history"
        )
        self.assertEqual(
            latest_query_age(self.client, "index_minute", {}, "", NOW, None, None, **options), 180
        )
        minute["trademin"] = "1500"
        self.assertIsNone(
            latest_query_age(self.client, "index_minute", {}, "", NOW, None, None, **options)
        )
