"""Vendor-neutral research snapshots; no stock-price assumptions for signed statistics."""

from dataclasses import dataclass, replace
from datetime import datetime


@dataclass(frozen=True)
class ThemeIdentity:
    source: str
    level: str
    code: str

    def __post_init__(self):
        if not self.source or not self.level or not self.code:
            raise ValueError("题材身份必须包含来源、层级和原始代码")


@dataclass(frozen=True)
class ResearchColumn:
    key: str
    label: str
    unit: str = ""
    description: str = ""

    @property
    def caption(self):
        return self.label + (
            f"（{self.unit}）" if self.unit and self.unit not in self.label else ""
        )


@dataclass(frozen=True)
class ResearchMeta:
    source: str
    source_url: str
    business_day: str
    source_updated_at: str
    published_at: str
    fetched_at: datetime
    first_seen_at: datetime | None = None
    revision: str = ""
    complete: bool = True
    coverage: str = ""
    cached: bool = False
    observed_at: datetime | None = None

    def __post_init__(self):
        for value in (self.fetched_at, self.first_seen_at, self.observed_at):
            if value is not None and value.utcoffset() is None:
                raise ValueError("研究时间必须包含时区")


@dataclass(frozen=True)
class ResearchTable:
    key: str
    title: str
    columns: tuple[ResearchColumn, ...]
    rows: tuple[tuple, ...]
    meta: ResearchMeta
    notice: str = ""
    document: str = ""

    def __post_init__(self):
        object.__setattr__(self, "columns", tuple(self.columns))
        object.__setattr__(self, "rows", tuple(tuple(r) for r in self.rows))
        if len({c.key for c in self.columns}) != len(self.columns):
            raise ValueError("研究字段重复")
        if any(len(r) != len(self.columns) for r in self.rows):
            raise ValueError("研究数据列数不一致")
        if any(
            not isinstance(v, (str, int, float, bool, type(None))) for r in self.rows for v in r
        ):
            raise ValueError("嵌套研究数据必须显式编码，不可修改快照")

    def records(self):
        keys = tuple(c.key for c in self.columns)
        return tuple(dict(zip(keys, r)) for r in self.rows)

    def with_records(self, records, *, columns=None, key=None, title=None):
        columns = tuple(columns or self.columns)
        return replace(
            self,
            columns=columns,
            rows=tuple(tuple(r.get(c.key) for c in columns) for r in records),
            key=key or self.key,
            title=title or self.title,
        )


@dataclass(frozen=True)
class ResearchRequest:
    module: str
    day: str
    latest: bool = True
    symbol: str = ""
    level: str = "parent"
    code: str = ""
    action: str = "overview"
    platform: str = "tdx&resou"
    source: str = "xgb"
    seat: str = ""
    cutoff: str = ""


@dataclass(frozen=True)
class ResearchBundle:
    request: ResearchRequest
    tables: tuple[ResearchTable, ...]
    notes: tuple[str, ...] = ()
