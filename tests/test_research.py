import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from chengchuan.application.errors import MarketError
from chengchuan.application.research import (
    SpecialtyResearchService,
    auction_shares,
    detail_tables,
    export_research,
    interval_changes,
    member_rows,
    research_stock_symbol,
)
from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.research import (
    ResearchBundle,
    ResearchColumn,
    ResearchMeta,
    ResearchRequest,
    ResearchTable,
    ThemeIdentity,
)
from chengchuan.infrastructure.meoz_client import ApiResult, MeozClient, MeozStore
from chengchuan.infrastructure.meoz_research import MeozResearchProvider, scope_key
from chengchuan.infrastructure.research_archive import SqliteResearchArchive

NOW = datetime(2026, 11, 5, 14, tzinfo=SHANGHAI)


def table(key, records=(), document="", **kwargs):
    keys = list(dict.fromkeys(k for row in records for k in row))
    columns = tuple(ResearchColumn(k, k) for k in keys)
    return ResearchTable(
        key,
        key,
        columns,
        tuple(tuple(r.get(k) for k in keys) for r in records),
        ResearchMeta(
            "test-source", "https://example.test", "20261104", "09:35:01", "", NOW, **kwargs
        ),
        document=document,
    )


class FakeResearchProvider:
    def __init__(self, fixtures=None):
        self.fixtures, self.calls = fixtures or {}, []

    def fetch(self, key, params, day, latest=True):
        self.calls.append((key, dict(params), day, latest))
        value = self.fixtures.get(key, table(key))
        if isinstance(value, Exception):
            raise value
        return value

    def quotes(self, symbols, day):
        return table("stocks", [dict(symbol=s) for s in symbols])

    def previous(self, key, params, current):
        return self.fixtures.get("previous")


