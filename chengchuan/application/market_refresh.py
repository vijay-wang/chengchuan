"""Local SH/SZ refresh decisions; no Qt, storage, credentials or HTTP."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from chengchuan.domain.calendar import CalendarUnavailable, TradingCalendar
from chengchuan.domain.market import INDEX_CATALOG
from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.periods import chart_period


@dataclass(frozen=True)
class MarketCheckpoint:
    live: bool
    cutoff: datetime
    confirm_after: datetime


def market_checkpoint(now, calendar=None):
    """Keep auction/after-hours updates; confirm lunch and final daily data later.

    The chart's ordinary session still ends at 15:00. Fixed-price after-hours
    trading can change daily amounts until 15:30; 15:45 is a publication buffer,
    not an exchange trading time. Calendar coverage is explicit, never guessed.
    """
    if now.utcoffset() is None:
        raise ValueError("自动更新判断须使用带时区的时间")
    now = now.astimezone(SHANGHAI)
    calendar = calendar or TradingCalendar()
    day, clock = now.date(), now.time()
    session = calendar.is_session(day)
    live = session and (time(9, 15) <= clock < time(11, 30) or time(13) <= clock < time(15, 30))
    if live:
        return MarketCheckpoint(True, now, now)
    if session and time(11, 30) <= clock < time(13):
        return MarketCheckpoint(
            False,
            datetime.combine(day, time(11, 30), SHANGHAI),
            datetime.combine(day, time(11, 33), SHANGHAI),
        )
    if not session or clock < time(9, 15):
        day = calendar.shift(day, -1)
    return MarketCheckpoint(
        False,
        datetime.combine(day, time(15), SHANGHAI),
        datetime.combine(day, time(15, 45), SHANGHAI),
    )


def cache_confirmation(now):
    """Minimum underlying acquisition time for the latest closed checkpoint."""
    try:
        point = market_checkpoint(now)
    except CalendarUnavailable:
        return None
    return point.confirm_after if not point.live and now >= point.confirm_after else None


def observed_current(stamps, now):
    try:
        point = market_checkpoint(now)
    except CalendarUnavailable:
        return False
    return point.live or all(point.cutoff <= stamp <= now for stamp in stamps)


def frame_current(frame, point, now):
    if frame is None or frame.as_of is not None:
        return False
    series = frame.snapshot.series
    last = series.bars[-1].timestamp.astimezone(SHANGHAI)
    if last.date() != point.cutoff.date() or not point.confirm_after <= frame.retrieved_at <= now:
        return False
    if not chart_period(series.interval).daily and not point.cutoff <= last <= now:
        return False
    # Daily labels alone cannot prove completion. A source quote, when present,
    # must also reach the checkpoint rather than remain at a midday observation.
    return frame.quote is None or point.cutoff <= frame.quote.timestamp <= now


def details_current(details, point, now):
    return bool(
        details is not None
        and details.as_of is None
        and details.complete
        and details.day == point.cutoff.date()
        and point.confirm_after <= details.retrieved_at <= now
    )


def overview_current(overview, point, now):
    return bool(
        overview is not None
        and overview.complete
        and {i.symbol for i in overview.indices} == {s for s, _, _ in INDEX_CATALOG}
        and point.confirm_after <= overview.retrieved_at <= now
        and all(
            i.day == point.cutoff.date()
            and (i.observed_at is None or point.cutoff <= i.observed_at <= now)
            for i in overview.indices
        )
        and all(b.day == point.cutoff.date() for b in overview.breadth)
    )


def funds_current(funds, point, now):
    return bool(
        funds is not None
        and funds.complete
        and funds.day == point.cutoff.date()
        and point.cutoff <= funds.observed_at <= now
        and point.confirm_after <= funds.retrieved_at <= now
    )


@dataclass(frozen=True)
class RefreshPlan:
    chart: bool = False
    details: bool = False
    overview: bool = False
    funds: bool = False
    force: bool = False
    reason: str = ""


class MarketRefreshController:
    # At most four closed-market attempts per current context/checkpoint.
    # A slow source, suspended stock or unsupported field cannot poll all night.
    retry_seconds = (180, 300, 900)

    def __init__(self, calendar=None):
        self.calendar = calendar or TradingCalendar()
        self.reset()

    def reset(self):
        self._context = None
        self._attempts = 0
        self._next_attempt = None

    def plan(
        self,
        now,
        context,
        frame,
        details=None,
        overview=None,
        funds=None,
        *,
        want_details=False,
        want_overview=False,
        want_funds=False,
    ):
        if now.utcoffset() is None:
            raise ValueError("自动更新判断须使用带时区的时间")
        now = now.astimezone(SHANGHAI)
        try:
            point = market_checkpoint(now, self.calendar)
        except CalendarUnavailable as error:
            return RefreshPlan(reason=str(error) + "；自动更新暂停，可手动F5")
        if point.live:
            self.reset()
            return RefreshPlan(
                True, want_details, want_overview, want_funds, reason="交易更新窗口，按设置间隔检查"
            )
        if now < point.confirm_after:
            return RefreshPlan(reason=f"等待{point.confirm_after:%H:%M}确认数据，暂不自动查询")
        needs = (
            not frame_current(frame, point, now),
            want_details and not details_current(details, point, now),
            want_overview and not overview_current(overview, point, now),
            want_funds and not funds_current(funds, point, now),
        )
        if not any(needs):
            return RefreshPlan(reason=f"缓存已覆盖{point.cutoff:%m-%d %H:%M}，闭市自动查询暂停")
        key = (context, point.cutoff, point.confirm_after)
        if key != self._context:
            self.reset()
            self._context = key
        if self._attempts > len(self.retry_seconds):
            return RefreshPlan(reason="闭市补齐已达4次，自动查询暂停；可手动F5重试")
        if self._next_attempt and now < self._next_attempt:
            return RefreshPlan(reason=f"缓存待补齐，{self._next_attempt:%H:%M:%S}后重试")
        self._attempts += 1
        if self._attempts <= len(self.retry_seconds):
            self._next_attempt = now + timedelta(seconds=self.retry_seconds[self._attempts - 1])
        return RefreshPlan(
            *needs, force=True, reason=f"闭市缓存待补齐，第{self._attempts}/4次自动查询"
        )
