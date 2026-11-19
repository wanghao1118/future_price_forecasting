"""Contract-specific public positioning and options data, with raw provenance.

INE publishes futures, options and member rankings in daily JSON files.  The
Farmbucks quote endpoint supplies exact NYMEX month-contract open interest but
does not disclose its statistical date; its last-trade time is not an OI date.
"""

import copy
import json
import math
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


MAX_BYTES = 6 * 1024 * 1024
INE_ROOT = "https://www.ine.cn/data/tradedata/"
INE_RANKING_RULE = "https://www.shfe.cn/reports/tradedata/dailyandweeklydata/dailyranking/decl/202401/t20240117_331641.html"
INE_PATHS = {
    "futures": "future/dailydata/kx",
    "members": "future/dailydata/pm",
    "options": "option/dailydata/kx",
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def numeric(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def nonnegative(value):
    result = numeric(value)
    return result if result is not None and result >= 0 else None


def day_label(value):
    text = str(value).replace("-", "")
    try:
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    except ValueError:
        raise ValueError("补充数据未提供可核对的交易日期")


def unavailable(detail, source=None, as_of=None, **extra):
    return {"status": "unavailable", "detail": detail, "source": source or {},
            "as_of": as_of, **extra}


def parse_ine(raw, kind, code, expected_day, source):
    """Read one official day, filtering exact month and ignoring subtotal rows."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("交易所响应不是对象")
    report_day = day_label(data.get("report_date", ""))
    if report_day != expected_day:
        raise ValueError("交易所返回日期与请求不一致")
    key = "o_cursor" if kind == "members" else "o_curinstrument"
    rows = data.get(key)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("交易所响应缺少有效明细数组")
    common = {"status": "available", "as_of": report_day, "source": source}
    symbol = code.lower()
    if kind == "futures":
        matches = [row for row in rows if row.get("PRODUCTID") == "sc_f"
                   and str(row.get("DELIVERYMONTH", "")).strip() == symbol[2:]]
        if not matches:
            return unavailable("交易所日行情未包含 {}。".format(code), source, report_day,
                               value=None, previous=None)
        if len(matches) != 1:
            raise ValueError("交易所返回重复月份合约")
        value = nonnegative(matches[0].get("OPENINTEREST"))
        change = numeric(matches[0].get("OPENINTERESTCHG"))
        if value is None:
            return unavailable("交易所未提供该合约的有效持仓量。", source, report_day,
                               value=None, previous=None)
        previous = value - change if change is not None else None
        if previous is not None and previous < 0:
            previous = None
        return {**common, "value": value, "previous": previous,
                "detail": "{} 官方日终持仓量；前日值由持仓量减当日增减还原。".format(code)}
    if kind == "members":
        matches = [row for row in rows if str(row.get("INSTRUMENTID", "")).strip().lower() == symbol
                   and numeric(row.get("RANK")) is not None and 0 < numeric(row["RANK"]) <= 20]
        scope = "{} 多头与空头各自持仓排名前20名，分别合计；两侧会员可能不同".format(code)
        if not matches:
            return unavailable("交易所当日排名文件未披露 {}，无法计算会员多空比和净持仓。".format(code),
                               source, report_day, long=None, short=None, scope=scope)
        ranks = [numeric(row["RANK"]) for row in matches]
        if len(set(ranks)) != len(ranks):
            raise ValueError("交易所排名中存在重复名次")
        longs = [nonnegative(row.get("CJ2")) for row in matches]
        shorts = [nonnegative(row.get("CJ3")) for row in matches]
        if None in longs or None in shorts:
            return unavailable("交易所会员排名含缺失持仓值，未以零补齐或计算部分合计。", source,
                               report_day, long=None, short=None, scope=scope)
        return {**common, "long": sum(longs), "short": sum(shorts), "scope": scope,
                "detail": "按官方实际披露的 {} 个排名行分别汇总多、空持仓；不含汇总行，不代表全市场净持仓。".format(len(matches))}
    if kind != "options":
        raise ValueError("未知补充数据类型")
    scope = "{} 同标的月份、全部已披露行权价的原油期权".format(code)
    matches = [row for row in rows if str(row.get("UNDERLYINGINSTRID", "")).strip().lower() == symbol
               and re.fullmatch(re.escape(symbol) + r"[cp]\d+(?:\.\d+)?", str(row.get("INSTRUMENTID", "")).lower())]
    if not matches:
        return unavailable("交易所当日期权文件未包含以 {} 为标的的期权。".format(code), source,
                           report_day, puts=None, calls=None, basis="open_interest", scope=scope)
    instruments = [row["INSTRUMENTID"] for row in matches]
    if len(set(instruments)) != len(instruments):
        raise ValueError("交易所期权明细存在重复合约")
    totals = {"1": 0, "2": 0}
    counts = {"1": 0, "2": 0}
    for row in matches:
        side = str(row.get("OPTIONSTYPE", ""))
        value = nonnegative(row.get("OPENINTEREST"))
        suffix = str(row["INSTRUMENTID"])[len(symbol):len(symbol) + 1].upper()
        if side not in totals or suffix != {"1": "C", "2": "P"}.get(side) or value is None:
            return unavailable("同月期权明细含缺失持仓量或类型不一致，未计算不完整PCR。", source,
                               report_day, puts=None, calls=None, basis="open_interest", scope=scope)
        totals[side] += value
        counts[side] += 1
    if not all(counts.values()):
        return unavailable("同月期权缺少看涨或看跌一侧，无法核对PCR。", source, report_day,
                           puts=None, calls=None, basis="open_interest", scope=scope)
    return {**common, "puts": totals["2"], "calls": totals["1"], "basis": "open_interest", "scope": scope,
            "detail": "覆盖同月全部已披露行权价。"}


def parse_farmbucks_quote(raw, code, source):
    data = json.loads(raw)
    if not isinstance(data, dict) or not data.get("ok") or data.get("symbol") != code:
        raise ValueError("供应商报价合约与请求不一致")
    value = nonnegative(data.get("openInterest"))
    if value is None:
        return unavailable("供应商未提供该月份合约的有效持仓量。", source, value=None, previous=None)
    quote_time = str(data.get("lastTrade") or "未提供")
    return {"status": "available", "value": value, "previous": None, "as_of": None,
            "source": source, "quote_time": quote_time,
            "detail": "供应商未披露持仓统计日期，报价显示 {}；无可比前日值。报价时间不作为持仓统计日。".format(quote_time)}


class SupplementalData:
    def __init__(self, cache_dir=None, ttl_seconds=600, timeout=6):
        self.cache_dir = Path(cache_dir) if cache_dir is not None else Path(__file__).resolve().parent / "data" / "market" / "supplemental"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = max(0, ttl_seconds)
        self.timeout = timeout
        self._guard = threading.Lock()
        self._locks = {}
        self._errors = {}
        self._history_executor = None
        self._history_jobs = {}
        self._network_slots = threading.BoundedSemaphore(6)
        self._closed = False

    def close(self, wait=False):
        """Stop accepting work and cancel downloads that have not started."""
        with self._guard:
            self._closed = True
            executor = self._history_executor
            for job in self._history_jobs.values():
                job.cancel()
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=True)

    def _lock(self, key):
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def _read_cache(self, key, source):
        try:
            entry = json.loads((self.cache_dir / (key + ".json")).read_text(encoding="utf-8"))
            if entry.get("kind") != "real_supplemental_response" or entry.get("source") != source or not isinstance(entry.get("raw_response"), str):
                return None
            stamp = datetime.fromisoformat(entry["fetched_at"])
            age = max(0, (datetime.now(timezone.utc) - stamp).total_seconds())
            return entry, age
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _write_cache(self, key, entry):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=self.cache_dir, prefix=".supplemental-", suffix=".json", encoding="utf-8", delete=False) as output:
                temporary = output.name
                json.dump(entry, output, ensure_ascii=False, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.cache_dir / (key + ".json"))
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def _request(self, source, body=None):
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Referer": source["url"]}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        request = Request(source["url"], data=body, headers=headers)
        with self._network_slots:
            if self._closed:
                raise OSError("补充数据客户端已关闭")
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError("补充数据响应超过6MB限制")
                return raw.decode("utf-8-sig")

    def _fetch(self, key, source, validator, refresh=False, body=None):
        with self._lock(key):
            cached = self._read_cache(key, source)
            if cached and not refresh and cached[1] < self.ttl_seconds:
                entry = cached[0]
                try:
                    return validator(entry["raw_response"]), {"hit": True, "stale": False, "fetched_at": entry["fetched_at"]}
                except (ValueError, KeyError, TypeError):
                    # A damaged local file is not an upstream data point.
                    cached = None
            failure = self._errors.get(key)
            if failure and time.monotonic() - failure[0] < 60:
                error = failure[1]
            else:
                try:
                    raw = self._request(source, body)
                    parsed = validator(raw)
                    entry = {"kind": "real_supplemental_response", "source": source,
                             "fetched_at": now(), "raw_response": raw}
                    self._write_cache(key, entry)
                    self._errors.pop(key, None)
                    return parsed, {"hit": False, "stale": False, "fetched_at": entry["fetched_at"]}
                except HTTPError as caught:
                    error = "HTTP {}".format(caught.code)
                except (URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as caught:
                    error = str(caught) or type(caught).__name__
                self._errors[key] = (time.monotonic(), error)
            if cached:
                try:
                    parsed = validator(cached[0]["raw_response"])
                    return parsed, {"hit": True, "stale": True, "error": error, "fetched_at": cached[0]["fetched_at"]}
                except (ValueError, KeyError, TypeError):
                    pass
            return unavailable("{} 请求失败：{}。".format(source["name"], error), source), {"hit": False, "stale": False, "error": error, "fetched_at": None}

    @staticmethod
    def _provenance(result, metadata):
        result = copy.deepcopy(result)
        result["fetched_at"] = metadata["fetched_at"]
        result["cache"] = metadata
        if metadata.get("stale"):
            result["status"] = "stale"
            result["detail"] += " 刷新失败，显示此前抓取的真实缓存（{}）。".format(metadata["error"])
        return result

    def _load_ine(self, kind, code, day, refresh=False):
        compact = day.replace("-", "")
        source = {"name": "上海国际能源交易中心", "url": INE_ROOT + INE_PATHS[kind] + compact + ".dat"}

        def validate(raw):
            # Persist the complete day file so other SC months share it.
            parse_ine(raw, kind, code, day, source)
            return raw

        raw, metadata = self._fetch("ine-" + kind + "-" + compact, source, validate, refresh)
        result = raw if isinstance(raw, dict) else parse_ine(raw, kind, code, day, source)
        return self._provenance(result, metadata)

    def _history_cached(self, kind, code, day):
        """Read one atomic day cache without waiting for an in-flight fetch."""
        compact = day.replace("-", "")
        key = "ine-" + kind + "-" + compact
        source = {"name": "上海国际能源交易中心", "url": INE_ROOT + INE_PATHS[kind] + compact + ".dat"}
        cached = self._read_cache(key, source)
        failure = self._errors.get(key)
        recent_failure = failure is not None and time.monotonic() - failure[0] < 60
        if cached:
            try:
                result = parse_ine(cached[0]["raw_response"], kind, code, day, source)
                metadata = {"hit": True, "stale": bool(recent_failure), "fetched_at": cached[0]["fetched_at"]}
                if recent_failure:
                    metadata["error"] = failure[1]
                result = self._provenance(result, metadata)
                # Closed daily reports can be reused indefinitely. Manual
                # refresh remains available for exchange corrections.
                past = day < datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
                return result, past or cached[1] < self.ttl_seconds
            except (ValueError, TypeError, KeyError):
                pass
        result = unavailable("该日期历史资料正在加载。" if not recent_failure else "历史资料请求失败：{}。".format(failure[1]),
                             source, as_of=day)
        result["cache"] = {"hit": False, "stale": False, "fetched_at": None}
        if recent_failure:
            result["cache"]["error"] = failure[1]
        return result, False

    def _history_task(self, kind, code, day, refresh):
        try:
            return self._load_ine(kind, code, day, refresh)
        except Exception as error:
            # Mark a failed background attempt as finished, with a gap. It
            # must not leave the browser polling an unreachable date forever.
            key = "ine-" + kind + "-" + day.replace("-", "")
            self._errors[key] = (time.monotonic(), type(error).__name__)
            return None

    def _history_background(self, code, valid_dates, refresh):
        snapshots = {day: {"as_of": day, "open_interest": unavailable(
            "历史持仓量采用同日真实日K中的持仓量。", as_of=day, value=None, previous=None)} for day in valid_dates}
        pending = set()
        # Start with the newest visible dates. All viewports and SC months
        # share one executor and one job per official day file.
        for day in reversed(valid_dates):
            for kind in ("members", "options"):
                result, fresh = self._history_cached(kind, code, day)
                snapshots[day]["member_positions" if kind == "members" else "pcr"] = result
                key = "ine-" + kind + "-" + day.replace("-", "")
                failure = self._errors.get(key)
                recent_failure = failure is not None and time.monotonic() - failure[0] < 60
                with self._guard:
                    if self._closed:
                        continue
                    job = self._history_jobs.get(key)
                    active = job is not None and not job.done()
                    if not active and (refresh or not fresh and not recent_failure):
                        if self._history_executor is None:
                            self._history_executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="market-history")
                        self._history_jobs[key] = self._history_executor.submit(self._history_task, kind, code, day, refresh)
                        active = True
                    if active:
                        pending.add((day, kind))
        ordered = [snapshots[day] for day in valid_dates]
        result = self._history_result(ordered)
        result.update({"pending": bool(pending), "pending_count": len(pending),
                       "completed_dates": sum(not any(item[0] == day for item in pending) for day in valid_dates)})
        return result

    @staticmethod
    def _history_result(ordered):
        notes = []
        failed = sum(bool(item[key].get("cache", {}).get("error"))
                     for item in ordered for key in ("member_positions", "pcr"))
        stale = sum(bool(item[key].get("cache", {}).get("stale"))
                    for item in ordered for key in ("member_positions", "pcr"))
        if failed:
            notes.append("历史补充数据有{}个日文件请求失败；其中{}个保留此前真实缓存，其余日期留空。".format(failed, stale))
        if any(item["member_positions"].get("long") is None for item in ordered):
            notes.append("会员排名仅显示交易所实际披露的合约与日期，未披露日期留空。")
        return {"snapshots": ordered, "notes": notes}

    def get_history(self, contract, dates, refresh=False, background=False):
        """Return dated positioning snapshots for actual caller-supplied bars.

        Only official member/options files are downloaded here. Daily K lines
        already contain SC OI, and an undated latest quote cannot supply an OI
        time series. Missing dates remain gaps, never interpolated values.
        """
        if self._closed:
            raise RuntimeError("补充数据客户端已关闭")
        if not isinstance(dates, list) or len(dates) > 1000:
            raise ValueError("历史补充数据最多接受1000个交易日期")
        valid_dates = []
        for value in dates:
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) or day_label(value) != value:
                raise ValueError("历史补充数据日期必须为有效的YYYY-MM-DD")
            if value not in valid_dates:
                valid_dates.append(value)
        valid_dates.sort()
        family = contract.get("familyId")
        code = str(contract.get("code", ""))
        if family in {"WTI", "Brent"} and re.fullmatch(r"(?:CL|BZ)[FGHJKMNQUVXZ]\d{2}", code):
            return {"snapshots": [], "notes": [
                "{} 当前来源仅提供最新报价持仓量，未披露其统计日期及可核对的历史持仓量序列，历史OI留空。".format(code),
                "{} 尚未取得同月历史会员多空持仓及期权PCR明细，对应日期留空。".format(code)]}
        if family != "SC" or not re.fullmatch(r"SC\d{4}", code):
            raise ValueError("未知或无效的补充行情合约")
        if background:
            return self._history_background(code, valid_dates, refresh)
        snapshots = {day: {"as_of": day, "open_interest": unavailable(
            "历史持仓量采用同日真实日K中的持仓量。", as_of=day, value=None, previous=None)} for day in valid_dates}

        def load(task):
            day, kind = task
            return day, kind, self._load_ine(kind, code, day, refresh)

        # A single bounded pool caps network concurrency across all dates.
        # Even if every request times out, 60 files / 6 workers * 6s is bounded;
        # successful or unavailable dates do not block the remaining dates.
        tasks = [(day, kind) for day in valid_dates for kind in ("members", "options")]
        with self._guard:
            if self._closed:
                raise RuntimeError("补充数据客户端已关闭")
            if self._history_executor is None:
                self._history_executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="market-history")
            executor = self._history_executor
        for day, kind, result in executor.map(load, tasks):
            snapshots[day]["member_positions" if kind == "members" else "pcr"] = result
        ordered = [snapshots[day] for day in valid_dates]
        return self._history_result(ordered)

    def get(self, contract, as_of=None, refresh=False):
        if self._closed:
            raise RuntimeError("补充数据客户端已关闭")
        family = contract.get("familyId")
        code = str(contract.get("code", ""))
        if family == "SC" and re.fullmatch(r"SC\d{4}", code):
            day = day_label(str(as_of)[:10]) if as_of else datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            with ThreadPoolExecutor(max_workers=3) as executor:
                fetched = dict(executor.map(lambda kind: (kind, self._load_ine(kind, code, day, refresh)),
                                            ("futures", "members", "options")))
            oi = fetched["futures"]
            members = fetched["members"]
            if members.get("long") is None and oi.get("value") is not None and oi["value"] < 100000 and not members.get("cache", {}).get("error"):
                members["detail"] += "该合约持仓量 {:,.0f} 手，未达到原油10万手的排名披露门槛。".format(oi["value"])
                members["disclosure_rule_url"] = INE_RANKING_RULE
            return {"open_interest": oi, "member_positions": members, "pcr": fetched["options"], "fetched_at": now()}
        if family not in {"WTI", "Brent"} or not re.fullmatch(r"(?:CL|BZ)[FGHJKMNQUVXZ]\d{2}", code):
            raise ValueError("未知或无效的补充行情合约")
        slug = "wti-crude" if family == "WTI" else "brent-crude"
        source = {"name": "Farmbucks", "url": "https://farmbucks.com/futures/energy/" + slug + "/quote"}
        quote, metadata = self._fetch("farmbucks-" + code, source, lambda raw: parse_farmbucks_quote(raw, code, source), refresh,
                                      urlencode({"contract": code}).encode("ascii"))
        oi = self._provenance(quote, metadata)
        return {"open_interest": oi,
                "member_positions": unavailable("当前公开数据源未提供 {} 的会员多空持仓明细，无法计算；不使用全品种周报替代月份合约。".format(code),
                                                 {"name": "Farmbucks", "url": source["url"]}, long=None, short=None,
                                                 scope="{} 月份合约会员持仓".format(code)),
                "pcr": unavailable("当前公开数据源未提供以 {} 为标的的同月期权持仓明细，无法计算PCR。".format(code),
                                    {"name": "Farmbucks", "url": source["url"]}, puts=None, calls=None,
                                    scope="{} 同标的月份期权".format(code), basis="open_interest"),
                "fetched_at": now()}
