"""Exactly two completed up sessions: separate event, model and acceptance contracts."""

from dataclasses import dataclass
from datetime import date, datetime

from chengchuan.domain.models import MarketSnapshot

EVENT_VERSION = "exactly-two-up-comparable-close-v1"
TARGET_VERSION = "third-session-comparable-return-v1"


@dataclass(frozen=True)
class TwoUpConfig:
    target_accuracy: float = 0.8
    min_train: int = 80
    min_validation: int = 40
    min_signals: int = 30
    min_coverage: float = 0.1
    excluded_dates: tuple[str, ...] = ()

    def __post_init__(self):
        if self.target_accuracy != 0.8:
            raise ValueError("当前任务验收目标固定为80%，不能降低目标来达标")
        if any(
            type(v) is not int or v < 3
            for v in (self.min_train, self.min_validation, self.min_signals)
        ):
            raise ValueError("训练、验证及信号最小样本须为不小于3的整数")
        if not 0.1 <= self.min_coverage <= 1:
            raise ValueError("最低验证覆盖率须在10%至100%之间")
        object.__setattr__(self, "excluded_dates", tuple(sorted(set(self.excluded_dates))))
        for value in self.excluded_dates:
            date.fromisoformat(value)


@dataclass(frozen=True)
class ComparableHistory:
    snapshot: MarketSnapshot
    previous_closes: tuple[tuple[date, float], ...]
    observed_at: datetime | None = None
    basis: str = "meoz-daily-pre-close-1.0.52"


@dataclass(frozen=True)
class TwoUpSample:
    symbol: str
    origin: date
    target: date
    available_at: datetime
    revision: str
    values: tuple[tuple[str, float | None], ...]
    return_pct: float


@dataclass(frozen=True)
class EventState:
    as_of: date
    status: str
    reason: str
    returns: tuple[float, ...] = ()


@dataclass(frozen=True)
class TwoUpPrediction:
    record_id: str
    symbol: str
    as_of: str
    target: str | None
    status: str
    reason: str
    feature_revision: str
    event_returns: tuple[float, ...] = ()
    direction: int | None = None
    candidate_direction: int | None = None
    score: float | None = None
    threshold: float | None = None
    train_count: int = 0
    validation_count: int = 0
    model_id: str | None = None
    model_name: str | None = None
    training_label_through: str | None = None
    validation_summary: dict | None = None
    calibrated_probabilities: None = None


@dataclass(frozen=True)
class TwoUpEvaluation:
    prediction_id: str
    actual_return_pct: float | None = None
    actual_direction: int | None = None
    correct: bool | None = None
    status: str = "evaluated"
    reason: str = ""


@dataclass(frozen=True)
class TwoUpRun:
    experiment_id: str
    manifest: dict
    features: object | None
    predictions: tuple[TwoUpPrediction, ...]
    evaluations: tuple[TwoUpEvaluation, ...]
    summaries: tuple[dict, ...]
    notes: tuple[str, ...]
    executed_at: str
