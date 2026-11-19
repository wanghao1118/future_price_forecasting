"""Explainable indicators for exact-month contracts, based on real daily bars.

Public positioning data has its own date and scope. Missing data remains
missing, and a variety-level figure never silently substitutes for a contract.
"""

from datetime import date
import math
import re

from newsdesk.market_data import MarketDataError, number
from newsdesk.trading_sessions import daily_session, session_rules


LOOKBACK = 10


TECHNICAL_LABELS = {
    "ma_trend": "均线趋势",
    "returns": "多周期收益动量",
    "rsi14": "RSI（14）",
    "williams_r14": "Williams %R（14）",
    "adx14": "ADX（14）",
    "cci20": "CCI（20）",
    "atr14": "ATR（14）",
    "bollinger": "布林带（20，2σ）",
    "realized_vol20": "年化波动率（20日）",
    "oi_change": "持仓量变化",
}


def _amount(value):
    return "{:,.0f}".format(value) if value == int(value) else "{:,.2f}".format(value)


def _signed(value, places=2):
    return ("{:+,." + str(places) + "f}").format(value)


def _percent(current, previous):
    # Absolute denominator also describes a change from negative oil prices.
    return None if previous == 0 else (current - previous) / abs(previous) * 100


def _change_text(current, previous):
    change = current - previous
    percentage = _percent(current, previous)
    return "{}{}".format(_signed(change), "（{}%）".format(_signed(percentage))
                         if percentage is not None else "（前值为0，未计算百分比）")


def _source(value):
    source = value if isinstance(value, dict) else {}
    return {"name": str(source.get("name") or "行情来源"), "url": str(source.get("url") or "")}


def _format_indicator(value, places=2, suffix=""):
    if value is None:
        return "暂无数据"
    return ("{:,." + str(places) + "f}{}").format(value, suffix)


def _rolling(values, window, function):
    result = [None] * len(values)
    for index in range(window - 1, len(values)):
        sample = values[index - window + 1:index + 1]
        if all(value is not None for value in sample):
            result[index] = function(sample)
    return result


def _mean(sample):
    return sum(sample) / len(sample) if sample else None


def _std(sample):
    if not sample:
        return None
    average = _mean(sample)
    return math.sqrt(sum((value - average) ** 2 for value in sample) / len(sample))


def _ema(values, window):
    result = [None] * len(values)
    if len(values) < window:
        return result
    alpha = 2 / (window + 1)
    previous = None
    for index, value in enumerate(values):
        if index == window - 1:
            sample = values[:window]
            if all(item is not None for item in sample):
                previous = _mean(sample)
        elif index >= window and previous is not None and value is not None:
            previous = alpha * value + (1 - alpha) * previous
        result[index] = previous
    return result


def _true_ranges(bars):
    result = [None] * len(bars)
    for index, bar in enumerate(bars):
        high, low, close = number(bar.get("high")), number(bar.get("low")), number(bar.get("close"))
        if high is None or low is None:
            continue
        if index == 0:
            result[index] = high - low
            continue
        previous_close = number(bars[index - 1].get("close"))
        if previous_close is None:
            continue
        result[index] = max(high - low, abs(high - previous_close), abs(low - previous_close))
    return result


