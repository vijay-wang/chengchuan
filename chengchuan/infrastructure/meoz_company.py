"""Company sources use their reviewed service, raw field contract and shared HTTP cache."""

import hashlib
import json
from dataclasses import replace
from datetime import datetime

from chengchuan.application.company import date_key
from chengchuan.application.errors import MarketError, StorageError
from chengchuan.application.research import research_stock_symbol
from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.research import ResearchColumn, ResearchMeta, ResearchTable
from chengchuan.infrastructure.company_contracts import COMPANY_SPECS
from chengchuan.infrastructure.meoz_research import scalar


class MeozCompanyProvider:
    def __init__(self, client, archive):
        self.client, self.archive = client, archive

    def describe(self, key):
        spec = COMPANY_SPECS[key]
        return spec["title"], spec["url"], "猫爪／" + spec["api"]

    def fetch(self, key, params, refresh=False):
        spec = COMPANY_SPECS[key]
        params = dict(params)
        unknown = set(params) - set(spec["params"])
        if unknown:
            raise ValueError("公司接口不支持参数：" + ",".join(sorted(unknown)))
        wanted = research_stock_symbol(str(params.get("symbols") or ""))
        scope = hashlib.sha256(
            json.dumps(["company", key, params], sort_keys=True).encode()
        ).hexdigest()
        expected = tuple(ResearchColumn(*c) for c in spec["columns"])
        fields = "" if spec["raw"] else ",".join(c.key for c in expected)
        if spec["paged"]:
            params.update(limit=500, offset=0)
        rows, results, notices = [], [], []
        complete, expected_total = True, None
        for _ in range(6):
            try:
                result = self.client.query(
                    spec["api"],
                    params,
                    fields,
                    service=spec["service"],
                    max_age=0 if refresh else spec["ttl"],
                )
            except MarketError:
                if getattr(self.client, "offline", False) is True:
                    stored = self.archive.before(scope, datetime.now(SHANGHAI))
                    if stored is not None:
                        return replace(
                            stored,
                            meta=replace(stored.meta, cached=True),
                            notice=stored.notice
                            + "；离线读取已归档版本（含源当时返回的空记录），不代表最新覆盖。",
                        )
                raise
            results.append(result)
            rows.extend(result.rows)
            total = (result.metadata or {}).get("total")
            if total is not None and (type(total) is not int or total < 0):
                raise MarketError("公司资料分页总数无效")
            offset = (result.metadata or {}).get("offset")
            if spec["paged"] and offset is not None and offset != params["offset"]:
                raise MarketError("公司资料返回的分页位置与请求不符")
            if isinstance(total, int):
                if expected_total is not None and total != expected_total:
                    complete = False
                    notices.append("分页过程中源总数变化")
                expected_total = total
            if not spec["paged"]:
                break
            if total is not None and len(rows) >= total:
                if len(rows) > total:
                    complete = False
                    notices.append("返回行数超过源声明总数")
                break
            if len(result.rows) < params["limit"]:
                if total is not None and len(rows) < total:
                    complete = False
                    notices.append("源总数与已返回分页不一致")
                break
            params["offset"] += len(result.rows)
        else:
            complete = False
            notices.append("达到6页／3000行保护上限，请缩小年限或指定报告期")
        stamps = {str((r.metadata or {}).get("updated_at") or "") for r in results}
        if len(stamps) > 1:
            complete = False
            notices.append("分页期间源更新时间变化")
        if spec["raw"]:
            notices.append(
                "VIP原字段／原值；未核定尺度不换算为元、万股或百分数。报表可能为累计口径。"
            )
        if key == "basic":
            notices.append("当前基础档案，与历史披露截止日独立；空字段表示源未提供。")
        if key == "qa":
            notices.append("问答覆盖自2026-06-26累积，按回答日期查询；日级回看不代表分钟级可得。")
        dates, missing_dates = set(), 0
        cutoff = str(params.get("as_of") or params.get("enddate") or "")[:10].replace("-", "")[:8]
        for row in rows:
            try:
                symbol = research_stock_symbol(str(row.get("symbol") or row.get("code") or ""))
            except ValueError as exc:
                raise MarketError("公司资料缺少有效证券身份，拒绝错配") from exc
            if symbol != wanted:
                raise MarketError("公司资料返回其他股票，拒绝串股")
            announce = str(row.get("announce_date") or "").replace("-", "")[:8]
            if params.get("as_of") and announce and announce > cutoff:
                raise MarketError("公司资料混入披露截止日之后的数据")
            event = str(row.get("event_date") or row.get("tradedate") or "").replace("-", "")[:8]
            if event and params.get("enddate") and event > str(params["enddate"]):
                raise MarketError("公司资料返回查询日期范围之后的数据")
            answer_date = date_key(row.get("answer_time")) if key == "qa" else ""
            if answer_date and cutoff and answer_date > cutoff:
                raise MarketError("问答混入截止日之后的回答")
            if params.get("as_of") and not announce:
                missing_dates += 1
            value = row.get("report_date") or row.get("event_date") or row.get("tradedate")
            if value:
                dates.add(str(value))
        if missing_dates:
            notices.append(f"{missing_dates}行未提供披露日期，源端as_of筛选无法在本地逐行复核")
        if any(r.get("version_ambiguous") for r in rows):
            notices.append("存在并列修订版本，不选择唯一最新值或生成确定差值")
        observed_keys = tuple(dict.fromkeys(k for row in rows for k in row))
        by_key = {c.key: c for c in expected}
        keys = tuple(c.key for c in expected if c.key in observed_keys)
        keys += tuple(k for k in observed_keys if k not in by_key)
        columns = (
            tuple(
                by_key.get(k, ResearchColumn(k, k, "原值", "源新增字段，含义与单位待核"))
                for k in keys
            )
            if rows
            else expected
        )
        if len(columns) > 512:
            raise MarketError("公司资料字段超过512列，请核对源契约")
        meta = results[0].metadata or {}
        table = ResearchTable(
            key,
            spec["title"],
            columns,
            tuple(tuple(scalar(row.get(c.key)) for c in columns) for row in rows),
            ResearchMeta(
                "猫爪／" + spec["api"],
                spec["url"],
                ",".join(sorted(dates)),
                str(meta.get("updated_at") or meta.get("synced_at") or ""),
                str(meta.get("published_at") or ""),
                min(r.fetched_at for r in results),
                complete=complete,
                coverage=f"返回{len(rows)}行；源总数{expected_total if expected_total is not None else '未提供'}；{len(results)}页",
                cached=all(r.cached for r in results),
            ),
            "；".join(dict.fromkeys([*notices, *(r.notice for r in results if r.notice)])),
        )
        try:
            return self.archive.append(scope, table, datetime.now(SHANGHAI))
        except StorageError as exc:
            return replace(table, notice=table.notice + "；归档失败：" + str(exc))
