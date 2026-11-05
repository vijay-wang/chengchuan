import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from chengchuan.application.errors import MarketError
from chengchuan.application.microstructure import (
    analyze_orders,
    execution_spans,
    match_groups,
    order_queue,
    select_bands,
    select_executions,
)
from chengchuan.domain.microstructure import OrderEvent, OrderFilter, OrderTape, TradeEvent
from chengchuan.domain.models import SHANGHAI
from chengchuan.infrastructure.meoz_client import ApiResult, MeozClient, MeozStore, page_metadata
from chengchuan.infrastructure.meoz_market import MeozMarketProvider
from chengchuan.infrastructure.meoz_stream import STREAM_FIELDS, parse_stream_book, request_ticket
from chengchuan.infrastructure.microstructure import (
    JsonOrderTapeRepository,
    fen,
    fetch_tape,
    integer,
    load_pages,
    parse_book,
    record_time,
)

DAY = date(2026, 11, 4)
NOW = datetime(2026, 11, 4, 18, tzinfo=SHANGHAI)
START = NOW.replace(hour=9, minute=30)


def order(order_id=1, side="B", price=2000, shares=60000, kind="0", seconds=0):
    return OrderEvent(START + timedelta(seconds=seconds), order_id, 0, kind, side, price, shares)


def trade(trade_id=1, bid=1, ask=10, shares=10000, kind="0", seconds=1):
    return TradeEvent(
        START + timedelta(seconds=seconds), trade_id, kind, "B", bid, ask, 2000, shares
    )


def tape(orders=None, trades=None, complete=True):
    return OrderTape(
        "sz002318",
        DAY,
        tuple(
            orders
            if orders is not None
            else [
                order(),
                order(10, "S", shares=10000),
                order(11, "S", shares=10000),
                order(12, "S", shares=10000),
            ]
        ),
        tuple(
            trades
            if trades is not None
            else [trade(i + 1, ask=10 + i, seconds=i + 1) for i in range(3)]
        ),
        (),
        complete,
        complete,
        True,
        NOW,
    )