def _technical_calculations(bars):
    """Return daily technical series from one exact-month contract.

    Each series keeps leading values as None until its lookback is available.
    No interpolation or cross-contract substitution is performed here.
    """
    closes = [number(bar.get("close")) for bar in bars]
    highs = [number(bar.get("high")) for bar in bars]
    lows = [number(bar.get("low")) for bar in bars]
    open_interests = [number(bar.get("open_interest")) for bar in bars]
    true_ranges = _true_ranges(bars)

    ma20 = _rolling(closes, 20, _mean)
    ma60 = _rolling(closes, 60, _mean)
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)

    returns = {}
    for window in (5, 20, 60, 120, 252):
        values = [None] * len(closes)
        for index in range(window, len(closes)):
            current, previous = closes[index], closes[index - window]
            if current is not None and previous not in (None, 0):
                values[index] = (current / previous - 1) * 100
        returns[window] = values

    rsi14 = [None] * len(closes)
    for index in range(14, len(closes)):
        changes = [closes[item] - closes[item - 1] for item in range(index - 13, index + 1)
                   if closes[item] is not None and closes[item - 1] is not None]
        if len(changes) != 14:
            continue
        gains = sum(change for change in changes if change > 0) / 14
        losses = sum(-change for change in changes if change < 0) / 14
        if losses == 0:
            rsi14[index] = 50 if gains == 0 else 100
        else:
            rsi14[index] = 100 - 100 / (1 + gains / losses)

    williams_r14 = [None] * len(closes)
    for index in range(13, len(closes)):
        high_sample = highs[index - 13:index + 1]
        low_sample = lows[index - 13:index + 1]
        close = closes[index]
        if close is None or not all(value is not None for value in high_sample + low_sample):
            continue
        highest, lowest = max(high_sample), min(low_sample)
        williams_r14[index] = -100 * (highest - close) / (highest - lowest) if highest > lowest else None

    cci20 = [None] * len(closes)
    typical = [None if any(value is None for value in values) else sum(values) / 3
               for values in zip(highs, lows, closes)]
    for index in range(19, len(typical)):
        sample = typical[index - 19:index + 1]
        if not all(value is not None for value in sample):
            continue
        average = _mean(sample)
        deviation = _mean([abs(value - average) for value in sample])
        cci20[index] = (typical[index] - average) / (0.015 * deviation) if deviation else 0

    adx14 = [None] * len(closes)
    plus_dm = [None] * len(closes)
    minus_dm = [None] * len(closes)
    for index in range(1, len(closes)):
        high, low = highs[index], lows[index]
        previous_high, previous_low = highs[index - 1], lows[index - 1]
        if None in (high, low, previous_high, previous_low):
            continue
        up_move, down_move = high - previous_high, previous_low - low
        plus_dm[index] = up_move if up_move > down_move and up_move > 0 else 0
        minus_dm[index] = down_move if down_move > up_move and down_move > 0 else 0
    dx = [None] * len(closes)
    for index in range(14, len(closes)):
        tr_sample = true_ranges[index - 13:index + 1]
        plus_sample = plus_dm[index - 13:index + 1]
        minus_sample = minus_dm[index - 13:index + 1]
        if not all(value is not None for value in tr_sample + plus_sample + minus_sample):
            continue
        tr_total = sum(tr_sample)
        if tr_total == 0:
            continue
        plus_di = 100 * sum(plus_sample) / tr_total
        minus_di = 100 * sum(minus_sample) / tr_total
        denominator = plus_di + minus_di
        dx[index] = 100 * abs(plus_di - minus_di) / denominator if denominator else 0
    adx14 = _rolling(dx, 14, _mean)

    atr14 = _rolling(true_ranges, 14, _mean)
    bollinger_middle = _rolling(closes, 20, _mean)
    bollinger_upper = [None] * len(closes)
    bollinger_lower = [None] * len(closes)
    bollinger_pct_b = [None] * len(closes)
    for index in range(19, len(closes)):
        sample = closes[index - 19:index + 1]
        if not all(value is not None for value in sample):
            continue
        deviation = _std(sample)
        middle = bollinger_middle[index]
        upper, lower = middle + 2 * deviation, middle - 2 * deviation
        bollinger_upper[index], bollinger_lower[index] = upper, lower
        bollinger_pct_b[index] = (closes[index] - lower) / (upper - lower) if upper > lower else 0.5

    realized_vol20 = [None] * len(closes)
    for index in range(20, len(closes)):
        daily_returns = []
        for item in range(index - 19, index + 1):
            current, previous = closes[item], closes[item - 1]
            if current is None or previous in (None, 0):
                daily_returns = []
                break
            daily_returns.append(current / previous - 1)
        if len(daily_returns) == 20:
            realized_vol20[index] = _std(daily_returns) * math.sqrt(252) * 100

    oi_change = [None] * len(open_interests)
    for index in range(1, len(open_interests)):
        current, previous = open_interests[index], open_interests[index - 1]
        if current is not None and previous is not None:
            oi_change[index] = current - previous

    return {
        "ma_trend": {"curves": [("price", "收盘价", "价格", "line", closes),
                                  ("ma20", "MA20", "价格", "line", ma20),
                                  ("ma60", "MA60", "价格", "line", ma60),
                                  ("ema12", "EMA12", "价格", "line", ema12),
                                  ("ema26", "EMA26", "价格", "line", ema26)]},
        "returns": {"curves": [("return5", "5日收益", "%", "line", returns[5]),
                                ("return20", "20日收益", "%", "line", returns[20]),
                                ("return60", "60日收益", "%", "line", returns[60]),
                                ("return120", "120日收益", "%", "line", returns[120]),
                                ("return252", "252日收益", "%", "line", returns[252])]},
        "rsi14": {"curves": [("rsi14", "RSI14", "", "line", rsi14)]},
        "williams_r14": {"curves": [("williams_r14", "Williams %R", "", "line", williams_r14)]},
        "adx14": {"curves": [("adx14", "ADX14", "", "line", adx14)]},
        "cci20": {"curves": [("cci20", "CCI20", "", "line", cci20)]},
        "atr14": {"curves": [("atr14", "ATR14", "价格", "line", atr14)]},
        "bollinger": {"curves": [("price", "收盘价", "价格", "line", closes),
                                   ("middle", "中轨 MA20", "价格", "line", bollinger_middle),
                                   ("upper", "上轨", "价格", "line", bollinger_upper),
                                   ("lower", "下轨", "价格", "line", bollinger_lower)],
                      "pct_b": bollinger_pct_b},
        "realized_vol20": {"curves": [("realized_vol20", "年化波动率", "%", "line", realized_vol20)]},
        "oi_change": {"curves": [("oi_change", "日持仓量变化", "手", "bar", oi_change)]},
    }


