"""Date lookup and quote inspection shared by chart views, independent of Qt."""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime

from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.periods import chart_period


def period_key(day, period):
    if period == "week":
        return day.isocalendar()[:2]
    if period == "month":
        return day.year, day.month
    if period == "quarter":
        return day.year, (day.month - 1) // 3
    if period == "year":
        return (day.year,)
    return day


@dataclass(frozen=True)
class DateLocation:
    index: int
    requested: date
    actual: date
    caption: str


def locate_date(series, requested):
    dates = [b.timestamp.astimezone(SHANGHAI).date() for b in series.bars]
    keys = [period_key(d, series.interval) for d in dates]
    key = period_key(requested, series.interval)
    if key < keys[0] or key > keys[-1]:
        raise ValueError("该日期超出当前图表覆盖范围")
    index = bisect_right(keys, key) - 1
    actual = dates[index]
    caption = f"已定位 {actual:%Y-%m-%d}"
    if chart_period(series.interval).daily and series.interval != "day":
        caption = f"对应{chart_period(series.interval).label} · 该根数据截至 {actual:%Y-%m-%d}"
    elif actual != requested:
        caption = f"所选日无记录，定位上一可用交易日 {actual:%Y-%m-%d}"
    return DateLocation(index, requested, actual, caption)


@dataclass(frozen=True)
class InspectedQuote:
    timestamp: datetime
    price: float
    change_pct: float | None
    reference: str


class ChartInspector:
    def __init__(self, series, reference_closes=(), baseline_price=None):
        self.series = series
        self.references = []
        given = dict(reference_closes)
        first_day = series.bars[0].timestamp.astimezone(SHANGHAI).date()
        previous_day = None
        previous_end = None
        day_reference = None
        period = chart_period(series.interval)
        labels = {
            "week": "较前周收盘",
            "month": "较前月收盘",
            "quarter": "较前季收盘",
            "year": "较前年收盘",
        }
        for i, bar in enumerate(series.bars):
            day = bar.timestamp.astimezone(SHANGHAI).date()
            if day != previous_day:
                day_reference = previous_end
                previous_end = None
            if period.daily and series.interval != "day":
                reference = series.bars[i - 1].close if i else None
                label = labels[series.interval]
            elif day in given:
                reference, label = given[day], "较当日昨收"
            elif series.interval == "day":
                reference = series.bars[i - 1].close if i else None
                label = "较前日收盘（未复权）"
            elif day == first_day and baseline_price is not None:
                reference, label = baseline_price, "较当日昨收"
            else:
                reference, label = day_reference, "较前日末价（未复权）"
            self.references.append((reference, label))
            local = bar.timestamp.astimezone(SHANGHAI)
            if local.hour == 15 and local.minute == 0:
                previous_end = bar.close
            previous_day = day

    def inspect(self, index):
        bar = self.series.bars[index]
        reference, label = self.references[index]
        change = (bar.close / reference - 1) * 100 if reference else None
        return InspectedQuote(bar.timestamp, bar.close, change, label)
