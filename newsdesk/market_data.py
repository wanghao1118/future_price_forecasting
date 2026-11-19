"""Public, contract-specific crude futures OHLCV; never synthesizes prices.

Sina serves INE contracts. Farmbucks serves NYMEX WTI and Brent contracts.
The cache retains the exact upstream response alongside normalized OHLCV.
"""

import copy
import json
import math
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from newsdesk.trading_sessions import aggregate_sessions, daily_session, session_rules


MAX_RESPONSE_BYTES = 10 * 1024 * 1024
HISTORY_START = "2026-01-01"
PERIODS = ("1h", "1d", "1w", "1M")
MONTHS = ("2026-12", "2027-01", "2027-02")
FAMILY_SPECS = (
    ("WTI", "WTI 原油", "NYMEX", "USD", "美元 / 桶", ("CLZ26", "CLF27", "CLG27")),
    ("Brent", "Brent 原油", "NYMEX", "USD", "美元 / 桶", ("BZZ26", "BZF27", "BZG27")),
    ("SC", "上海原油", "INE", "CNY", "元 / 桶", ("SC2612", "SC2701", "SC2702")),
)
CONTRACTS = {}
for family, label, exchange, currency, unit, codes in FAMILY_SPECS:
    for month, code in zip(MONTHS, codes):
        identifier = family.lower() + "-" + month.replace("-", "")
        CONTRACTS[identifier] = {
            "id": identifier, "familyId": family, "name": label, "code": code,
            "month": month, "label": "{}年{}月".format(month[:4], int(month[5:])),
            "exchange": exchange, "currency": currency, "unit": unit,
            "provider_symbol": code, "isDemo": False,
        }


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MarketDataError(Exception):
    def __init__(self, message, status=502, source=None, contract_id=None):
        super().__init__(message)
        self.message = str(message)
        self.status = status
        self.source = source
        self.contract_id = contract_id


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def parse_timestamp(value, period, zone="UTC"):
    text = str(value)
    date = datetime.fromisoformat(text.replace("Z", "+00:00"))
    trading_date = text[:10]
    if period == "1d":
        # Date is an exchange trading-date label, not an inferred night session.
        date = datetime.strptime(trading_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    elif date.tzinfo is None:
        date = date.replace(tzinfo=ZoneInfo(zone))
    return int(date.timestamp() * 1000), trading_date


def normalize_rows(rows, period, timezone_name):
    bars = {}
    skipped = 0
    missing_volume = 0
    duplicates = 0
    for row in rows:
        values = [number(row.get(field)) for field in ("open", "high", "low", "close")]
        if any(value is None for value in values):
            skipped += 1
            continue
        opening, high, low, close = values
        if high < max(opening, close) or low > min(opening, close) or low > high:
            skipped += 1
            continue
        try:
            timestamp, trading_date = parse_timestamp(row["date"], period, timezone_name)
        except (KeyError, ValueError, TypeError):
            skipped += 1
            continue
        volume = number(row.get("volume"))
        if volume is None or volume < 0:
            volume = None
            missing_volume += 1
        elif volume.is_integer():
            volume = int(volume)
        open_interest = number(row.get("open_interest"))
        if open_interest is None or open_interest < 0:
            open_interest = None
        elif open_interest.is_integer():
            open_interest = int(open_interest)
        if timestamp in bars:
            duplicates += 1
        bars[timestamp] = {
            "time": timestamp, "trading_date": trading_date,
            "open": opening, "high": high, "low": low, "close": close,
            "volume": volume, "open_interest": open_interest,
        }
    notes = []
    if skipped:
        notes.append("已跳过 {} 个空缺或无效 OHLC 槽位，未作插值。".format(skipped))
    if missing_volume:
        notes.append("{} 根 K 线的成交量未提供，保留为空。".format(missing_volume))
    if duplicates:
        notes.append("{} 个重复时间槽保留供应商最后返回的记录。".format(duplicates))
    return sorted(bars.values(), key=lambda item: item["time"]), notes


def parse_farmbucks(raw, period):
    try:
        data = json.loads(raw)["chartData"]
        expected_interval = "1h" if period == "1h" else "1d"
        if data.get("interval") != expected_interval:
            raise ValueError("供应商返回了不同周期")
        fields = ("labels", "open", "high", "low", "data")
        if not all(isinstance(data.get(field), list) for field in fields):
            raise ValueError("缺少 OHLC 数组")
        size = len(data["labels"])
        if any(len(data[field]) != size for field in fields):
            raise ValueError("OHLC 数组长度不一致")
        volumes = data.get("volume") or [None] * size
        if not isinstance(volumes, list) or len(volumes) != size:
            raise ValueError("成交量数组长度不一致")
        interests = next((data[field] for field in ("openInterest", "open_interest", "oi")
                          if isinstance(data.get(field), list)), [None] * size)
        if len(interests) != size:
            raise ValueError("持仓量数组长度不一致")
        rows = [
            {"date": data["labels"][index], "open": data["open"][index],
             "high": data["high"][index], "low": data["low"][index],
             "close": data["data"][index], "volume": volumes[index],
             "open_interest": interests[index]}
            for index in range(size)
        ]
        bars, notes = normalize_rows(rows, period, "America/New_York")
        if not bars:
            raise ValueError("该月份暂无有效 OHLC 数据")
        return bars, str(data.get("asOf") or rows[-1]["date"]), notes
    except (ValueError, TypeError, KeyError) as error:
        raise MarketDataError("Farmbucks 行情解析失败：{}".format(error))


def parse_sina(raw, period):
    try:
        # Parse JSONP as data only. Never eval the upstream script prefix.
        beginning = raw.index("=(") + 2
        ending = raw.rindex(");")
        data = json.loads(raw[beginning:ending])
        if not isinstance(data, list):
            raise ValueError("供应商未返回行情数组")
        rows = [
            {"date": row.get("d"), "open": row.get("o"), "high": row.get("h"),
             "low": row.get("l"), "close": row.get("c"), "volume": row.get("v"),
             "open_interest": row.get("p")}
            for row in data if isinstance(row, dict)
        ]
        bars, notes = normalize_rows(rows, period, "Asia/Shanghai")
        if not bars:
            raise ValueError("该月份暂无有效 OHLC 数据")
        return bars, str(rows[-1]["date"]), notes
    except (ValueError, TypeError, KeyError) as error:
        raise MarketDataError("新浪行情解析失败：{}".format(error))


def aggregate_bars(bars, period):
    if period not in {"1w", "1M"}:
        raise MarketDataError("不支持的聚合周期", status=400)
    result = []
    session_groups = []
    previous_key = None
    for bar in bars:
        date = datetime.strptime(bar["trading_date"], "%Y-%m-%d")
        key = date.isocalendar()[:2] if period == "1w" else (date.year, date.month)
        if key != previous_key:
            result.append(dict(bar))
            result[-1].pop("session", None)
            session_groups.append([])
            previous_key = key
        else:
            current = result[-1]
            current["high"] = max(current["high"], bar["high"])
            current["low"] = min(current["low"], bar["low"])
            current["close"] = bar["close"]
            current["volume"] = None if current["volume"] is None or bar["volume"] is None else current["volume"] + bar["volume"]
            # Outstanding positions are a snapshot, unlike traded volume.
            current["open_interest"] = bar.get("open_interest")
        if isinstance(bar.get("session"), dict):
            session_groups[-1].append(bar["session"])
    for bar, sessions in zip(result, session_groups):
        if sessions:
            bar["session"] = aggregate_sessions(sessions, period)
    return result


class MarketData:
    def __init__(self, cache_dir=None, ttl_seconds=600):
        self.cache_dir = Path(cache_dir) if cache_dir is not None else Path(__file__).resolve().parent / "data" / "market"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = max(0, ttl_seconds)
        self._lock = threading.Lock()
        self._key_locks = {}
        self._errors = {}
        self._last_success = {}

    def catalog(self):
        families = []
        for family_id, label, exchange, currency, unit, _ in FAMILY_SPECS:
            families.append({
                "id": family_id, "name": family_id, "label": label,
                "exchange": exchange, "currency": currency, "unit": unit,
                "contracts": [dict(item) for item in CONTRACTS.values() if item["familyId"] == family_id],
            })
        return {"families": families, "periods": list(PERIODS)}

    def refresh_all(self):
        """Refresh every configured month from its real upstream source.

        Weekly and monthly candles derive from daily candles, so only the two
        upstream intervals are fetched. A failed refresh can retain an older
        cache for viewing, but it is never counted as a successful update.
        """
        started_at = utc_now()
        series = []
        for contract_id, contract in CONTRACTS.items():
            for period in ("1d", "1h"):
                result = {"contract_id": contract_id, "code": contract["code"], "period": period}
                try:
                    payload = self.get(contract_id, period, refresh=True)
                    result.update({"fetched_at": payload["fetched_at"],
                                   "data_as_of": payload["data_as_of"],
                                   "bars": len(payload["bars"])})
                    if payload.get("cache", {}).get("stale"):
                        result.update({"status": "failed", "error": (payload.get("notes") or [""])[-1]
                                       or "供应商刷新失败，保留此前的真实缓存。"})
                    else:
                        result["status"] = "updated"
                except Exception as error:
                    # One unavailable provider/month must not prevent the
                    # remaining contracts from being refreshed.
                    result.update({"status": "failed", "error": str(error) or type(error).__name__})
                series.append(result)
        failures = [item for item in series if item["status"] == "failed"]
        updated = len(series) - len(failures)
        failed_contracts = {item["contract_id"] for item in failures}
        return {"started_at": started_at, "finished_at": utc_now(),
                "contracts_total": len(CONTRACTS), "series_total": len(series),
                "updated": updated, "failed": len(failures),
                "contracts_updated": len(CONTRACTS) - len(failed_contracts),
                "status": "success" if not failures else "partial" if updated else "failed",
                "series": series, "failures": failures}

    def _key_lock(self, key):
        with self._lock:
            return self._key_locks.setdefault(key, threading.RLock())

    def _source(self, contract, period):
        if contract["familyId"] == "SC":
            endpoint = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_{}=/InnerFuturesNewService.{}".format(
                contract["code"], "getFewMinLine" if period == "1h" else "getDailyKLine")
            params = {"symbol": contract["code"]}
            if period == "1h":
                params["type"] = "60"
            endpoint += "?" + urlencode(params)
            return {"name": "新浪财经", "provider": "sina", "url": "https://finance.sina.com.cn/futures/quotes/{}.shtml".format(contract["code"]),
                    "endpoint": endpoint, "provider_symbol": contract["code"], "interval": period,
                    "timezone": "Asia/Shanghai", "delay": "公开行情，延迟以供应商为准"}
        slug = "wti-crude" if contract["familyId"] == "WTI" else "brent-crude"
        page = "https://farmbucks.com/futures/energy/" + slug
        return {"name": "Farmbucks", "provider": "farmbucks", "url": page,
                "endpoint": page + "/price-history", "provider_symbol": contract["code"],
                "interval": period, "timezone": "America/New_York", "delay": "公开延迟行情（网站标示延迟 10 分钟）"}

    def _request(self, source):
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/javascript,*/*;q=0.8", "Referer": source["url"]}
        body = None
        if source["provider"] == "farmbucks":
            body = urlencode({"window": "5D" if source["interval"] == "1h" else "6M", "contract": source["provider_symbol"], "ohlc": "true"}).encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        request = Request(source["endpoint"], data=body, headers=headers)
        try:
            with urlopen(request, timeout=15) as response:
                declared_length = int(response.headers.get("Content-Length", "0"))
                if declared_length > MAX_RESPONSE_BYTES:
                    raise MarketDataError("供应商响应超过 10 MB 限制", source=source)
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise MarketDataError("供应商响应超过 10 MB 限制", source=source)
                return payload.decode("utf-8-sig")
        except MarketDataError:
            raise
        except HTTPError as error:
            raise MarketDataError("{} 暂时不可用（HTTP {}），未使用替代价格。".format(source["name"], error.code), source=source)
        except (URLError, TimeoutError, OSError, UnicodeError, ValueError) as error:
            raise MarketDataError("{} 行情请求失败：{}".format(source["name"], type(error).__name__), source=source)

    def _parse(self, raw, source, period):
        return parse_sina(raw, period) if source["provider"] == "sina" else parse_farmbucks(raw, period)

    def _path(self, contract_id, period):
        return self.cache_dir / (contract_id + "-" + period + ".json")

    def _decorate_sessions(self, payload, raw_response=None):
        """Apply current schedule metadata to old caches without moving bars."""
        family = payload["contract"]["familyId"]
        period = payload["period"]
        payload["display_timezone"] = "UTC"
        payload["session_rules"] = session_rules(family)
        if period == "1d":
            for bar in payload["bars"]:
                bar["session"] = daily_session(family, bar.get("trading_date"))
        elif period in {"1w", "1M"} and raw_response is not None:
            daily, _, _ = self._parse(raw_response, payload["source"], "1d")
            daily = [bar for bar in daily if bar["trading_date"] >= HISTORY_START]
            for bar in daily:
                bar["session"] = daily_session(family, bar.get("trading_date"))
            def key(bar):
                stamp = datetime.strptime(bar["trading_date"], "%Y-%m-%d")
                return stamp.isocalendar()[:2] if period == "1w" else (stamp.year, stamp.month)
            schedules = {key(bar): bar.get("session") for bar in aggregate_bars(daily, period)}
            for bar in payload["bars"]:
                bar["session"] = schedules.get(key(bar))
        return payload

    def _read(self, contract_id, period):
        try:
            entry = json.loads(self._path(contract_id, period).read_text(encoding="utf-8"))
            if entry.get("kind") != "real_market_response" or entry.get("version") != 1:
                return None
            payload = entry["normalized"]
            if payload["contract"]["id"] != contract_id or payload["period"] != period:
                return None
            if not isinstance(entry["raw_response"], str) or not payload.get("bars"):
                return None
            if any("open_interest" not in bar for bar in payload["bars"]):
                # Reuse the exact saved vendor response when upgrading old
                # caches; no network request or synthetic OI is needed.
                base_period = "1h" if period == "1h" else "1d"
                source = payload.get("source") or self._source(CONTRACTS[contract_id], base_period)
                parsed, _, _ = self._parse(entry["raw_response"], source, base_period)
                if period in {"1w", "1M"}:
                    parsed = aggregate_bars(parsed, period)
                interests = {bar["time"]: bar.get("open_interest") for bar in parsed}
                for bar in payload["bars"]:
                    bar["open_interest"] = interests.get(bar["time"])
            self._decorate_sessions(payload, entry["raw_response"])
            fetched = datetime.fromisoformat(payload["fetched_at"].replace("Z", "+00:00"))
            age = max(0, (datetime.now(timezone.utc) - fetched).total_seconds())
            return entry, age
        except (OSError, ValueError, KeyError, TypeError, MarketDataError):
            return None

    def _write(self, contract_id, period, entry):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.cache_dir, prefix=".market-", suffix=".json", delete=False) as output:
                temporary = output.name
                json.dump(entry, output, ensure_ascii=False, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path(contract_id, period))
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _cached_payload(entry, age, stale=False, error=None):
        result = copy.deepcopy(entry["normalized"])
        result["cache"] = {"hit": True, "stale": stale, "age_seconds": round(age, 2)}
        if stale:
            result["notes"].append("刷新失败，显示此前抓取的真实缓存；不是最新行情。" + (" " + str(error) if error else ""))
        return result

    def get(self, contract_id, period="1d", refresh=False):
        if contract_id not in CONTRACTS:
            raise MarketDataError("未知的具体月份合约", status=404, contract_id=contract_id)
        if period not in PERIODS:
            raise MarketDataError("周期必须是 1h、1d、1w 或 1M", status=400, contract_id=contract_id)
        key = (contract_id, period)
        started = time.monotonic()
        with self._key_lock(key):
            cached = self._read(contract_id, period)
            if cached and period in {"1w", "1M"}:
                # A scheduled daily refresh supersedes a still-within-TTL
                # derived cache. Read the atomic base file without acquiring
                # its lock here; get() below preserves the normal lock order.
                base_cached = self._read(contract_id, "1d")
                if base_cached and (base_cached[0]["raw_response"] != cached[0]["raw_response"]
                                    or base_cached[0]["normalized"]["fetched_at"] > cached[0]["normalized"]["fetched_at"]):
                    cached = None
            just_refreshed = self._last_success.get(key, 0) >= started
            if cached and ((not refresh and cached[1] < self.ttl_seconds) or just_refreshed):
                return self._cached_payload(*cached)
            failed = self._errors.get(key)
            if failed and time.monotonic() - failed[0] < 30:
                if cached:
                    return self._cached_payload(*cached, stale=True, error=failed[1])
                raise failed[1]
            contract = dict(CONTRACTS[contract_id])
            base_period = "1h" if period == "1h" else "1d"
            source = self._source(contract, base_period)
            source["request_url"] = source["endpoint"]
            try:
                if period in {"1w", "1M"}:
                    # Keep the normalized rows and raw response from the same
                    # daily snapshot if a background refresh runs concurrently.
                    with self._key_lock((contract_id, "1d")):
                        base = self.get(contract_id, "1d", refresh=refresh)
                        base_cached = self._read(contract_id, "1d")
                        if not base_cached:
                            raise MarketDataError("真实日线缓存不可用，无法聚合。", source=source)
                        raw = base_cached[0]["raw_response"]
                    bars = aggregate_bars(base["bars"], period)
                    payload = copy.deepcopy(base)
                    payload["period"] = period
                    payload["bars"] = bars
                    payload["source"]["calculated_period"] = period
                    payload["notes"].append("{}由供应商真实日K聚合，最新周期可能尚未结束。".format("周K" if period == "1w" else "月K"))
                    if base["cache"]["stale"]:
                        return payload
                else:
                    raw = self._request(source)
                    bars, as_of, notes = self._parse(raw, source, period)
                    if period == "1d":
                        bars = [bar for bar in bars if bar["trading_date"] >= HISTORY_START]
                        if not bars:
                            raise MarketDataError("来源没有 2026-01-01 起的有效日线", source=source)
                        if bars[0]["trading_date"] > "2026-01-05":
                            notes.append("请求历史起点为 2026-01-01；来源最早可用日线为 {}，此前留空，未用周线或其他合约补齐。".format(bars[0]["trading_date"]))
                    actual_as_of = bars[-1]["trading_date"] if period == "1d" else datetime.fromtimestamp(bars[-1]["time"] / 1000, timezone.utc).isoformat(timespec="seconds")
                    payload = {"contract": contract, "period": period, "bars": bars,
                               "source": source, "fetched_at": utc_now(), "data_as_of": actual_as_of,
                               "source_as_of": as_of,
                               "available_periods": list(PERIODS), "notes": notes}
                    if period == "1d":
                        payload["history_coverage"] = {"requested_start": HISTORY_START,
                            "available_start": bars[0]["trading_date"], "available_end": bars[-1]["trading_date"],
                            "bar_count": len(bars), "earlier_data_unavailable": bars[0]["trading_date"] > "2026-01-05"}
                self._decorate_sessions(payload, raw)
                self._write(contract_id, period, {"kind": "real_market_response", "version": 1, "raw_response": raw, "normalized": payload})
                self._errors.pop(key, None)
                self._last_success[key] = time.monotonic()
                payload["cache"] = {"hit": False, "stale": False, "age_seconds": 0}
                return payload
            except (MarketDataError, OSError) as caught:
                error = caught if isinstance(caught, MarketDataError) else MarketDataError("本地行情缓存写入失败", source=source)
                error.source = error.source or source
                error.contract_id = contract_id
                self._errors[key] = (time.monotonic(), error)
                if cached:
                    return self._cached_payload(*cached, stale=True, error=error)
                raise error