class OriginalOrderTests(unittest.TestCase):
    def test_inline_executions_keep_chronology_and_only_merge_adjacent_ids(self):
        source = tape(
            orders=[order(1, shares=300), order(2, shares=100), order(10, "S", shares=400)],
            trades=[
                trade(1, bid=1, shares=100, seconds=1),
                trade(2, bid=2, shares=100, seconds=2),
                trade(3, bid=1, shares=100, seconds=3),
            ],
        )
        a = analyze_orders(source)
        rows = select_executions(a, OrderFilter(True, True))
        self.assertEqual([r.trade.trade_id for r in rows], [1, 2, 3])
        self.assertEqual(
            [(start, count) for start, count, _ in execution_spans(rows, "buy")],
            [(0, 1), (1, 1), (2, 1)],
        )
        self.assertEqual(
            [(start, count) for start, count, _ in execution_spans(rows, "sell")], [(0, 3)]
        )

    def test_inline_cancel_filter_and_ambiguous_counterparts_keep_real_trade_distinction(self):
        source = tape(
            orders=[order(1, shares=300), order(10, "S", shares=400), order(10, "S", shares=400)],
            trades=[trade(1, shares=100), trade(2, ask=0, kind="C")],
        )
        a = analyze_orders(source)
        rows = select_executions(a, OrderFilter(True, True))
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].sell)
        self.assertEqual(select_executions(a, OrderFilter(True, False)), ())

    def test_independent_unfilled_and_cancelled_filters_keep_original_amount(self):
        source = tape(
            orders=[
                order(1, shares=300),
                order(2, shares=100),
                order(3, shares=200),
                order(4, shares=500),
            ],
            trades=[
                trade(bid=1, ask=0, shares=50),
                trade(2, bid=3, ask=0, shares=40),
                trade(3, bid=3, ask=0, kind="C"),
                trade(4, bid=4, ask=0, kind="C"),
            ],
        )
        a = analyze_orders(source)
        expected = {
            (False, False): (1, 600000),
            (True, False): (2, 800000),
            (False, True): (2, 1000000),
            (True, True): (4, 2200000),
        }
        for (unfilled, cancelled), totals in expected.items():
            filters = OrderFilter(unfilled, cancelled)
            buy, sell = select_bands(a.band_groups, filters)
            self.assertEqual((sum(b.orders for b in buy), sum(b.amount_fen for b in buy)), totals)
            self.assertEqual(sum(b.orders for b in sell), 0)
            visible = [o for o in a.restored if filters.accepts(o)]
            self.assertEqual(sum(o.original_fen for o in visible), totals[1])

    def test_minute_groups_sum_to_day_and_use_original_submission_not_fill_time(self):
        source = tape(
            orders=[order(1, shares=100), order(2, shares=200, seconds=60)],
            trades=[trade(bid=1, ask=0, shares=50, seconds=121)],
        )
        a = analyze_orders(source)
        self.assertEqual([m.minute.strftime("%H:%M") for m in a.minute_groups], ["09:30", "09:31"])
        for u in (True, False):
            for c in (True, False):
                filters = OrderFilter(u, c)
                day_buy, _ = select_bands(a.band_groups, filters)
                minute_amount = sum(
                    b.amount_fen
                    for m in a.minute_groups
                    for b in select_bands(m.groups, filters)[0]
                )
                self.assertEqual(minute_amount, sum(b.amount_fen for b in day_buy))
        self.assertEqual(
            select_bands(a.minute_groups[0].groups, OrderFilter(False, False))[0][0].orders, 1
        )
        before = analyze_orders(source, START + timedelta(seconds=61))
        self.assertEqual(
            sum(
                b.orders
                for m in before.minute_groups
                for b in select_bands(m.groups, OrderFilter(False, False))[0]
            ),
            0,
        )

    def test_match_groups_preserve_three_counterpart_orders_and_real_execution_prices(self):
        source = tape(
            orders=[
                order(1, price=1700, shares=300),
                *(order(i, "S", price=1700, shares=100) for i in (10, 11, 12)),
            ],
            trades=[
                replace(trade(i + 1, ask=10 + i, shares=100), price_fen=1700 + i) for i in range(3)
            ],
        )
        a = analyze_orders(source)
        selected = next(o for o in a.restored if o.side == "B")
        groups = match_groups(source, a, selected)
        self.assertEqual([g.counterpart.exchange_id for g in groups], [10, 11, 12])
        self.assertEqual([g.trades[0].price_fen for g in groups], [1700, 1701, 1702])
        self.assertAlmostEqual(selected.average_price, 17.01)
        self.assertEqual(len(match_groups(source, a, a.restored[1])), 1)

    def test_match_missing_and_duplicate_counterparts_are_not_invented(self):
        for originals in ([order()], [order(), order(10, "S"), order(10, "S", seconds=1)]):
            source = tape(orders=originals, trades=[trade()])
            a = analyze_orders(source)
            group = match_groups(source, a, a.restored[0])[0]
            self.assertIsNone(group.counterpart)
            self.assertEqual(group.trades[0].shares, 10000)

    def test_split_executions_are_one_original_order_at_original_not_executed_amount(self):
        result = analyze_orders(tape())
        self.assertEqual(sum(b.orders for b in result.buy), 1)
        self.assertEqual(result.buy[3].amount_fen, 1200000 * 100)
        self.assertEqual(result.buy[3].orders, 1)
        self.assertEqual(sum(b.orders for b in result.sell), 3)
        restored = next(r for r in result.restored if r.side == "B")
        self.assertEqual((restored.executed_shares, restored.trade_count), (30000, 3))
        self.assertEqual(restored.executed_fen, 600000 * 100)

    def test_sz_cancel_record_removes_whole_order_even_after_partial_fill(self):
        source = tape()
        source = replace(
            source, trades=(*source.trades, trade(99, ask=0, kind="C", shares=30000, seconds=10))
        )
        result = analyze_orders(source)
        self.assertEqual(sum(b.orders for b in result.buy), 0)
        self.assertEqual(sum(b.amount_fen for b in result.buy), 0)
        self.assertEqual(result.cancelled_orders, 1)
        self.assertEqual(sum(p.buy_shares for p in result.price_volume), 30000)

    def test_sh_delete_order_is_not_a_new_sell_or_a_counted_order(self):
        source = replace(
            tape(),
            symbol="sh600519",
            orders=(order(kind="A"), order(kind="D", seconds=10)),
            trades=(),
        )
        result = analyze_orders(source)
        self.assertEqual(result.cancelled_orders, 1)
        self.assertEqual(sum(b.orders for b in result.buy), 0)

    def test_future_cancel_does_not_leak_into_earlier_replay(self):
        source = tape(orders=[order()], trades=[trade(ask=0, kind="C", seconds=60)])
        before = analyze_orders(source, START + timedelta(seconds=30))
        after = analyze_orders(source, START + timedelta(seconds=90))
        self.assertEqual(sum(b.orders for b in before.buy), 1)
        self.assertEqual(sum(b.orders for b in after.buy), 0)

    def test_duplicate_and_zero_exchange_ids_are_not_repaired_from_other_ids(self):
        source = tape(orders=[order(0), order(7), order(7, seconds=1)], trades=[])
        result = analyze_orders(source)
        self.assertEqual(result.ambiguous_orders, 3)
        self.assertEqual(sum(b.orders for b in result.buy), 0)

    def test_exact_band_boundaries_are_left_closed_and_last_band_is_unbounded(self):
        values = (199999, 200000, 500000, 1000000, 2000000, 4000000, 6000000, 10000000, 20000000)
        result = analyze_orders(
            tape(
                orders=[order(i + 1, price=100, shares=v) for i, v in enumerate(values)], trades=[]
            )
        )
        self.assertEqual(tuple(b.orders for b in result.buy), (1,) * 9)
        self.assertEqual(tuple(b.amount_fen for b in result.buy), tuple(v * 100 for v in values))

    def test_unpriced_market_order_is_excluded_instead_of_using_execution_price(self):
        result = analyze_orders(tape(orders=[order(price=0, kind="1")], trades=[trade(ask=0)]))
        self.assertEqual(result.unpriced_orders, 1)
        self.assertEqual(sum(b.orders for b in result.buy), 0)

    def test_cancelled_unpriced_orders_remain_excluded_when_cancels_are_included(self):
        result = analyze_orders(tape(orders=[order(price=0)], trades=[trade(ask=0, kind="C")]))
        self.assertEqual(result.cancelled_orders, 1)
        self.assertEqual(result.all_unpriced_orders, 1)
        self.assertEqual(
            sum(b.orders for b in select_bands(result.band_groups, OrderFilter(True, True))[0]), 0
        )

    def test_incomplete_pages_do_not_claim_final_bands(self):
        result = analyze_orders(tape(complete=False))
        self.assertFalse(result.complete)
        self.assertIn("暂不发布", result.notice)

    def test_unknown_trade_kind_excludes_referenced_orders_conservatively(self):
        result = analyze_orders(tape(orders=[order()], trades=[trade(kind="UNRECOGNIZED", ask=0)]))
        self.assertEqual(sum(b.orders for b in result.buy), 0)
        self.assertEqual(result.unknown_events, 1)
        self.assertEqual(result.all_ambiguous_orders, 1)
        self.assertEqual(
            sum(b.orders for b in select_bands(result.band_groups, OrderFilter(True, True))[0]), 0
        )

    def test_price_volume_includes_real_fills_and_excludes_cancel_records(self):
        source = tape(
            orders=[], trades=[trade(kind="0x00", shares=10), trade(2, kind="C", shares=100)]
        )
        result = analyze_orders(source)
        self.assertEqual(len(result.price_volume), 1)
        self.assertEqual(result.price_volume[0].buy_shares, 10)

    def test_queue_deducts_fills_excludes_cancelled_and_market_orders_and_selects_price(self):
        source = tape(
            orders=[
                order(1, shares=100),
                order(2, shares=100, seconds=1),
                order(3, price=1999, shares=500),
                order(4, shares=100, kind="1"),
            ],
            trades=[trade(bid=1, ask=0, shares=40), trade(2, bid=2, ask=0, kind="C", seconds=5)],
        )
        result = analyze_orders(source)
        queue = order_queue(result, "B", 2000)
        self.assertEqual([(r.exchange_id, r.remaining_shares) for r in queue], [(1, 60)])
        self.assertEqual(order_queue(result, "S", 2000), ())

    def test_wrong_replay_day_and_naive_time_are_rejected(self):
        for stamp in (NOW + timedelta(days=1), NOW.replace(tzinfo=None)):
            with self.assertRaises(ValueError):
                analyze_orders(tape(), stamp)


