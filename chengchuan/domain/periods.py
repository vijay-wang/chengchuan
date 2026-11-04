"""Chart periods are independent of a vendor's endpoint names."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChartPeriod:
    code: str
    label: str
    daily: bool = False
    minutes: int | None = None


PERIODS = (
    ChartPeriod("day", "日K", daily=True),
    ChartPeriod("minute", "分时"),
    ChartPeriod("five_day", "五日"),
    ChartPeriod("week", "周K", daily=True),
    ChartPeriod("month", "月K", daily=True),
    ChartPeriod("quarter", "季K", daily=True),
    ChartPeriod("year", "年K", daily=True),
    ChartPeriod("min1", "1分钟K", minutes=1),
    ChartPeriod("min5", "5分钟K", minutes=5),
    ChartPeriod("min15", "15分钟K", minutes=15),
    ChartPeriod("min30", "30分钟K", minutes=30),
    ChartPeriod("min60", "60分钟K", minutes=60),
)
PERIOD_BY_CODE = {p.code: p for p in PERIODS}


def chart_period(code):
    try:
        return PERIOD_BY_CODE[code]
    except KeyError:
        raise ValueError("不支持的图表周期") from None
