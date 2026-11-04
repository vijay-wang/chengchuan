"""沪深交易日历：仅覆盖已核对的2025/2026年，不将未知年份猜成工作日。"""

from datetime import date, timedelta

CALENDAR_VERSION = "shsz-official-2025-2026-v1"
CALENDAR_SOURCES = (
    "https://www.sse.com.cn/disclosure/announcement/general/c/c_20241223_10767108.shtml",
    "https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml",
)
_CLOSED = {
    2025: (
        ("01-01", "01-01"),
        ("01-28", "02-04"),
        ("04-04", "04-06"),
        ("05-01", "05-05"),
        ("05-31", "06-02"),
        ("10-01", "10-08"),
    ),
    2026: (
        ("01-01", "01-03"),
        ("02-15", "02-23"),
        ("04-04", "04-06"),
        ("05-01", "05-05"),
        ("06-19", "06-21"),
        ("09-25", "09-27"),
        ("10-01", "10-07"),
    ),
}


class CalendarUnavailable(ValueError):
    pass


class TradingCalendar:
    version = CALENDAR_VERSION
    first_supported = date(min(_CLOSED), 1, 1)

    def is_session(self, day: date) -> bool:
        if day.year not in _CLOSED:
            raise CalendarUnavailable(f"交易日历未覆盖{day.year}年，需更新官方休市安排")
        md = day.strftime("%m-%d")
        return day.weekday() < 5 and not any(a <= md <= b for a, b in _CLOSED[day.year])

    def shift(self, day: date, count: int) -> date:
        direction = 1 if count >= 0 else -1
        for _ in range(abs(count)):
            day += timedelta(days=direction)
            while not self.is_session(day):
                day += timedelta(days=direction)
        return day

    def previous(self, day: date, count: int) -> tuple[date, ...]:
        rows = []
        for _ in range(count):
            day = self.shift(day, -1)
            rows.append(day)
        return tuple(reversed(rows))

    def sessions(self, first: date, last: date) -> tuple[date, ...]:
        rows = []
        while first <= last:
            if self.is_session(first):
                rows.append(first)
            first += timedelta(days=1)
        return tuple(rows)
