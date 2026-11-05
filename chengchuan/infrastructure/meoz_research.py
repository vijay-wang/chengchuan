"""MeoZ specialty adapters. HTTP/auth/cache remain in the shared client."""

import hashlib
import json
import math
from dataclasses import replace
from datetime import datetime, time, timedelta

from chengchuan.application.errors import MarketError, StorageError
from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.research import ResearchColumn, ResearchMeta, ResearchTable
from chengchuan.infrastructure.research_contracts import SPECS


def scalar(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def scope_key(key, params):
    body = [key, {k: v for k, v in params.items() if k not in ("limit", "offset")}]
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class MeozResearchProvider:
    def __init__(self, client, archive):
        self.client, self.archive = client, archive

    def describe(self, key):
        spec = SPECS[key]
        return spec["title"], spec["url"], "猫爪／" + spec["api"]

    def fetch(self, key, params, day, latest=True):
        spec = SPECS[key]
        params = dict(params)
        unknown = set(params) - set(spec["params"])
        if unknown:
            raise ValueError("研究接口不支持参数：" + ",".join(sorted(unknown)))
        scope = scope_key(key, params)
        if spec["latest_only"] and not latest:
            cutoff = datetime.combine(datetime.strptime(day, "%Y%m%d").date(), time.max, SHANGHAI)
            stored = self.archive.before(scope, cutoff)
            if stored is None:
                raise MarketError("所选历史日之前没有本机采集快照；此接口仅保留最新数据")
            return replace(
                stored,
                notice="本机当时已采集快照；" + stored.notice,
                meta=replace(stored.meta, cached=True),
            )
        columns = tuple(ResearchColumn(*c) for c in spec["columns"])
        fields = "" if spec["fixed"] else ",".join(c.key for c in columns)
        rows, results = [], []
        complete, coverage = True, []
        if spec["paged"]:
            params.update(limit=1000, offset=0)
        for _ in range(6):
            result = self.client.query(
                spec["api"],
                params,
                fields,
                service=spec["service"],
                max_age=86400 if key in ("library", "detail", "aliases") else 180,
                structured=spec["structured"],
            )
            results.append(result)
            rows.extend(result.rows)
            meta = result.metadata or {}
            if spec["structured"] or not spec["paged"]:
                if len(rows) >= 6000:
                    complete = False
                    coverage.append("达到6000条边界，可能截断")
                break
            total = meta.get("total")
            if isinstance(total, int):
                coverage.append(f"源总数{total}")
                if len(rows) >= total:
                    break
            elif len(result.rows) < params["limit"]:
                break
            if not result.rows:
                complete = False
                coverage.append("总数与空分页不一致")
                break
            params["offset"] += len(result.rows)
        else:
            complete = False
            coverage.append("已达6页上限；缩小范围后重试")
        stamps = {str((r.metadata or {}).get("updated_at") or "") for r in results}
        if len(stamps) > 1:
            complete = False
            coverage.append("分页期间源版本变化")
        result = results[0]
        metadata = result.metadata or {}
        business_days = sorted(
            {str(r.get("tradedate") or r.get("source_day") or "") for r in rows} - {""}
        )
        business_day = str(metadata.get("tradedate") or ",".join(business_days))
        requested_day = params.get("tradedate")
        if requested_day and key != "anomaly":
            if (
                any(d != requested_day for d in business_days)
                or metadata.get("tradedate")
                and str(metadata["tradedate"]) != requested_day
            ):
                raise MarketError("研究接口返回日期与请求不一致，拒绝将当前数据混入历史")
        for row in rows:
            row_day = str(row.get("tradedate") or row.get("source_day") or "")
            if row_day and (
                params.get("startdate", row_day) > row_day
                or params.get("enddate", row_day) < row_day
            ):
                raise MarketError("研究接口返回查询日期范围之外的数据")
        coverage = "；".join(dict.fromkeys(coverage)) or f"返回{len(rows)}行"
        document = json.dumps(result.document, ensure_ascii=False) if result.document else ""
        table = ResearchTable(
            key,
            spec["title"],
            columns,
            tuple(tuple(scalar(r.get(c.key)) for c in columns) for r in rows),
            ResearchMeta(
                "猫爪／" + spec["api"],
                spec["url"],
                business_day,
                str(metadata.get("updated_at") or metadata.get("synced_at") or ""),
                str(metadata.get("published_at") or ""),
                min(r.fetched_at for r in results),
                complete=complete,
                coverage=coverage,
                cached=all(r.cached for r in results),
            ),
            "；".join(dict.fromkeys(r.notice for r in results if r.notice)),
            document,
        )
        try:
            return self.archive.append(scope, table, datetime.now(SHANGHAI))
        except StorageError as exc:
            return replace(table, notice=table.notice + "；归档失败：" + str(exc))

    def previous(self, key, params, current):
        if current.meta.observed_at is None:
            return None
        return self.archive.before(
            scope_key(key, params), current.meta.observed_at - timedelta(microseconds=1)
        )

    def quotes(self, symbols, day):
        # Shared full-market request also serves existing sector views; filter only locally.
        from chengchuan.infrastructure.meoz_dashboard import SCREENING_FIELDS

        result = self.client.query("screening", dict(tradedate=day), SCREENING_FIELDS)
        if any(str(r.get("tradedate")) != day for r in result.rows):
            raise MarketError("成份行情混入其他日期，拒绝显示")
        wanted = set(symbols)
        rows = [r for r in result.rows if str(r.get("symbol") or "").split(".")[0] in wanted]
        rows = [dict(r, symbol=str(r.get("symbol") or "").split(".")[0]) for r in rows]
        columns = tuple(
            ResearchColumn(*c)
            for c in (
                ("symbol", "股票代码", ""),
                ("name", "名称", ""),
                ("close", "价格", "元"),
                ("pct_chg", "涨跌幅", "%"),
                ("main_net_amount", "供应商主力净额", "元"),
                ("amount", "成交额", "元"),
            )
        )
        known = {str(r.get("symbol")) for r in rows}
        rows.extend(dict(symbol=s) for s in symbols if s not in known)
        return ResearchTable(
            "stocks",
            "成份股（双击打开行情）",
            columns,
            tuple(tuple(r.get(c.key) for c in columns) for r in rows),
            ResearchMeta(
                "猫爪／screening",
                "https://meoz.cn/docs/query/stock/screening",
                day,
                "",
                "",
                result.fetched_at,
                complete=len(result.rows) < 6000,
                cached=result.cached,
            ),
            "当前成份，不代表历史成份；缺行情保留为空。" + result.notice,
        )
