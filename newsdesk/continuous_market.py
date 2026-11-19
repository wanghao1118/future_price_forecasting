"""Vendor continuous futures OHLC, kept separate from fixed-month contracts.

FT's front-month futures series and Sina's domestic dominant series are
unadjusted vendor series. Foreign Sina CL/OIL are CFDs and are never used here.
"""

import copy
from datetime import datetime, time, timedelta, timezone
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time as clock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from newsdesk.market_data import HISTORY_START, MarketDataError, parse_sina


SPECS = {
    "WTI": {"id": "wti-main", "familyId": "WTI", "code": "CL.1:NYM", "name": "WTI 主连",
            "exchange": "NYMEX", "currency": "USD", "unit": "美元 / 桶", "timezone": "America/New_York"},
    "Brent": {"id": "brent-main", "familyId": "Brent", "code": "IB.1:IEU", "name": "Brent 主连",
              "exchange": "ICE", "currency": "USD", "unit": "美元 / 桶", "timezone": "Europe/London"},
    "SC": {"id": "sc-main", "familyId": "SC", "code": "SC0", "name": "SC 主连",
           "exchange": "INE", "currency": "CNY", "unit": "元 / 桶", "timezone": "Asia/Shanghai"},
}
MAX_BYTES = 8 * 1024 * 1024


def utc_now():
    return datetime.now(timezone.utc)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def parse_ft(raw):
    """Parse FT's public HistoricalPricesApp response without executing HTML."""
    response = json.loads(raw)
    if not isinstance(response.get("html"), str):
        raise MarketDataError("FT 主连响应没有历史 OHLC 表")
    bars, skipped = {}, 0
    for fragment in re.findall(r"<tr\b[^>]*>(.*?)</tr>", response["html"], re.S | re.I):
        cells = []
        for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", fragment, re.S | re.I):
            # FT includes both long desktop and short mobile representations.
            preferred = re.search(r'<span[^>]*class="[^"]*mod-ui-hide-small-below[^"]*"[^>]*>(.*?)</span>', cell, re.S)
            cells.append(html.unescape(re.sub(r"<[^>]*>", "", preferred.group(1) if preferred else cell)).strip())
        if len(cells) != 6:
            skipped += 1
            continue
        try:
            stamp = datetime.strptime(cells[0], "%A, %B %d, %Y").replace(tzinfo=timezone.utc)
            opening, high, low, close = [float(value.replace(",", "")) for value in cells[1:5]]
            if not all(_finite(value) for value in (opening, high, low, close)) or high < max(opening, close) or low > min(opening, close) or high < low:
                raise ValueError("invalid OHLC")
            try:
                volume = float(cells[5].replace(",", ""))
                if not math.isfinite(volume) or volume < 0:
                    volume = None
                elif volume.is_integer():
                    volume = int(volume)
            except ValueError:
                volume = None
            day = stamp.date().isoformat()
            bars[day] = {"trading_date": day, "time": int(stamp.timestamp() * 1000),
                         "open": opening, "high": high, "low": low, "close": close, "volume": volume}
        except (ValueError, TypeError):
            skipped += 1
    if not bars:
        raise MarketDataError("FT 主连没有有效真实 OHLC")
    return sorted(bars.values(), key=lambda row: row["trading_date"]), (["跳过 {} 条无效 OHLC。".format(skipped)] if skipped else [])


def mark_complete(bars, family, fetched_at):
    """Exclude a running session; today's row needs a post-close observation.

    The buffer is deliberately after the exchange daily session close. This
    is a daily-close heuristic, not a claim of exchange final-settlement data.
    """
    zone = ZoneInfo(SPECS[family]["timezone"])
    local = fetched_at.astimezone(zone)
    close = time(15, 30) if family == "SC" else time(23, 30) if family == "Brent" else time(17, 30)
    completed = []
    result = []
    for row in bars:
        value = dict(row)
        day = datetime.fromisoformat(row["trading_date"]).date()
        value["is_complete"] = day < local.date() or (day == local.date() and local.time().replace(tzinfo=None) >= close)
        if value["is_complete"]:
            completed.append(row["trading_date"])
        result.append(value)
    return result, max(completed, default=None)


