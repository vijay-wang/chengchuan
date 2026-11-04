"""预测研究契约；所有公开概率保持为空，实验概率只用于诊断评分。"""

from dataclasses import dataclass
from datetime import date

TARGET_VERSION = "raw-close-direction-v1-diagnostic"
FEATURE_VERSION = "daily-price-volume-stage-v1"
DIRECTIONS = (-1, 0, 1)
GROUPS = ("A", "B", "C")


@dataclass(frozen=True)
class ForecastConfig:
    neighbors: int = 21
    min_train: int = 40
    train_window: int = 160
    excluded_dates: tuple[str, ...] = ()

    def __post_init__(self):
        if not 3 <= self.neighbors <= 101:
            raise ValueError("相似日数量须为3至101")
        if not self.neighbors <= self.min_train <= self.train_window <= 1000:
            raise ValueError("需满足相似日数量≤最小训练样本≤训练窗口≤1000")
        object.__setattr__(self, "excluded_dates", tuple(sorted(set(self.excluded_dates))))
        for value in self.excluded_dates:
            date.fromisoformat(value)


@dataclass(frozen=True)
class DailyFeatures:
    as_of: date
    revision: str
    values: tuple[tuple[str, float | None], ...]
    eligible: bool
    reasons: tuple[str, ...]
    version: str = FEATURE_VERSION

    def vector(self, group: str):
        if group not in GROUPS:
            raise ValueError("特征组须为A、B或C")
        prefixes = (
            ("price_",)
            if group == "A"
            else (("price_", "volume_") if group == "B" else ("price_", "volume_", "stage_"))
        )
        return tuple(value for name, value in self.values if name.startswith(prefixes))


@dataclass(frozen=True)
class TrainingPoint:
    origin: date
    target: date
    revision: str
    values: tuple[float | None, ...]
    return_pct: float


@dataclass(frozen=True)
class ModelOutput:
    model_id: str
    direction: int | None
    raw_probabilities: tuple[float, float, float]
    neighbor_counts: tuple[int, int, int]
    neighbor_mean_return: float
    train_count: int
    neighbor_dates: tuple[str, ...]


@dataclass(frozen=True)
class Prediction:
    record_id: str
    symbol: str
    as_of: str
    target: str | None
    horizon: int
    group: str
    status: str
    reason: str
    feature_revision: str
    model_id: str | None = None
    direction: int | None = None
    raw_probabilities: tuple[float, float, float] | None = None
    neighbor_counts: tuple[int, int, int] | None = None
    neighbor_mean_return: float | None = None
    train_count: int = 0
    training_label_through: str | None = None
    majority_direction: int | None = None
    momentum_direction: int | None = None
    neighbor_dates: tuple[str, ...] = ()
    calibrated_probabilities: None = None


@dataclass(frozen=True)
class Evaluation:
    prediction_id: str
    actual_return_pct: float | None
    actual_direction: int | None
    correct: bool | None
    reason: str = ""
    status: str = "evaluated"


def direction(value: float) -> int:
    return 1 if value > 0 else (-1 if value < 0 else 0)