class MarketIndicators:
    def __init__(self, market_data, supplemental=None):
        self.market_data = market_data
        self.supplemental = supplemental

    def close(self):
        close = getattr(self.supplemental, "close", None)
        if close is not None:
            close()

    def get(self, contract_id, refresh=False, history=False, days=30, start=None, end=None):
        requested_range = start is not None or end is not None
        if requested_range:
            try:
                if not all(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
                           and date.fromisoformat(value).isoformat() == value for value in (start, end)):
                    raise ValueError("invalid range")
                if start > end:
                    raise ValueError("reversed range")
            except ValueError:
                raise MarketDataError("历史范围必须同时提供有效YYYY-MM-DD起止日期，且开始日期不晚于结束日期", status=400, contract_id=contract_id)
        if history and not requested_range and (isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 30):
            raise MarketDataError("指标历史天数必须是1至30的整数", status=400, contract_id=contract_id)
        payload = self.market_data.get(contract_id, "1d", refresh=refresh)
        contract = payload["contract"]
        bars = payload["bars"]
        latest = bars[-1]
        previous = bars[-2] if len(bars) > 1 else None
        window = bars[-LOOKBACK:]
        as_of = latest["trading_date"]
        source = _source(payload.get("source"))
        rows = []
        notes = ["按具体月份合约日线计算；切换小时、周、月K不改变统计周期。",
                 "采用供应商最新日K；最新交易日可能尚未收盘，数据日期逐项列示。"]
        if payload.get("cache", {}).get("stale"):
            notes.append("行情来源刷新失败，价格、成交量及日线持仓指标使用上次成功缓存。")
        if len(window) < LOOKBACK:
            notes.append("该合约仅有{}根有效日K，近10日指标尚不完整。".format(len(window)))

        def row(identifier, label, value, detail, status="available", date=as_of, provider=None):
            item = {"id": identifier, "label": label, "value": value, "detail": detail,
                    "status": status, "as_of": date,
                    "source": _source(provider) if provider is not None else source}
            rows.append(item)
            return item

        # This is the first-to-last close over ten observed trading-day bars,
        # deliberately labelled rather than calling nine intervals a 10d return.
        first = window[0]
        change = latest["close"] - first["close"]
        trend = "上行" if change > 0 else "下行" if change < 0 else "持平"
        trend_pct = _percent(latest["close"], first["close"])
        trend_detail = "最近{}根日K首尾收盘：{} {:.2f} → {} {:.2f}；变动{}。".format(
            len(window), first["trading_date"], first["close"], as_of, latest["close"],
            _change_text(latest["close"], first["close"]))
        if previous:
            trend_detail += " 日变动{}。".format(_change_text(latest["close"], previous["close"]))
        if first["close"] < 0 or previous and previous["close"] < 0:
            trend_detail += " 百分比以基期价格绝对值为分母。"
        if len(window) < LOOKBACK:
            trend_detail += " 不足10根日K。"
        row("price_trend", "价格趋势（近10日）",
            ("{} {}%".format(trend, _signed(trend_pct)) if trend_pct is not None else trend)
            if len(window) > 1 else "数据不足", trend_detail,
            "available" if len(window) == LOOKBACK else "partial")

        traded = number(latest.get("volume"))
        prior_traded = number(previous.get("volume")) if previous else None
        volume_detail = "最新日K成交量。"
        if traded is not None and prior_traded is not None:
            volume_detail += " 较前一交易日{}手。".format(_change_text(traded, prior_traded))
        window_volumes = [number(bar.get("volume")) for bar in window]
        if len(window) == LOOKBACK and all(value is not None for value in window_volumes):
            volume_detail += " 近10日日均量{}手。".format(_amount(sum(window_volumes) / LOOKBACK))
        else:
            volume_detail += " 近10日日均量数据不足，未计算。"
        if traded is None:
            volume_detail = "供应商未提供该合约最新日K的成交量；缺失值未按0处理。"
        row("volume", "成交量", "{} 手".format(_amount(traded)) if traded is not None else "暂无数据",
            volume_detail, "available" if traded is not None else "unavailable")

        if traded is not None and prior_traded is not None and previous:
            price_change = latest["close"] - previous["close"]
            volume_change = traded - prior_traded
            alignment = ("放量" if volume_change > 0 else "缩量" if volume_change < 0 else "平量") + (
                "上涨" if price_change > 0 else "下跌" if price_change < 0 else "持平")
            detail = "{}与{}比较：收盘{}；成交量{}手。".format(
                as_of, previous["trading_date"], _change_text(latest["close"], previous["close"]),
                _change_text(traded, prior_traded))
            row("price_volume", "量价关系", alignment, detail)
        else:
            row("price_volume", "量价关系", "数据不足", "需要相邻两个交易日的收盘价及成交量。", "unavailable")

        extra = {}
        if self.supplemental is not None and not requested_range:
            try:
                extra = self.supplemental.get(contract, as_of=as_of, refresh=refresh) or {}
            except Exception as error:
                # A positioning provider must not hide the available candles.
                notes.append("补充持仓和期权数据请求失败（{}），已保留日线计算结果。".format(type(error).__name__))

        def supplementary(key):
            item = extra.get(key)
            item = item if isinstance(item, dict) else {}
            date = str(item.get("as_of") or "")
            if date and date[:10] > as_of:
                return {"status": "unavailable", "detail": "补充数据日期晚于行情日期，未混入该日指标。",
                        "source": item.get("source"), "as_of": as_of}
            return item

        def extra_status(item, default="available"):
            return "stale" if default == "available" and (
                item.get("status") == "stale" or item.get("cache", {}).get("stale")) else default

        interest = number(latest.get("open_interest"))
        prior_interest = number(previous.get("open_interest")) if previous else None
        oi_extra = supplementary("open_interest")
        oi_date, oi_source = as_of, source
        interest_detail = "最新日K未平仓合约数量。"
        if interest is None:
            interest = number(oi_extra.get("value"))
            prior_interest = number(oi_extra.get("previous"))
            oi_date = oi_extra.get("as_of")
            oi_source = oi_extra.get("source") or source
            interest_detail = str(oi_extra.get("detail") or "公开来源未提供该具体月份合约的持仓量。")
        if interest is not None and interest < 0:
            interest = None
        if interest is not None and prior_interest is not None and prior_interest >= 0:
            interest_detail += " 较{}{}手。".format("前一日K" if oi_date == as_of and latest.get("open_interest") is not None else "前次统计",
                                                    _change_text(interest, prior_interest))
        interest_status = "available" if interest is not None else "unavailable"
        if latest.get("open_interest") is None:
            interest_status = extra_status(oi_extra, interest_status)
        row("open_interest", "持仓量（OI）", "{} 手".format(_amount(interest)) if interest is not None else "暂无数据",
            interest_detail, interest_status, oi_date, oi_source)

        members = supplementary("member_positions")
        longs, shorts = number(members.get("long")), number(members.get("short"))
        if longs is not None and longs < 0 or shorts is not None and shorts < 0:
            longs, shorts = None, None
        member_detail = "；".join(str(value) for value in (members.get("scope"), members.get("detail")) if value)
        member_date, member_source = members.get("as_of"), members.get("source") or source
        if longs is not None and shorts is not None:
            member_detail = "{}{}多头{}手，空头{}手。".format(member_detail, "；" if member_detail else "",
                                                               _amount(longs), _amount(shorts))
            ratio = longs / shorts if shorts else None
            row("member_ratio", "会员多空持仓比", "{:.3f}".format(ratio) if ratio is not None else "无法计算",
                member_detail + " 多头持仓 ÷ 空头持仓。" + (" 空头持仓为0，未计算比值。" if shorts == 0 else ""),
                extra_status(members, "available" if ratio is not None else "unavailable"), member_date, member_source)
            net = longs - shorts
            row("member_net", "会员净持仓", "{} 手（{}）".format(_signed(net, 0), "净多" if net > 0 else "净空" if net < 0 else "多空相等"),
                member_detail + " 多头持仓 − 空头持仓，仅代表所披露会员样本。",
                extra_status(members), member_date, member_source)
        else:
            member_detail = member_detail or "公开来源未提供该具体月份合约的会员多、空持仓数据。"
            for identifier, label in (("member_ratio", "会员多空持仓比"), ("member_net", "会员净持仓")):
                row(identifier, label, "暂无数据", member_detail, "unavailable", member_date, member_source)

        options = supplementary("pcr")
        puts, calls = number(options.get("puts")), number(options.get("calls"))
        basis = str(options.get("basis") or "")
        basis_label = {"open_interest": "持仓量", "volume": "成交量"}.get(basis, basis)
        option_detail = "；".join(str(value) for value in (options.get("scope"), options.get("detail")) if value)
        option_source, option_date = options.get("source") or source, options.get("as_of")
        if puts is not None and calls is not None and puts >= 0 and calls > 0 and basis:
            row("pcr", "PCR（看跌 / 看涨）", "{:.3f}".format(puts / calls),
                "{}{}{}口径：看跌{} ÷ 看涨{}。".format(option_detail, "；" if option_detail else "", basis_label, _amount(puts), _amount(calls)),
                extra_status(options), option_date, option_source)
        else:
            if calls == 0:
                option_detail += " 看涨期权数量为0，未计算比值。"
            if puts is not None and calls is not None and not basis:
                option_detail += " 来源未明确成交量或持仓量口径，未计算比值。"
            row("pcr", "PCR（看跌 / 看涨）", "暂无数据", option_detail or "未取得该合约对应期权的看跌、看涨成交量或持仓量。",
                "unavailable", option_date, option_source)

        high, low = max(bar["high"] for bar in window), min(bar["low"] for bar in window)
        location = (latest["close"] - low) / (high - low) * 100 if high > low else None
        range_detail = "最近{}根日K：最低{:.2f}，最高{:.2f}；（最新收盘 − 区间最低）÷（区间最高 − 区间最低）。".format(len(window), low, high)
        if len(window) < LOOKBACK:
            range_detail += " 不足10根日K。"
        if location is None:
            range_detail += " 最高价等于最低价，未计算位置。"
        row("range_position", "最近10日区间位置", "{:.1f}%".format(location) if location is not None else "无法计算",
            range_detail, "unavailable" if location is None else "available" if len(window) == LOOKBACK else "partial")

        technical = _technical_calculations(bars)
        latest_index = len(bars) - 1

        def technical_value(identifier, curve_id):
            curves = technical[identifier]["curves"]
            for item_id, _label, _unit, _kind, values in curves:
                if item_id == curve_id:
                    return values[latest_index]
            return None

        def technical_status(identifier, curve_id):
            values = next(values for item_id, _label, _unit, _kind, values in technical[identifier]["curves"]
                          if item_id == curve_id)
            latest_value = values[latest_index]
            if latest_value is not None:
                return "available"
            return "partial" if any(value is not None for value in values) else "unavailable"

        def technical_row(identifier, value, detail, curve_id=None):
            status = technical_status(identifier, curve_id or identifier)
            row(identifier, TECHNICAL_LABELS[identifier], value, detail, status)

        technical_row(
            "ma_trend",
            "MA20 {}；MA60 {}；EMA12 {}；EMA26 {}".format(
                _format_indicator(technical_value("ma_trend", "ma20")),
                _format_indicator(technical_value("ma_trend", "ma60")),
                _format_indicator(technical_value("ma_trend", "ema12")),
                _format_indicator(technical_value("ma_trend", "ema26"))),
            "基于收盘价计算 MA20、MA60、EMA12 和 EMA26。图中同时保留收盘价，缺少对应窗口时留空。",
            "ma20")
        technical_row(
            "returns",
            "5日 {}；20日 {}；60日 {}；120日 {}；252日 {}".format(
                _format_indicator(technical_value("returns", "return5"), suffix="%"),
                _format_indicator(technical_value("returns", "return20"), suffix="%"),
                _format_indicator(technical_value("returns", "return60"), suffix="%"),
                _format_indicator(technical_value("returns", "return120"), suffix="%"),
                _format_indicator(technical_value("returns", "return252"), suffix="%")),
            "收盘价相对前5、20、60、120和252个交易日的简单收益率。",
            "return5")
        technical_row("rsi14", _format_indicator(technical_value("rsi14", "rsi14")),
                      "RSI14 使用最近14个交易日的上涨和下跌幅度计算，范围为0至100。")
        technical_row("williams_r14", _format_indicator(technical_value("williams_r14", "williams_r14")),
                      "Williams %R 使用最近14根日K的最高价、最低价和收盘价计算，范围通常为-100至0。")
        technical_row("adx14", _format_indicator(technical_value("adx14", "adx14")),
                      "ADX14 使用真实波幅和方向移动计算趋势强度，不代表方向。")
        technical_row("cci20", _format_indicator(technical_value("cci20", "cci20")),
                      "CCI20 使用典型价格与20日均值偏差计算。")
        technical_row("atr14", _format_indicator(technical_value("atr14", "atr14")),
                      "ATR14 为最近14个交易日真实波幅的均值，单位与合约价格相同。")
        technical_row(
            "bollinger",
            "%B {}；中轨 {}；上轨 {}；下轨 {}".format(
                _format_indicator(technical["bollinger"]["pct_b"][latest_index]),
                _format_indicator(technical_value("bollinger", "middle")),
                _format_indicator(technical_value("bollinger", "upper")),
                _format_indicator(technical_value("bollinger", "lower"))),
            "布林带使用20日均线和两倍20日滚动标准差；%B为（收盘价−下轨）÷（上轨−下轨）。",
            "middle")
        technical_row("realized_vol20", _format_indicator(technical_value("realized_vol20", "realized_vol20"), suffix="%"),
                      "20日收盘收益率标准差按252个交易日年化，缺少完整窗口时留空。")
        technical_row("oi_change", _format_indicator(technical_value("oi_change", "oi_change"), places=0, suffix=" 手"),
                      "相邻交易日日线持仓量变化；供应商未提供持仓量时保留空值。")

        cache = dict(payload.get("cache") or {})
        if any(isinstance(item, dict) and item.get("cache", {}).get("stale") for item in extra.values()):
            cache["stale"] = True
            notes.append("部分补充持仓或期权来源刷新失败，相关行保留带统计日期的上次成功缓存。")
        result = {"contract": contract, "as_of": as_of, "lookback": LOOKBACK, "rows": rows,
                  "cache": cache, "fetched_at": payload.get("fetched_at"), "notes": notes,
                  "display_timezone": "UTC", "session_rules": session_rules(contract.get("familyId"))}
        if history:
            self._add_history(result, payload, days, refresh, start, end)
        return result

    def _add_history(self, result, payload, days, refresh, start=None, end=None):
        """Aligned daily plots. Every point is known on its own trading date."""
        bars = payload["bars"]
        requested_range = start is not None and end is not None
        selected = [bar for bar in bars if start <= bar["trading_date"] <= end] if requested_range else bars[-days:]
        dates = [bar["trading_date"] for bar in selected]
        notes = []
        report = {}
        if dates and self.supplemental is not None and hasattr(self.supplemental, "get_history"):
            try:
                kwargs = {"dates": dates, "refresh": refresh}
                if requested_range:
                    kwargs["background"] = True
                report = self.supplemental.get_history(payload["contract"], **kwargs) or {}
            except Exception as error:
                notes.append("会员及期权历史请求失败（{}）；已保留价格、成交量与日线持仓图。".format(type(error).__name__))
        else:
            notes.append("尚未取得会员及期权历史数据，相关日期保留空缺。")
        notes.extend(str(note) for note in report.get("notes", []) if note)
        snapshots, duplicated = {}, set()
        for snapshot in report.get("snapshots", []):
            if not isinstance(snapshot, dict):
                continue
            day = snapshot.get("as_of") or snapshot.get("date")
            if day not in dates:
                continue
            if day in snapshots or day in duplicated:
                snapshots.pop(day, None)
                duplicated.add(day)
                continue
            snapshots[day] = snapshot
        if duplicated:
            notes.append("补充历史返回重复日期，相关日期未合并计算。")

        def item(day, key):
            value = snapshots.get(day, {}).get(key)
            # Unknown, earlier and later statistics cannot be assigned to this
            # date. A recent quote timestamp is also never an OI history point.
            return value if isinstance(value, dict) and value.get("as_of") == day else {}

        def nonnegative(value):
            value = number(value)
            return value if value is not None and value >= 0 else None

        def member_values(day):
            member = item(day, "member_positions")
            return nonnegative(member.get("long")), nonnegative(member.get("short"))

        def pcr_basis(value):
            return {"open_interest": "open_interest", "持仓量": "open_interest",
                    "volume": "volume", "成交量": "volume"}.get(value)

        # A graph uses a single PCR definition throughout; never join volume
        # and outstanding-position ratios under one unlabeled line.
        basis = next((pcr_basis(item(day, "pcr").get("basis")) for day in reversed(dates)
                      if pcr_basis(item(day, "pcr").get("basis"))), "open_interest")
        basis_label = "持仓量" if basis == "open_interest" else "成交量"
        indices = {bar["trading_date"]: index for index, bar in enumerate(bars)}
        locations = {}
        for bar in selected:
            index = indices[bar["trading_date"]]
            recent = bars[max(0, index - LOOKBACK + 1):index + 1]
            location = None
            if len(recent) == LOOKBACK:
                high = max(bar["high"] for bar in recent)
                low = min(bar["low"] for bar in recent)
                if high > low:
                    location = (bars[index]["close"] - low) / (high - low) * 100
            locations[bars[index]["trading_date"]] = location

        def interest_value(bar):
            value = nonnegative(bar.get("open_interest"))
            if value is not None:
                return value
            return nonnegative(item(bar["trading_date"], "open_interest").get("value"))

        def ratio_value(bar):
            longs, shorts = member_values(bar["trading_date"])
            return longs / shorts if longs is not None and shorts is not None and shorts > 0 else None

        def net_value(bar):
            longs, shorts = member_values(bar["trading_date"])
            return longs - shorts if longs is not None and shorts is not None else None

        def pcr_value(bar):
            options = item(bar["trading_date"], "pcr")
            puts, calls = nonnegative(options.get("puts")), nonnegative(options.get("calls"))
            return puts / calls if puts is not None and calls is not None and calls > 0 and pcr_basis(options.get("basis")) == basis else None

        def curve(identifier, label, unit, kind, compute):
            return {"id": identifier, "label": label, "unit": unit, "kind": kind,
                    "points": [{"time": bar["time"], "date": bar["trading_date"], "value": compute(bar),
                                "session": daily_session(payload["contract"].get("familyId"), bar.get("trading_date"))}
                               for bar in selected]}

        price_unit = payload["contract"].get("unit") or "价格"
        closing = lambda: curve("price", "日收盘价", price_unit, "line", lambda bar: number(bar["close"]))
        volume = lambda: curve("volume", "日成交量", "手", "bar", lambda bar: nonnegative(bar.get("volume")))
        technical = _technical_calculations(bars)
        bar_indices = {bar["trading_date"]: index for index, bar in enumerate(bars)}

        def technical_curve(indicator, curve_id, label, unit, kind):
            values = next(values for item_id, _label, _unit, _kind, values in technical[indicator]["curves"]
                          if item_id == curve_id)
            display_unit = price_unit if unit == "价格" else unit
            return {"id": curve_id, "label": label, "unit": display_unit, "kind": kind,
                    "points": [{"time": bar["time"], "date": bar["trading_date"],
                                "value": values[bar_indices[bar["trading_date"]]],
                                "session": daily_session(payload["contract"].get("familyId"), bar.get("trading_date"))}
                               for bar in selected]}

        technical_series = {}
        for identifier, data in technical.items():
            technical_series[identifier] = [technical_curve(identifier, curve_id, label, unit, kind)
                                            for curve_id, label, unit, kind, _values in data["curves"]]
        series = {
            "price_trend": [closing()],
            "volume": [volume()],
            "price_volume": [closing(), volume()],
            "open_interest": [curve("open_interest", "日持仓量", "手", "line", interest_value)],
            "member_ratio": [curve("member_ratio", "会员多空持仓比", "倍", "line", ratio_value)],
            "member_net": [curve("member_net", "会员净持仓", "手", "bar", net_value)],
            "pcr": [curve("pcr", "PCR（{}）".format(basis_label), "倍", "line", pcr_value)],
            "range_position": [curve("range_position", "滚动10日区间位置", "%", "line",
                                      lambda bar: locations[bar["trading_date"]])],
            **technical_series,
        }
        definitions = {
            "price_trend": "每日收盘价，展示该月份合约的价格走势。",
            "volume": "每日成交量以柱形显示，缺失日期留空。",
            "price_volume": "上图为每日收盘价，下图为每日成交量，共用日期轴；价格与成交量使用各自单位。",
            "open_interest": "每日持仓量；只使用统计日期可核对且与图中日期相同的记录。",
            "member_ratio": "每日所披露会员多头持仓 ÷ 空头持仓，空头为0时留空。",
            "member_net": "每日所披露会员多头持仓 − 空头持仓，正值为净多、负值为净空。",
            "pcr": "每日同标的月份、全部已披露行权价的看跌期权{} ÷ 看涨期权{}；看涨数量为0或缺失时留空，单图保持同一口径。".format(basis_label, basis_label),
            "range_position": "逐日计算：（当日收盘 − 近10日最低）÷（近10日最高 − 近10日最低）×100%；不足10根日K或区间无波动时留空。",
            "ma_trend": "收盘价、MA20、MA60、EMA12 和 EMA26；各均线只在其完整窗口可用后绘制。",
            "returns": "收盘价相对前5、20、60、120和252个交易日的简单收益率。",
            "rsi14": "RSI14，使用最近14个交易日的上涨和下跌幅度计算。",
            "williams_r14": "Williams %R，使用最近14根日K的最高价、最低价和收盘价计算。",
            "adx14": "ADX14，使用真实波幅和方向移动计算趋势强度。",
            "cci20": "CCI20，使用典型价格与20日均值偏差计算。",
            "atr14": "ATR14，为最近14个交易日真实波幅的均值，单位与合约价格相同。",
            "bollinger": "布林带使用20日均线和两倍20日滚动标准差，曲线包含收盘价、中轨、上轨和下轨。",
            "realized_vol20": "20日收盘收益率标准差按252个交易日年化。",
            "oi_change": "相邻交易日日线持仓量变化，供应商未提供持仓量时保留空点。",
        }
        for row in result["rows"]:
            curves = series[row["id"]]
            values = [point["value"] for line in curves for point in line["points"]]
            populated = sum(value is not None for value in values)
            status = "available" if values and populated == len(values) else "partial" if populated else "unavailable"
            detail = definitions[row["id"]]
            if not populated:
                detail += " 当前所选日期没有可核对的历史数据。"
                if row["id"] == "open_interest" and payload["contract"].get("familyId") in {"WTI", "Brent"}:
                    detail += " 现有来源仅有当前持仓快照且未披露统计日期，无法据此绘制历史曲线。"
                elif row["id"] in {"member_ratio", "member_net", "pcr"}:
                    detail += " " + row["detail"]
            elif status == "partial":
                detail += " 无法计算或缺失的日期保留空点，曲线在空点处断开。"
            row.update({"series": curves, "history_status": status, "history_detail": detail})

        history_stale = any(isinstance(value, dict) and value.get("cache", {}).get("stale")
                            for snapshot in snapshots.values() for value in snapshot.values())
        if history_stale:
            result["cache"]["stale"] = True
            notes.append("部分历史补充数据刷新失败，使用原统计日期的上次成功缓存。")
        result["history"] = {"days": len(selected), "requested_days": None if requested_range else days, "dates": dates,
                             "from": dates[0] if dates else None, "to": dates[-1] if dates else None,
                             "requested_from": start, "requested_to": end, "notes": notes,
                             "pending": bool(report.get("pending")), "pending_count": report.get("pending_count", 0),
                             "completed_dates": report.get("completed_dates", len(dates)), "total_dates": len(dates)}