class ResearchCalculationTests(unittest.TestCase):
    def test_stock_suffix_is_normalized_with_exchange_validation(self):
        self.assertEqual(research_stock_symbol("002318.SZ"), "sz002318")
        for value in ("002318.SH", "801165k", "920001.BJ"):
            with self.assertRaises(ValueError):
                research_stock_symbol(value)

    def test_curve_cutoff_excludes_later_buckets_without_claiming_known_at_time(self):
        rows = [
            dict(
                tradedate="20261104",
                theme_symbol="801165k",
                trademin=t,
                pct_chg=1,
                main_net_amount=100,
            )
            for t in ("0955", "1000", "1005")
        ]
        provider = FakeResearchProvider({"minute": table("minute", rows)})
        bundle = SpecialtyResearchService(provider).run(
            ResearchRequest(
                "rotation", "20261104", False, code="801165k", action="detail", cutoff="100000"
            )
        )
        self.assertEqual([r["trademin"] for r in bundle.tables[0].records()], ["0955", "1000"])
        self.assertTrue(any("不能视为" in n for n in bundle.notes))

    def test_signed_differences_require_every_bucket_and_never_cross_lunch(self):
        rows = [
            dict(
                tradedate="20261104",
                theme_symbol="801165k",
                trademin=t,
                main_net_amount=n,
                pct_chg=p,
            )
            for t, n, p in [
                ("0930", -30, -2),
                ("0935", -10, -1),
                ("0940", -20, -3),
                ("0945", 10, 0),
                ("0955", 100, 2),
                ("1130", 150, 1),
                ("1305", 180, 2),
            ]
        ]
        source = table("minute", rows)
        result = interval_changes(source).records()
        self.assertEqual(result[1]["net_5m"], 20)
        self.assertEqual(result[1]["condition"], "涨幅上升／净额增加")
        self.assertEqual(result[3]["net_15m"], 40)
        for i in (0, 4, 6):
            self.assertIsNone(result[i]["net_5m"])
        partial = replace(source, meta=replace(source.meta, complete=False))
        self.assertTrue(all(r["net_5m"] is None for r in interval_changes(partial).records()))
        self.assertNotIn("net_5m", source.records()[0])

    def test_filtering_leaders_preserves_original_rank_and_ties(self):
        source = table(
            "leaders",
            [
                dict(
                    theme_symbol="801165k",
                    symbols=json.dumps(["300308", ["002318", "600000"], "300502"]),
                )
            ],
        )
        result = member_rows(source, "002318", leaders=True).records()
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["rank"], result[0]["tied"]), (2, True))
        self.assertNotEqual(
            ThemeIdentity("jx", "parent", "801165k"), ThemeIdentity("xgb", "parent", "801165k")
        )

    def test_auction_units_and_unknown_side_do_not_turn_into_sell(self):
        source = table(
            "auction_ticks", [dict(auc_vol=2, um_vol=3, um_side=s) for s in ("b", "s", None)]
        )
        result = auction_shares(source)
        records = result.records()
        self.assertEqual([r["auc_vol"] for r in records], [200, 200, 200])
        self.assertEqual([r["um_signed_shares"] for r in records], [300, -300, None])
        self.assertEqual(result.columns[0].unit, "股")

    def test_nested_details_render_plain_text_and_keep_stock_namespace(self):
        doc = dict(
            name="AI硬件",
            introduction='<p>公开解读</p><script>steal()</script><img src="https://remote.test/x">',
            stock_list=[dict(symbol="002318", name="久立特材")],
            table=dict(Level1=[dict(name="子题材")]),
        )
        source = table("detail", document=json.dumps(doc))
        evidence, stocks, tree = detail_tables(source)
        self.assertIn("公开解读", evidence.document)
        self.assertNotIn("steal", evidence.document)
        self.assertNotIn("remote.test", evidence.document)
        self.assertEqual(stocks.records()[0]["symbol"], "002318")
        self.assertTrue(any("子题材" in r["path"] for r in tree.records()))

    def test_source_level1_level2_pairs_keep_parent_path_and_stock_reason(self):
        doc = dict(
            table=[
                dict(
                    Level1=dict(ID="1", Name="父题材"),
                    Level2=[
                        dict(
                            ID="2",
                            Name="子题材",
                            Stocks=[
                                dict(
                                    StockID="002318.SZ", prod_name="久立", Reason="<p>源内关联</p>"
                                )
                            ],
                        )
                    ],
                )
            ]
        )
        tree = detail_tables(table("detail", document=json.dumps(doc)))[2]
        stock = next(r for r in tree.records() if r["symbol"])
        self.assertEqual(stock["path"], "父题材 / 子题材")
        self.assertEqual(stock["reason"], "源内关联")

    def test_one_missing_source_does_not_hide_other_panels(self):
        provider = FakeResearchProvider(
            {
                "emotion": MarketError("403 no access"),
                "pool": table("pool", [dict(symbol="002318", pct_chg=0)]),
            }
        )
        result = SpecialtyResearchService(provider).run(
            ResearchRequest("sentiment", "20261104", False)
        )
        self.assertFalse(result.tables[0].meta.complete)
        self.assertEqual(result.tables[0].records()[0]["status"], "无权限／额度")
        self.assertEqual(result.tables[1].records()[0]["pct_chg"], 0)
        yes = next(call for call in provider.calls if call[0] == "yesterday")
        self.assertEqual(yes[1]["tradedate"], "20261104")

    def test_leader_request_never_uses_server_stock_filter(self):
        provider = FakeResearchProvider()
        request = ResearchRequest("evidence", "20261104", False, symbol="sz002318")
        result = SpecialtyResearchService(provider).run(request)
        self.assertEqual(result.request, request)
        leaders = next(c for c in provider.calls if c[0] == "leaders")
        self.assertNotIn("symbols", leaders[1])
        self.assertEqual(leaders[1]["tag"], "long")

    def test_auction_comparison_uses_previous_session_across_weekend(self):
        provider = FakeResearchProvider(
            {"auction": table("auction", [dict(tradedate="20261102", auc_vol=2)])}
        )
        SpecialtyResearchService(provider).run(
            ResearchRequest("auction", "20261102", False, "002318")
        )
        dates = [c[1]["tradedate"] for c in provider.calls if c[0] == "auction_ticks"]
        self.assertEqual(dates, ["20261102", "20261030"])

    def test_auction_summary_failure_keeps_tick_and_last_quote_sources(self):
        provider = FakeResearchProvider({"auction": MarketError("1002 unavailable")})
        result = SpecialtyResearchService(provider).run(
            ResearchRequest("auction", "20261104", False, "002318")
        )
        self.assertFalse(result.tables[0].meta.complete)
        self.assertEqual(
            [t.key for t in result.tables[1:]],
            ["auction_ticks_20261104", "auction_ticks_20261103", "auction_last"],
        )

    def test_disclosure_reasons_and_both_seat_directions_are_never_summed(self):
        rows = [
            dict(symbol="002318", seat_name="席位A", side=side, reason=reason, buy=100, sell=30)
            for side, reason in ((0, "日涨幅"), (1, "日涨幅"), (0, "三日涨幅"))
        ]
        provider = FakeResearchProvider({"seats": table("seats", rows)})
        bundle = SpecialtyResearchService(provider).run(ResearchRequest("seats", "20261104", False))
        self.assertEqual(len(bundle.tables[1].rows), 3)
        self.assertNotIn("持仓", str(bundle.tables[1].records()))

    def test_hot_live_does_not_invent_ranks_or_call_first_capture_new(self):
        provider = FakeResearchProvider(
            {"hot_live": table("hot_live", [dict(s="002318", n="久立", pc=0)])}
        )
        bundle = SpecialtyResearchService(provider).run(ResearchRequest("attention", "20261104"))
        row = bundle.tables[0].records()[0]
        self.assertEqual(row["observed_status"], "首次采集，无法比较")
        self.assertNotIn("rank", row)
        provider.fixtures["previous"] = table("hot_live", [dict(s="600000")])
        bundle = SpecialtyResearchService(provider).run(ResearchRequest("attention", "20261104"))
        self.assertEqual(bundle.tables[0].records()[0]["observed_status"], "新进入本机观察")

    def test_exports_include_evidence_and_separate_source_and_observation_time(self):
        source = table("sample", [dict(symbol="002318", reason="a|b\nc")], first_seen_at=NOW)
        bundle = ResearchBundle(ResearchRequest("evidence", "20261104"), (source,), ("研究口径",))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "研究.md"
            export_research(bundle, path)
            text = path.read_text()
            self.assertIn("09:35:01", text)
            self.assertIn("首次采集", text)
            self.assertIn("a\\|b<br>c", text)
            path = path.with_suffix(".json")
            export_research(bundle, path)
            self.assertEqual(
                json.loads(path.read_text())["tables"][0]["meta"]["source"], "test-source"
            )


