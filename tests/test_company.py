import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from chengchuan.application.company import (
    CompanyService,
    company_series,
    export_company,
    external_url,
    holder_changes,
    statement_matrix,
)
from chengchuan.application.errors import MarketError
from chengchuan.domain.company import CompanyBundle, CompanyRequest
from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.research import ResearchColumn, ResearchMeta, ResearchTable
from chengchuan.infrastructure.meoz_client import ApiResult
from chengchuan.infrastructure.meoz_company import MeozCompanyProvider
from chengchuan.infrastructure.research_archive import (
    SqliteResearchArchive,
    decode_table,
    encode_table,
)

NOW = datetime(2026, 11, 5, 15, tzinfo=SHANGHAI)


def company_table(key, rows, complete=True):
    fields = tuple(dict.fromkeys(k for r in rows for k in r))
    return ResearchTable(
        key,
        key,
        tuple(ResearchColumn(k, k) for k in fields),
        tuple(tuple(r.get(k) for k in fields) for r in rows),
        ResearchMeta("fixture", "https://example.test", "", "", "", NOW, complete=complete),
    )


class FakeCompanyProvider:
    def __init__(self, fixtures=None):
        self.fixtures, self.calls = fixtures or {}, []

    def fetch(self, key, params, refresh=False):
        self.calls.append((key, params, refresh))
        result = self.fixtures.get(key, company_table(key, []))
        if isinstance(result, Exception):
            raise result
        return result

    def describe(self, key):
        return key, "https://example.test/" + key, "fixture"


