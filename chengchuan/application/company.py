"""Company dossier workflows: published evidence, raw financial units and safe comparisons."""

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit

from chengchuan.application.errors import MarketError, StorageError
from chengchuan.application.evidence_export import export_bundle
from chengchuan.application.research import numeric, research_stock_symbol
from chengchuan.domain.company import COMPANY_SECTIONS, CompanyBundle, CompanyFact, CompanyRequest
from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.research import ResearchColumn, ResearchMeta, ResearchTable

IDENTITY_FIELDS = frozenset(
    (
        "symbol",
        "report_date",
        "announce_date",
        "revision_no",
        "version_ambiguous",
        "m_anntime",
        "m_timetag",
        "m_stateTypeCode",
        "m_currency",
        "m_industryCode",
        "rank",
        "declareDate",
        "endDate",
        "qa_id",
    )
)


class CompanyProvider(Protocol):
    def fetch(self, key, params, refresh=False) -> ResearchTable: ...
    def describe(self, key) -> tuple[str, str, str]: ...


def date_key(value):
    text = str(value or "")[:10].replace("-", "")
    try:
        return datetime.strptime(text, "%Y%m%d").strftime("%Y%m%d")
    except ValueError:
        return ""


def external_url(value):
    """Only an explicit user action opens a normal web URL; no local or script protocols."""
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        if (
            parsed.scheme in ("https", "http")
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not any(ord(c) < 32 for c in text)
        ):
            return text
    except ValueError:
        pass
    return ""


def statement_matrix(table, hide_empty=False):
    if not table.rows or not any(c.key == "report_date" for c in table.columns):
        return table
    records = sorted(
        table.records(),
        key=lambda r: (
            str(r.get("report_date") or ""),
            str(r.get("m_stateTypeCode") or ""),
            str(r.get("m_currency") or ""),
            numeric(r.get("revision_no")) or 0,
        ),
        reverse=True,
    )
    if len(records) > 80:
        records = records[:80]
    columns = [
        ResearchColumn("item", "科目"),
        ResearchColumn("raw_field", "源字段"),
        ResearchColumn("unit", "单位／尺度"),
    ]
    for i, row in enumerate(records):
        label = (
            str(row.get("report_date") or "未提供报告期")
            + " · v"
            + str(row.get("revision_no") if row.get("revision_no") is not None else "?")
        )
        desc = (
            f"披露：{row.get('announce_date')}；报表代码：{row.get('m_stateTypeCode')}；"
            f"币种代码：{row.get('m_currency')}；并列版本：{row.get('version_ambiguous')}"
        )
        columns.append(ResearchColumn("period_" + str(i), label, "", desc))
    rows = []
    for column in table.columns:
        if column.key in IDENTITY_FIELDS:
            continue
        if hide_empty and all(r.get(column.key) is None for r in records):
            continue
        rows.append(
            (
                column.label,
                column.key,
                column.unit or "源未注明",
                *(r.get(column.key) for r in records),
            )
        )
    notice = table.notice + "；矩阵仅重排原表；列头悬停查看披露、报表、币种和版本。"
    if hide_empty:
        notice += "已隐藏全空科目，零值科目保留；原始明细与JSON仍完整。"
    if len(table.rows) > 80:
        notice += "仅排列最新80条记录，原始明细与JSON仍保留全部。"
    return replace(table, columns=tuple(columns), rows=tuple(rows), notice=notice)


