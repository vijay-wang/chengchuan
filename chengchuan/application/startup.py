"""Choose the last stock, independently of historical dates and saved selections."""

from chengchuan.domain.market import instrument_symbol, is_index


def startup_target(state, intervals, symbol=None, interval=None):
    if symbol:
        target = instrument_symbol(symbol)
    else:
        target = "sz002318"
        for value in (state.get("last_stock_symbol"), state.get("symbol")):
            try:
                candidate = instrument_symbol(value or "")
            except (ValueError, AttributeError):
                continue
            if not is_index(candidate):
                target = candidate
                break
    period = interval or state.get("last_stock_interval") or state.get("interval") or "day"
    if period not in intervals:
        if interval:
            raise ValueError("当前行情源不支持此周期，请配置猫爪数据或使用日K／分时")
        period = "day"
    return target, period