class CompanyCalculationTests(unittest.TestCase):
    def test_future_report_forecast_is_retained_with_published_asof_filter(self):
        provider = FakeCompanyProvider(
            {
                "forecast": company_table(
                    "forecast",
                    [dict(symbol="002318", report_date="20261231", announce_date="20261031")],
                )
            }
        )
        request = CompanyRequest("sz002318", "20261105", "earnings")
        bundle = CompanyService(provider).run(request)
        self.assertEqual(bundle.request, request)
        query = next(p for k, p, _ in provider.calls if k == "forecast")
        self.assertEqual(query["as_of"], "20261105")
        self.assertEqual(query["enddate"], "20261231")
        self.assertEqual(bundle.tables[1].records()[0]["report_date"], "20261231")

    def test_period_is_not_combined_with_mutually_exclusive_business_date_range(self):
        provider = FakeCompanyProvider()
        CompanyService(provider).run(
            CompanyRequest("002318", "20261105", "statements", period="20260930", all_versions=True)
        )
        for _, params, _ in provider.calls:
            self.assertEqual(params["period"], "20260930")
            self.assertEqual(params["version"], "all")
            self.assertNotIn("startdate", params)
            self.assertNotIn("enddate", params)

    def test_qa_uses_answer_window_and_never_sends_unsupported_asof(self):
        provider = FakeCompanyProvider()
        CompanyService(provider).run(CompanyRequest("002318", "20261105", "qa"))
        key, params, _ = provider.calls[0]
        self.assertEqual(key, "qa")
        self.assertEqual(params["enddate"], "20261105")
        self.assertNotIn("as_of", params)

    def test_overview_uses_current_profile_and_recent_quote_window_without_calendar_guess(self):
        provider = FakeCompanyProvider()
        CompanyService(provider).run(CompanyRequest("002318", "20261101"))
        self.assertEqual(provider.calls[0][1], dict(symbols="002318"))
        quote = provider.calls[1][1]
        self.assertEqual(quote["enddate"], "20261101")
        self.assertIn("startdate", quote)
        self.assertNotIn("tradedate", quote)

    def test_one_source_failure_keeps_other_earnings_panels(self):
        provider = FakeCompanyProvider(
            {
                "forecast": MarketError("403 no permission"),
                "express": company_table("express", [dict(revenue=0)]),
            }
        )
        bundle = CompanyService(provider).run(CompanyRequest("002318", "20261105", "earnings"))
        self.assertFalse(bundle.tables[1].meta.complete)
        self.assertEqual(bundle.tables[2].records()[0]["revenue"], 0)

    def test_missing_top10_name_never_becomes_zero_position_or_buy_amount(self):
        rows = [
            dict(report_date="20260930", name="A", quantity=120, ratio=0.3),
            dict(report_date="20260630", name="A", quantity=100, ratio=0.25),
            dict(report_date="20260930", name="B", quantity=50, ratio=0.1),
            dict(report_date="20260630", name="C", quantity=10, ratio=0.02),
        ]
        result = {r["name"]: r for r in holder_changes(company_table("holders", rows)).records()}
        self.assertEqual(result["A"]["quantity_change"], 20)
        self.assertAlmostEqual(result["A"]["ratio_change"], 0.05)
        self.assertIsNone(result["B"]["quantity_change"])
        self.assertIsNone(result["B"]["previous_quantity"])
        self.assertIsNone(result["C"]["quantity_change"])

    def test_ambiguous_versions_and_duplicate_names_do_not_produce_certain_changes(self):
        rows = [
            dict(report_date="20260930", name="A", quantity=0, version_ambiguous=True),
            dict(report_date="20260630", name="A", quantity=10),
            dict(report_date="20260930", name="B", quantity=20),
            dict(report_date="20260930", name="B", quantity=40),
            dict(report_date="20260630", name="B", quantity=30),
        ]
        result = holder_changes(company_table("float_holders", rows))
        self.assertTrue(all(r["quantity_change"] is None for r in result.records()))
        partial = company_table("holders", rows, complete=False)
        self.assertFalse(holder_changes(partial).rows)

    def test_share_class_changes_are_not_treated_as_identical_holdings(self):
        rows = [
            dict(report_date="20260930", name="A", quantity=10, nature="A股"),
            dict(report_date="20260630", name="A", quantity=5, nature="B股"),
        ]
        self.assertIsNone(
            holder_changes(company_table("holders", rows)).records()[0]["quantity_change"]
        )

    def test_matrix_keeps_negative_zero_null_and_source_report_identity(self):
        source = company_table(
            "income",
            [
                dict(
                    symbol="002318",
                    report_date="20260930",
                    announce_date="20261027",
                    revision_no=0,
                    m_stateTypeCode=0,
                    m_currency=0,
                    revenue_inc=0,
                    net_profit_excl_min_int_inc=-12,
                    new_field=None,
                )
            ],
        )
        matrix = statement_matrix(source)
        by_key = {r["raw_field"]: r for r in matrix.records()}
        self.assertEqual(by_key["revenue_inc"]["period_0"], 0)
        self.assertEqual(by_key["net_profit_excl_min_int_inc"]["period_0"], -12)
        self.assertIsNone(by_key["new_field"]["period_0"])
        self.assertIn("v0", matrix.columns[-1].label)
        self.assertIn("报表代码：0", matrix.columns[-1].description)
        self.assertNotIn("raw_field", source.records()[0])

    def test_hiding_empty_matrix_items_keeps_zero_and_all_original_fields(self):
        source = company_table(
            "income", [dict(report_date="20260930", revenue_inc=0, missing=None)]
        )
        filtered = statement_matrix(source, hide_empty=True)
        self.assertEqual([r["raw_field"] for r in filtered.records()], ["revenue_inc"])
        self.assertIn("missing", source.records()[0])

    def test_curves_separate_currency_and_report_codes_and_skip_ambiguous_dates(self):
        source = company_table(
            "income",
            [
                dict(
                    report_date=d,
                    m_currency=c,
                    m_stateTypeCode=t,
                    revision_no=0,
                    version_ambiguous=a,
                    revenue_inc=v,
                )
                for d, c, t, a, v in (
                    ("20260630", 0, 0, False, -1),
                    ("20260930", 0, 0, False, 0),
                    ("20260930", 1, 0, False, 10),
                    ("20260930", 0, 1, False, 20),
                    ("20260331", 0, 0, True, 30),
                )
            ],
        )
        series = company_series(source, "revenue_inc", True)
        self.assertEqual(len(series), 3)
        first = next(points for key, points in series if key == "0 / 0 / 0")
        self.assertEqual(first, (("20260630", -1), ("20260930", 0)))

    def test_stockholder_curves_keep_each_holder_separate(self):
        source = company_table(
            "holders",
            [
                dict(report_date=d, name=n, quantity=q)
                for d, n, q in (
                    ("20260630", "A", 10),
                    ("20260930", "A", 20),
                    ("20260630", "B", 3),
                    ("20260930", "B", 4),
                )
            ],
        )
        series = company_series(source, "quantity")
        self.assertEqual(len(series), 2)
        self.assertTrue(all(len(points) == 2 for _, points in series))

    def test_url_protocols_and_credentials_are_not_executed(self):
        self.assertEqual(external_url("https://example.test/a.pdf"), "https://example.test/a.pdf")
        for value in (
            "javascript:alert(1)",
            "file:///etc/passwd",
            "data:text/html,x",
            "https://u:p@example.test/a",
            "https://[invalid",
        ):
            self.assertEqual(external_url(value), "")

    def test_exports_raw_json_and_matrix_markdown_without_fabricating_units(self):
        table = company_table(
            "income", [dict(symbol="002318", report_date="20260930", revenue_inc=0)]
        )
        bundle = CompanyBundle(CompanyRequest("002318", "20261105", "statements"), (table,))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "company.json"
            export_company(bundle, path)
            data = json.loads(path.read_text())
            self.assertEqual(data["tables"][0]["rows"][0][-1], 0)
            path = path.with_suffix(".md")
            export_company(bundle, path)
            self.assertIn("源字段", path.read_text())
            self.assertIn("revenue_inc", path.read_text())


