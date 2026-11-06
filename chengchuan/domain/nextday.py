"""CPU next-session research contracts; scores are not calibrated probabilities."""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

MODES = ("close", "cut1435")
GROUPS = ("price", "volume", "full")
FEATURE_VERSION = "nextday-factors-v1"
TARGET_VERSION = "next-session-raw-ohlc-v2"


@dataclass(frozen=True)
class NextDayConfig:
    neighbors: int = 21
    min_train: int = 60
    train_window: int = 240
    excluded_dates: tuple[str, ...] = ()
    vote_margin: float = 0.08

    def __post_init__(self):
        if any(
            type(value) is not int for value in (self.neighbors, self.min_train, self.train_window)
        ):
            raise ValueError("相似日数量、训练样本数及窗口必须为整数")
        if not 3 <= self.neighbors <= self.min_train <= self.train_window <= 1000:
            raise ValueError("需满足3≤相似日数量≤最小训练样本≤训练窗口≤1000")
        if not 0 <= self.vote_margin <= 0.5:
            raise ValueError("方向分歧阈值须在0至0.5之间")
        object.__setattr__(self, "excluded_dates", tuple(sorted(set(self.excluded_dates))))
        for value in self.excluded_dates:
            date.fromisoformat(value)


@dataclass(frozen=True)
class NextDayFeatures:
    as_of: date
    cutoff_at: datetime
    mode: str
    revision: str
    values: tuple[tuple[str, float | None], ...]
    eligible: bool
    reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    reference_price: float | None = None
    levels: tuple[tuple[str, float], ...] = ()
    evidence: tuple[str, ...] = ()
    coverage_at: str | None = None
    version: str = FEATURE_VERSION

    def selected(self, group="full"):
        if group not in GROUPS:
            raise ValueError("特征组须为price、volume或full")
        prefixes = ("price_",) if group == "price" else ("price_", "volume_")
        return (
            self.values
            if group == "full"
            else tuple((k, v) for k, v in self.values if k.startswith(prefixes))
        )


@dataclass(frozen=True)
class NextDaySample:
    origin: date
    target: date
    available_at: datetime
    revision: str
    values: tuple[tuple[str, float | None], ...]
    close_return: float
    open_return: float
    high_return: float
    low_return: float
    shape: str


@dataclass(frozen=True)
class NextDayEstimate:
    model_id: str
    direction: int | None
    scores: tuple[float, float, float]
    components: tuple[tuple[str, float], ...]
    ranges: tuple[tuple[str, tuple[float, float, float]], ...]
    shapes: tuple[tuple[str, int], ...]
    neighbors: tuple[str, ...]
    contributions: tuple[tuple[str, float], ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class NextDayPrediction:
    record_id: str
    symbol: str
    as_of: str
    cutoff_at: str
    mode: str
    target: str | None
    group: str
    status: str
    reason: str
    feature_revision: str
    train_count: int = 0
    training_label_through: str | None = None
    direction: int | None = None
    model_id: str | None = None
    scores: tuple[float, float, float] | None = None
    components: tuple[tuple[str, float], ...] = ()
    ranges: tuple[tuple[str, tuple[float, float, float]], ...] = ()
    shapes: tuple[tuple[str, int], ...] = ()
    neighbors: tuple[str, ...] = ()
    contributions: tuple[tuple[str, float], ...] = ()
    majority_direction: int | None = None
    momentum_direction: int | None = None
    reference_price: float | None = None
    calibrated_probabilities: None = None


@dataclass(frozen=True)
class NextDayEvaluation:
    prediction_id: str
    actual_return_pct: float | None = None
    actual_direction: int | None = None
    correct: bool | None = None
    actual_shape: str | None = None
    shape_correct: bool | None = None
    actual_ohlc: tuple[tuple[str, float], ...] = ()
    reason: str = ""
    status: str = "evaluated"


@dataclass(frozen=True)
class NextDayRun:
    experiment_id: str
    manifest: dict
    features: NextDayFeatures | None
    predictions: tuple[NextDayPrediction, ...]
    evaluations: tuple[NextDayEvaluation, ...]
    summaries: tuple[dict, ...]
    notes: tuple[str, ...] = ()
    executed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