class ResearchStorageTests(unittest.TestCase):
    def test_lhb_adapters_use_documented_history_service_without_live_fallback(self):
        calls = []

        class Client:
            def query(self, api, params, fields, **kwargs):
                calls.append((api, kwargs["service"]))
                return ApiResult((), NOW, False, "local")

        provider = MeozResearchProvider(Client(), self.archive)
        for key in ("lhb", "seats", "aliases"):
            result = provider.fetch(key, {}, "20261104")
            self.assertIn("/history/longhubang/", result.meta.source_url)
        self.assertEqual(
            calls,
            [
                ("longhubang_stock", "history"),
                ("longhubang_seat", "history"),
                ("longhubang_hot_money", "history"),
            ],
        )

    def test_http_migration_reason_is_shown_redacted_without_following_arbitrary_urls(self):
        key = "nc_" + "x" * 32
        client = MeozClient(MeozStore(Path(self.temp.name) / "raw.sqlite"))
        response = dict(message="此接口已迁移至历史服务；" + key)
        error = HTTPError(
            "https://sz.meoz.cn:6688/api",
            410,
            "Gone",
            {},
            io.BytesIO(json.dumps(response).encode()),
        )
        with (
            patch("chengchuan.infrastructure.meoz_client.private_key", return_value=key),
            patch("chengchuan.infrastructure.meoz_client.urlopen", side_effect=error) as http,
        ):
            with self.assertRaises(MarketError) as caught:
                client.query("longhubang_stock", {})
        self.assertIn("迁移至历史服务", str(caught.exception))
        self.assertNotIn(key, str(caught.exception))
        self.assertEqual(http.call_count, 1)

    def test_null_batches_expire_but_valid_nested_documents_keep_normal_ttl(self):
        store = MeozStore(Path(self.temp.name) / "raw.sqlite")
        with store.connect() as con:
            con.execute(
                "INSERT INTO responses VALUES (?,?,?)",
                ("empty", 1000, json.dumps(dict(code=200, data=None))),
            )
            con.execute(
                "INSERT INTO responses VALUES (?,?,?)",
                ("detail", 1000, json.dumps(dict(code=200, data=dict(theme_id="25")))),
            )
        self.assertIsNone(store.cached("empty", 1301, None, None))
        self.assertIsNotNone(store.cached("detail", 1301, None, None))

    def test_first_seen_is_after_response_arrival_not_request_start(self):
        state = {"returned": False}

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW + timedelta(seconds=10 if state["returned"] else 0)

        class Client:
            def query(self, *args, **kwargs):
                state["returned"] = True
                return ApiResult((dict(tradedate="20261104", s2=1),), NOW, False, "local")

        with patch("chengchuan.infrastructure.meoz_research.datetime", Clock):
            result = MeozResearchProvider(Client(), self.archive).fetch("emotion", {}, "20261104")
        self.assertEqual(result.meta.first_seen_at, NOW + timedelta(seconds=10))

    def test_wrong_business_day_is_rejected_before_archiving(self):
        class WrongDay:
            def query(self, *args, **kwargs):
                return ApiResult((dict(tradedate="20261105"),), NOW, False, "local")

        provider = MeozResearchProvider(WrongDay(), self.archive)
        with self.assertRaisesRegex(MarketError, "日期与请求不一致"):
            provider.fetch("emotion", dict(tradedate="20261104"), "20261104", False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = SqliteResearchArchive(Path(self.temp.name) / "research.sqlite")

    def test_append_only_history_handles_A_B_A_and_first_seen_stays_original(self):
        a, b = table("hot_live", [dict(s="002318")]), table("hot_live", [dict(s="600000")])
        a1 = self.archive.append("scope", a, NOW)
        self.archive.append("scope", b, NOW + timedelta(minutes=1))
        a2 = self.archive.append("scope", a, NOW + timedelta(minutes=2))
        self.assertEqual(a1.meta.first_seen_at, a2.meta.first_seen_at)
        self.assertEqual(a2.meta.observed_at, NOW + timedelta(minutes=2))
        for minutes, expected in ((0, "002318"), (1, "600000"), (2, "002318")):
            result = self.archive.before("scope", NOW + timedelta(minutes=minutes))
            self.assertEqual(result.records()[0]["s"], expected)
        self.assertIsNone(self.archive.before("scope", NOW - timedelta(seconds=1)))

    def test_current_only_historical_lookup_never_requests_today(self):
        class NoHTTP:
            def query(self, *args, **kwargs):
                raise AssertionError("historical snapshot must not use current API")

        provider = MeozResearchProvider(NoHTTP(), self.archive)
        with self.assertRaisesRegex(MarketError, "没有本机采集"):
            provider.fetch("library", {}, "20261104", False)
        scope = scope_key("library", {})
        self.archive.append(scope, table("library", [dict(theme_id="25")]), NOW)
        with self.assertRaises(MarketError):
            provider.fetch("library", {}, "20261104", False)
        stored = provider.fetch("library", {}, "20261105", False)
        self.assertEqual(stored.records()[0]["theme_id"], "25")

    def test_pagination_bound_and_source_revision_change_are_visible(self):
        class Pages:
            def query(self, api, params, *args, **kwargs):
                offset = params["offset"]
                rows = tuple(dict(theme_symbol=f"{i}k") for i in range(offset, offset + 1000))
                return ApiResult(
                    rows, NOW, False, "local", metadata=dict(total=9000, updated_at=str(offset))
                )

        result = MeozResearchProvider(Pages(), self.archive).fetch(
            "rotation", {"level": "parent"}, "20261104"
        )
        self.assertEqual(len(result.rows), 6000)
        self.assertFalse(result.meta.complete)
        self.assertIn("6页上限", result.meta.coverage)
        self.assertIn("版本变化", result.meta.coverage)

    def test_unchanged_hot_snapshot_compares_last_observation_not_last_different_version(self):
        scope = scope_key("hot_live", dict(type="tdx&resou"))
        a = table("hot_live", [dict(s="002318")])
        self.archive.append(scope, a, NOW)
        b = self.archive.append(scope, a, NOW + timedelta(minutes=1))
        previous = MeozResearchProvider(None, self.archive).previous(
            "hot_live", dict(type="tdx&resou"), b
        )
        self.assertEqual(previous.rows, a.rows)

    def test_nested_http_response_is_cached_with_source_metadata_and_no_key(self):
        client = MeozClient(MeozStore(Path(self.temp.name) / "raw.sqlite"))
        payload = dict(
            code=200, data=dict(theme_id="25", updated_at="2026-11-04 00:30:00", stock_list=[])
        )
        with (
            patch(
                "chengchuan.infrastructure.meoz_client.private_key", return_value="nc_" + "x" * 32
            ),
            patch(
                "chengchuan.infrastructure.meoz_client.urlopen",
                return_value=io.BytesIO(json.dumps(payload).encode()),
            ) as http,
        ):
            first = client.query("theme_lib_detail_kp", dict(theme_id="25"), structured=True)
            second = client.query("theme_lib_detail_kp", dict(theme_id="25"), structured=True)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(second.document["theme_id"], "25")
        self.assertEqual(first.metadata["updated_at"], "2026-11-04 00:30:00")
        self.assertTrue(second.cached)
        self.assertNotIn("nc_", str(second.document))


if __name__ == "__main__":
    unittest.main()
