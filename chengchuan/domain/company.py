"""Company dossier requests and evidence; dates are disclosure cutoffs, not quote dates."""

from dataclasses import dataclass, field

from chengchuan.domain.research import ResearchTable

COMPANY_SECTIONS = (
    ("overview", "公司概览"),
    ("indicator", "财务指标"),
    ("statements", "财务三表"),
    ("holders", "股东股本"),
    ("dividend", "分红送转"),
    ("earnings", "业绩披露"),
    ("announcements", "公司公告"),
    ("qa", "问董秘"),
)


@dataclass(frozen=True)
class CompanyRequest:
    symbol: str
    as_of: str
    section: str = "overview"
    years: int = 3
    period: str = ""
    all_versions: bool = False
    refresh: bool = False
    module: str = field(default="company", init=False)


@dataclass(frozen=True)
class CompanyFact:
    label: str
    value: str | float | None
    unit: str = ""
    source_key: str = ""


@dataclass(frozen=True)
class CompanyBundle:
    request: CompanyRequest
    tables: tuple[ResearchTable, ...]
    notes: tuple[str, ...] = ()
    facts: tuple[CompanyFact, ...] = ()