def holder_changes(table, period=""):
    columns = tuple(
        ResearchColumn(*c)
        for c in (
            ("name", "股东名称", ""),
            ("current_period", "本期报告期", ""),
            ("previous_period", "上期报告期", ""),
            ("status", "列示状态", ""),
            ("current_quantity", "本期持股数量", "原值"),
            ("previous_quantity", "上期持股数量", "原值"),
            ("quantity_change", "数量源值差", "原值"),
            ("ratio_change", "比例源值差", "原值"),
        )
    )
    records = table.records()
    periods = sorted(
        {date_key(r.get("report_date") or r.get("endDate")) for r in records} - {""}, reverse=True
    )
    if period:
        periods = [p for p in periods if p <= period]
    output = table.with_records(
        [], key=table.key + "_changes", title=table.title + " · 相邻期对比", columns=columns
    )
    if not table.meta.complete or len(periods) < 2 or period and periods[0] != period:
        return replace(output, notice="缺少两期完整可比名单，不能把缺失记录当作零持股。")
    current, previous = periods[:2]
    groups = []
    for selected in (current, previous):
        grouped = {}
        for row in records:
            if date_key(row.get("report_date") or row.get("endDate")) == selected:
                grouped.setdefault(str(row.get("name") or ""), []).append(row)
        groups.append(grouped)
    now, before = groups
    result = []
    for name in sorted(set(now) | set(before)):
        left, right = now.get(name, []), before.get(name, [])
        valid = (
            name
            and len(left) == len(right) == 1
            and not any(r.get("version_ambiguous") for r in (*left, *right))
        )
        a, b = left[0] if len(left) == 1 else {}, right[0] if len(right) == 1 else {}
        valid = valid and all(a.get(k) == b.get(k) for k in ("nature", "cType"))
        qa, qb = numeric(a.get("quantity")), numeric(b.get("quantity"))
        ra, rb = numeric(a.get("ratio")), numeric(b.get("ratio"))
        status = (
            "重复名称／并列版本，差值待核"
            if left and right and not valid
            else "两期均列示（数量差不等于实际买卖）"
            if valid
            else "本期列示、上期未列前十"
            if left
            else "本期未列前十，变动未知"
        )
        result.append(
            dict(
                name=name or "未提供名称",
                current_period=current,
                previous_period=previous,
                status=status,
                current_quantity=qa,
                previous_quantity=qb,
                quantity_change=qa - qb if valid and qa is not None and qb is not None else None,
                ratio_change=ra - rb if valid and ra is not None and rb is not None else None,
            )
        )
    return replace(
        output.with_records(result),
        notice=("仅按同类表精确名称比较；单位未确认，保留源值差；前十缺席不等于清仓／从零买入。"),
    )


def company_series(table, field, all_versions=False):
    """Unambiguous points only. Keep source report/currency codes and revisions separate."""
    if field in IDENTITY_FIELDS:
        return ()
    groups = {}
    for row in table.records():
        date = date_key(row.get("report_date") or row.get("endDate") or row.get("m_timetag"))
        value = numeric(row.get(field))
        if not date or value is None or row.get("version_ambiguous"):
            continue
        group = (
            str(
                row.get("m_stateTypeCode")
                if row.get("m_stateTypeCode") is not None
                else "源未提供报表代码"
            ),
            str(row.get("m_currency") if row.get("m_currency") is not None else "源未提供币种代码"),
            str(row.get("revision_no") if row.get("revision_no") is not None else "?")
            if all_versions
            else "latest",
        )
        if table.key in ("holders", "float_holders"):
            group = (str(row.get("name") or "未提供股东名称"), str(row.get("nature") or ""), *group)
        groups.setdefault(group, {}).setdefault(date, []).append(value)
    result = []
    for group, dates in sorted(groups.items()):
        points = tuple(
            (day, values[0]) for day, values in sorted(dates.items()) if len(values) == 1
        )
        if points:
            result.append((" / ".join(group), points))
    return tuple(result)