class TickAdapterTests(unittest.TestCase):
    def test_cached_loader_never_reads_credentials_or_calls_http_on_cache_miss(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MeozStore(Path(folder) / "cache.sqlite")
            provider = MeozMarketProvider(MeozClient(store=store, offline=False))
            before = store.usage()
            with (
                patch(
                    "chengchuan.infrastructure.meoz_client.private_key",
                    side_effect=AssertionError("不应读取凭据"),
                ),
                patch(
                    "chengchuan.infrastructure.meoz_client.urlopen",
                    side_effect=AssertionError("不应联网"),
                ),
            ):
                with self.assertRaises(MarketError):
                    provider.fetch_cached_microstructure("002318", DAY)
            self.assertEqual(store.usage(), before)

    def test_cached_bands_can_load_without_quotes_but_network_quote_errors_are_not_hidden(self):
        for offline in (True, False):
            client = Mock(offline=offline)
            empty = ApiResult(
                (),
                NOW,
                True,
                "https://example.invalid",
                page=dict(has_more=False, next_cursor=None, tradedate="20261104"),
            )
            client.query.side_effect = [empty, empty, MarketError("缺少十档缓存")]
            if offline:
                result = fetch_tape(client, "002318", DAY, quote_optional=True)
                self.assertTrue(result.orders_complete and result.trades_complete)
                self.assertFalse(result.quotes_complete)
                self.assertEqual(result.quotes, ())
            else:
                with self.assertRaises(MarketError):
                    fetch_tape(client, "002318", DAY, quote_optional=True)

    def test_times_and_large_identifiers_keep_precision(self):
        self.assertEqual(record_time(DAY, "093000123").microsecond, 123000)
        self.assertEqual(record_time(DAY, "2026-11-04T01:30:00+00:00").hour, 9)
        self.assertEqual(integer("9007199254740993"), 9007199254740993)
        self.assertEqual(fen("16.72"), 1672)
        for value in (True, 1.5, -1, None):
            with self.assertRaises(MarketError):
                integer(value)

    def test_live_lot_quantity_and_historical_share_quantity_are_not_confused(self):
        row = dict(time="150000000", bid1=16.72, bid_vol1=6, ask1=16.73, ask_vol1=None)
        live = parse_book(row, "sz002318", DAY, depth=5, lot_multiplier=100)
        history = parse_book(row, "sz002318", DAY)
        self.assertEqual(live.levels[0].bid_quantity, 600)
        self.assertEqual(history.levels[0].bid_quantity, 6)
        self.assertIsNone(live.levels[0].ask_quantity)
        self.assertFalse(live.historical)
        self.assertTrue(history.historical)

    def test_pagination_metadata_is_not_discarded_or_guessed(self):
        self.assertIsNone(page_metadata(dict(data=dict(fields=[], items=[]))))
        with self.assertRaises(MarketError):
            page_metadata(dict(data=dict(has_more=True, next_cursor=None)))
        self.assertFalse(
            page_metadata(dict(data=dict(has_more=False, next_cursor=None)))["has_more"]
        )

    def test_paging_is_bounded_and_cursor_is_passed_unchanged(self):
        client = Mock()
        row = dict(symbol="002318", tradedate="20261104", time="093000000")
        client.query.side_effect = [
            ApiResult(
                (row,),
                NOW,
                False,
                "https://example.invalid",
                page=dict(has_more=True, next_cursor="opaque", tradedate="20261104"),
            ),
            ApiResult(
                (row,),
                NOW,
                False,
                "https://example.invalid",
                page=dict(has_more=False, next_cursor=None, tradedate="20261104"),
            ),
        ]
        rows, complete, _, _ = load_pages(client, "level2_order_history", "sz002318", DAY, 2)
        self.assertTrue(complete)
        self.assertEqual(len(rows), 2)
        self.assertNotIn("cursor", client.query.call_args_list[0].args[1])
        self.assertEqual(client.query.call_args_list[1].args[1]["cursor"], "opaque")

    def test_truncation_wrong_asset_day_and_repeating_cursor_do_not_become_complete(self):
        client = Mock()
        row = dict(symbol="002318", tradedate="20261104", time="093000000")
        result = ApiResult(
            (row,),
            NOW,
            False,
            "https://example.invalid",
            page=dict(has_more=True, next_cursor="same", tradedate="20261104"),
        )
        client.query.return_value = result
        self.assertFalse(load_pages(client, "x", "sz002318", DAY, 1)[1])
        with self.assertRaises(MarketError):
            load_pages(client, "x", "sz002318", DAY, 2)
        client.query.return_value = replace(result, rows=(dict(row, symbol="600519"),))
        with self.assertRaises(MarketError):
            load_pages(client, "x", "sz002318", DAY, 1)

    def test_json_import_requires_explicit_coverage_and_rejects_mixed_securities(self):
        raw = dict(
            version=1,
            symbol="002318",
            day="2026-11-04",
            orders=[
                dict(
                    symbol="002318",
                    tradedate="20261104",
                    time="093000000",
                    exchange_order_id=1,
                    order_id=0,
                    order_type="0",
                    order_code="B",
                    price=20,
                    volume=100,
                )
            ],
            trades=[],
            quotes=[],
            coverage=dict(orders=True, trades=True, quotes=True),
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ticks.json"
            path.write_text(json.dumps(raw))
            self.assertTrue(analyze_orders(JsonOrderTapeRepository().load(path)).complete)
            raw["coverage"]["orders"] = "true"
            path.write_text(json.dumps(raw))
            self.assertFalse(analyze_orders(JsonOrderTapeRepository().load(path)).complete)
            raw["orders"][0]["symbol"] = "600519"
            path.write_text(json.dumps(raw))
            with self.assertRaises(MarketError):
                JsonOrderTapeRepository().load(path)

    def test_ws_fields_identity_and_quantity_units_remain_explicit(self):
        row = [
            "002318.SZ",
            int(NOW.timestamp() * 1000),
            16.72,
            100,
            1000,
            [16.71] * 5,
            [16.73] * 5,
            [6] * 5,
            [3] * 5,
            0,
        ]
        book = parse_stream_book(row, STREAM_FIELDS, "sz002318")
        self.assertEqual(book.quantity_unit, "源数量")
        self.assertEqual(book.levels[0].bid_quantity, 6)
        with self.assertRaises(MarketError):
            parse_stream_book(row, STREAM_FIELDS, "sh600519")

    def test_ticket_url_is_transient_and_never_written_to_response_cache(self):
        store = Mock()
        store.reserve.return_value = (1, 0)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(
            dict(
                code=200,
                data=dict(
                    url="wss://meoz.cn/api/tick_stream_v1?ticket=synthetic",
                    stream_version="tick_v1",
                ),
            )
        ).encode()
        with (
            patch(
                "chengchuan.infrastructure.meoz_stream.private_key", return_value="nc_" + "A" * 32
            ),
            patch("chengchuan.infrastructure.meoz_stream.urlopen", return_value=response),
        ):
            result = request_ticket(store, "002318")
        self.assertIn("ticket=", result["url"])
        self.assertEqual(store.finish.call_args.kwargs, {})