class ContinuousMarketData:
    def __init__(self, cache_dir=None, ttl_seconds=600):
        self.cache_dir = Path(cache_dir) if cache_dir is not None else Path(__file__).resolve().parent / "data" / "continuous-market"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = max(0, ttl_seconds)
        self._locks = {family: threading.Lock() for family in SPECS}
        self._errors = {}

    @staticmethod
    def _source(family):
        spec = SPECS[family]
        if family == "SC":
            return {"name": "新浪财经 SC 主力连续", "provider": "sina_domestic_continuous",
                    "url": "https://finance.sina.com.cn/futures/quotes/SC0.shtml",
                    "endpoint": "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_SC0=/InnerFuturesNewService.getDailyKLine?symbol=SC0",
                    "continuous_method": "provider_dominant_continuous", "timezone": spec["timezone"],
                    "series_type": "dominant_continuous", "adjustment": "unadjusted"}
        today = utc_now().astimezone(ZoneInfo(spec["timezone"])).date()
        params = {"startDate": HISTORY_START.replace("-", "/"),
                  "endDate": today.strftime("%Y/%m/%d"), "symbol": "1070572" if family == "WTI" else "1054972"}
        return {"name": "Financial Times / LSEG 近月连续期货", "provider": "ft_continuous_futures",
                "url": "https://markets.ft.com/data/commodities/tearsheet/historical?" + urlencode({"c": "WTI Crude Oil" if family == "WTI" else "Brent Crude Oil"}),
                "endpoint": "https://markets.ft.com/data/equities/ajax/get-historical-prices?" + urlencode(params),
                "continuous_method": "provider_front_month_continuous", "timezone": spec["timezone"],
                "series_type": "dominant_continuous", "adjustment": "unadjusted"}

    def _request(self, source):
        request = Request(source["endpoint"], headers={"User-Agent": "Mozilla/5.0", "Referer": source["url"],
                                                      "Accept": "application/json,text/javascript,*/*"})
        try:
            with urlopen(request, timeout=20) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise MarketDataError("主连响应超过大小限制", source=source)
            return raw.decode("utf-8-sig")
        except HTTPError as exc:
            raise MarketDataError("{} 暂不可用（HTTP {}）；不使用月份合约或 CFD 替代。".format(source["name"], exc.code), source=source) from None
        except (URLError, TimeoutError, OSError, UnicodeError) as exc:
            raise MarketDataError("{} 请求失败：{}".format(source["name"], type(exc).__name__), source=source) from None

    def _read(self, family):
        try:
            value = json.loads((self.cache_dir / (family.lower() + "-main-1d.json")).read_text())
            raw = value["raw_response"]
            if value.get("kind") != "real_continuous_response" or hashlib.sha256(raw.encode()).hexdigest() != value["normalized"]["raw_response_sha256"]:
                return None
            data = value["normalized"]
            signature = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            if value.get("normalized_sha256") != signature or data.get("raw_response") != raw:
                return None
            if data["contract"]["familyId"] != family or data["contract"]["series_type"] != "dominant_continuous":
                return None
            age = max(0, (utc_now() - datetime.fromisoformat(data["fetched_at"])).total_seconds())
            return data, age
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write(self, family, raw, payload):
        signature = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.cache_dir, delete=False) as handle:
            json.dump({"kind": "real_continuous_response", "raw_response": raw, "normalized": payload,
                       "normalized_sha256": signature}, handle, ensure_ascii=False, allow_nan=False)
            temporary = handle.name
        os.replace(temporary, self.cache_dir / (family.lower() + "-main-1d.json"))

    def get(self, family, refresh=False):
        if family not in SPECS:
            raise ValueError("未知主连品类")
        with self._locks[family]:
            cached = self._read(family)
            if cached and not refresh and cached[1] < self.ttl_seconds:
                result = copy.deepcopy(cached[0])
                result["cache"] = {"hit": True, "stale": False, "age_seconds": cached[1]}
                return result
            source = self._source(family)
            try:
                failure = self._errors.get(family)
                if failure and clock.monotonic() - failure[0] < 300:
                    raise failure[1]
                raw = self._request(source)
                if family == "SC":
                    bars, _, notes = parse_sina(raw, "1d")
                else:
                    bars, notes = parse_ft(raw)
                bars = [bar for bar in bars if bar["trading_date"] >= HISTORY_START]
                if not bars:
                    raise MarketDataError("来源没有 2026-01-01 起的有效主连日线")
                fetched = utc_now()
                bars, complete = mark_complete(bars, family, fetched)
                gaps = []
                for previous, following in zip(bars, bars[1:]):
                    day = datetime.fromisoformat(previous["trading_date"]).date() + timedelta(days=1)
                    until = datetime.fromisoformat(following["trading_date"]).date()
                    while day < until:
                        if day.weekday() < 5:
                            gaps.append(day.isoformat())
                        day += timedelta(days=1)
                if gaps:
                    notes.append("存在 {} 个未核实的工作日日线缺口/休市日，跨缺口日对不构建回测题或实盘真值。".format(len(gaps)))
                contract = {**SPECS[family], "isDemo": False, "series_type": "dominant_continuous"}
                payload = {"contract": contract, "period": "1d", "bars": bars, "source": source,
                           "fetched_at": fetched.isoformat(timespec="seconds"), "data_as_of": bars[-1]["trading_date"],
                           "completed_through": complete, "raw_response": raw,
                           "history_coverage": {"requested_start": HISTORY_START, "available_start": bars[0]["trading_date"],
                                                "available_end": bars[-1]["trading_date"], "bar_count": len(bars)},
                           "unverified_weekday_gaps": gaps,
                           "raw_response_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                           "notes": notes + ["供应商未复权连续序列，保留换月跳空；不从当前月份合约反推历史主连。", "当日日线须在交易所日盘/电子盘收盘后再留30分钟更新缓冲，才视为完整收盘数据。"],
                           "cache": {"hit": False, "stale": False}}
                self._write(family, raw, payload)
                self._errors.pop(family, None)
                return payload
            except (MarketDataError, ValueError) as exc:
                self._errors[family] = (clock.monotonic(), exc)
                if not cached:
                    raise
                result = copy.deepcopy(cached[0])
                result["cache"] = {"hit": True, "stale": True, "age_seconds": cached[1], "error": str(exc)}
                result["notes"].append("刷新失败，仅保留此前真实主连缓存；实盘不可据此标为数据就绪。")
                return result