class CompanyService:
    def __init__(self, provider: CompanyProvider):
        self.provider = provider

    def run(self, request: CompanyRequest):
        if request.section not in dict(COMPANY_SECTIONS) or request.years not in (1, 3, 5):
            raise ValueError("不支持的公司资料分类或年限")
        symbol = research_stock_symbol(request.symbol)[-6:]
        day = datetime.strptime(request.as_of, "%Y%m%d").date()
        if day > datetime.now(SHANGHAI).date():
            raise ValueError("披露截止日不能在未来")
        if request.period and (
            date_key(request.period) != request.period
            or request.period[4:] not in ("0331", "0630", "0930", "1231")
        ):
            raise ValueError("请选择有效季度报告期")
        start = f"{day.year - request.years + 1}0101"
        version = "all" if request.all_versions else "latest"
        params = dict(
            symbols=symbol,
            startdate=start,
            enddate=request.as_of,
            as_of=request.as_of,
            version=version,
        )
        tables, notes = (
            [],
            ["按披露自然日查询源端已保存版本；首次采集与披露时间不同，不构成分钟级当时已知回测。"],
        )

        def fetch(key, overrides=None):
            query = dict(params) if overrides is None else overrides
            if key in ("forecast", "schedule") and not request.period:
                query["enddate"] = f"{day.year}1231"
            if request.period and key in (
                "indicator",
                "income",
                "balance",
                "cashflow",
                "forecast",
                "express",
                "schedule",
                "dividend",
            ):
                query = {k: v for k, v in query.items() if k not in ("startdate", "enddate")}
                query["period"] = request.period
            try:
                table = self.provider.fetch(key, query, request.refresh)
            except (MarketError, StorageError) as exc:
                describe = getattr(self.provider, "describe", None)
                title, url, source = describe(key) if callable(describe) else (key, "", "资料源")
                text = str(exc)
                status = (
                    "无权限／额度"
                    if "403" in text
                    else "暂无记录／覆盖"
                    if "1002" in text
                    else "查询未完成"
                )
                table = ResearchTable(
                    key,
                    title + " · " + status,
                    (ResearchColumn("status", "状态"), ResearchColumn("message", "说明")),
                    ((status, text),),
                    ResearchMeta(source, url, "", "", "", datetime.now(SHANGHAI), complete=False),
                    text,
                )
            tables.append(table)
            return table

        if request.section == "overview":
            fetch("basic", dict(symbols=symbol))
            fetch(
                "valuation",
                dict(
                    symbols=symbol,
                    startdate=(day - timedelta(days=45)).strftime("%Y%m%d"),
                    enddate=request.as_of,
                ),
            )
            notes.append(
                "基础档案为当前截面；估值采用截至所选日的最近可用交易日，不冒充周末／休市日行情。"
            )
        elif request.section == "indicator":
            fetch("indicator")
        elif request.section == "statements":
            for key in ("income", "balance", "cashflow"):
                fetch(key)
            notes.append("三表保留累计口径及源报表／币种代码，不自动拆单季、年化或推算TTM。")
        elif request.section == "holders":
            for key in ("holders", "float_holders"):
                table = fetch(key)
                if any(c.key == "quantity" for c in table.columns):
                    tables.append(holder_changes(table, request.period))
            fetch("holder_number")
            fetch("capital")
            notes.append(
                "十大股东／流通股东分别比较；数量差不是实际买卖，名单不提供实时持仓或北向交易。"
            )
        elif request.section == "dividend":
            fetch("dividend")
            notes.append(
                "每股现金分红单位元；方案进度、登记日、除权日和派息日分开，不把预案当已实施。"
            )
        elif request.section == "earnings":
            for key in ("schedule", "forecast", "express"):
                fetch(key)
            notes.append("预告是区间，快报与定期报告分别列示，预约日不等于实际披露日。")
        elif request.section == "announcements":
            fetch("announcements")
            notes.append("公告为已保留记录；查不到不证明没有公告，原文仅在用户点击时打开。")
        elif request.section == "qa":
            fetch(
                "qa", dict(symbols=symbol, startdate=start, enddate=request.as_of, version=version)
            )
            notes.append("按回答日期筛选；问题与回答保留各自时间，北交所暂不在该接口范围。")
        if any("原值" in c.unit for t in tables for c in t.columns):
            notes.append("VIP单位／尺度未确认的字段显示原值；不套用旧字段别名、元或百分数。")
        facts = self._facts(tables)
        return CompanyBundle(request, tuple(tables), tuple(notes), facts)

    @staticmethod
    def _facts(tables):
        facts = []
        for table in tables:
            if not table.rows or not table.meta.complete:
                continue
            if table.key == "basic":
                row = table.records()[0]
                for key, label in (
                    ("name", "股票简称"),
                    ("fullname", "公司全称"),
                    ("industry", "行业"),
                    ("area", "地区"),
                    ("list_date", "上市日期"),
                    ("act_name", "实控人（当前档案）"),
                ):
                    facts.append(CompanyFact(label, row.get(key), "", table.key))
            if table.key == "valuation" and any(c.key == "tradedate" for c in table.columns):
                row = max(table.records(), key=lambda r: str(r.get("tradedate") or ""))
                by_key = {c.key: c for c in table.columns}
                for key in ("pe_ttm", "pb", "total_mv", "total_share", "dv_ttm", "close"):
                    column = by_key.get(key)
                    if column:
                        facts.append(
                            CompanyFact(
                                column.label + " · " + str(row.get("tradedate") or ""),
                                row.get(key),
                                column.unit,
                                table.key,
                            )
                        )
        return tuple(facts)


def export_company(bundle, path):
    matrices = [
        statement_matrix(t) if t.key in ("income", "balance", "cashflow") else t
        for t in bundle.tables
    ]
    export_bundle(bundle, path, "公司资料 · " + bundle.request.symbol, markdown_tables=matrices)