class CompanyProviderTests(unittest.TestCase):
    def test_offline_archive_can_replay_an_expired_empty_raw_response_without_http(self):
        state = {"offline": False}

        class Client:
            @property
            def offline(self):
                return state["offline"]

            def query(self, *args, **kwargs):
                if self.offline:
                    raise MarketError("离线原始缓存缺少空响应")
                return ApiResult((), NOW, False, "local", metadata=dict(total=0, offset=0))

        provider = MeozCompanyProvider(Client(), self.archive)
        first = provider.fetch("express", dict(symbols="002318", as_of="20261105"))
        self.assertEqual(first.rows, ())
        state["offline"] = True
        cached = provider.fetch("express", dict(symbols="002318", as_of="20261105"))
        self.assertTrue(cached.meta.cached)
        self.assertEqual(cached.rows, ())
        self.assertIn("已归档", cached.notice)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = SqliteResearchArchive(Path(self.temp.name) / "company.sqlite")

    def provider(self, rows, metadata=None):
        calls = []

        class Client:
            def query(self, api, params, fields, **kwargs):
                calls.append((api, dict(params), fields, kwargs))
                return ApiResult(tuple(rows), NOW, False, "local", metadata=metadata)

        return MeozCompanyProvider(Client(), self.archive), calls

    def test_raw_vip_field_names_units_zero_and_new_fields_are_preserved(self):
        provider, calls = self.provider(
            [
                dict(
                    symbol="002318",
                    report_date="20260930",
                    announce_date="20261027",
                    net_profit=0,
                    ExtraFIELD=-3,
                    version_ambiguous=False,
                )
            ]
        )
        result = provider.fetch("indicator", dict(symbols="002318", as_of="20261105"))
        self.assertEqual(calls[0][2], "")
        self.assertEqual(calls[0][3]["service"], "history")
        cols = {c.key: c for c in result.columns}
        self.assertTrue(cols["net_profit"].label.startswith("净利率"))
        self.assertEqual(cols["net_profit"].unit, "原值")
        self.assertIn("ExtraFIELD", cols)
        self.assertEqual(result.records()[0]["ExtraFIELD"], -3)
        self.assertEqual(result.records()[0]["net_profit"], 0)

    def test_explicit_units_for_valuation_and_dividend_are_kept(self):
        from chengchuan.infrastructure.company_contracts import COMPANY_SPECS

        for key, field, unit in (
            ("valuation", "total_share", "万股"),
            ("dividend", "cash_div", "元"),
        ):
            col = next(c for c in COMPANY_SPECS[key]["columns"] if c[0] == field)
            self.assertEqual(col[2], unit)

    def test_wrong_symbol_and_future_disclosures_are_rejected(self):
        for row in (
            dict(symbol="600519", announce_date="20261001"),
            dict(symbol="002318", announce_date="20261106"),
        ):
            provider, _ = self.provider([row])
            with self.assertRaises(MarketError):
                provider.fetch("indicator", dict(symbols="002318", as_of="20261105"))

    def test_question_date_does_not_allow_future_answer_to_leak(self):
        provider, _ = self.provider(
            [
                dict(
                    symbol="002318",
                    event_date="20261101",
                    question_time="2026-11-01 10:00:00",
                    answer_time="2026-11-06 10:00:00",
                )
            ]
        )
        with self.assertRaisesRegex(MarketError, "回答"):
            provider.fetch("qa", dict(symbols="002318", enddate="20261105"))

    def test_pagination_ceiling_is_partial_and_prevents_false_completeness(self):
        class Client:
            calls = 0

            def query(self, *args, **kwargs):
                self.calls += 1
                return ApiResult(
                    tuple(
                        dict(symbol="002318", report_date="20260930", name=str(i))
                        for i in range(500)
                    ),
                    NOW,
                    False,
                    "local",
                    metadata=dict(total=3001),
                )

        client = Client()
        result = MeozCompanyProvider(client, self.archive).fetch("holders", dict(symbols="002318"))
        self.assertEqual(client.calls, 6)
        self.assertEqual(len(result.rows), 3000)
        self.assertFalse(result.meta.complete)

    def test_wrong_page_offset_is_rejected(self):
        provider, _ = self.provider([dict(symbol="002318")], dict(total=1, offset=5))
        with self.assertRaisesRegex(MarketError, "分页位置"):
            provider.fetch("holders", dict(symbols="002318"))

    def test_old_research_archive_columns_decode_without_description_field(self):
        source = company_table("old", [dict(symbol="002318", value=0)])
        encoded = json.loads(encode_table(source))
        for col in encoded["columns"]:
            col.pop("description", None)
        restored = decode_table(json.dumps(encoded))
        self.assertEqual(restored.rows, source.rows)
        self.assertTrue(all(c.description == "" for c in restored.columns))


if __name__ == "__main__":
    unittest.main()
