"""Local, audited env01 news search exposed as a small stdio MCP server.

Only the host supplies the frozen question and snapshots. The time gate injects
the frozen UTC retrieval cutoff (currently forecast start minus 30 minutes); model arguments never
select a cutoff, file or question. Retrieval filters timestamps first, then the
gate independently validates every returned candidate before release.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid
from zoneinfo import ZoneInfo

from time_gate_agent.local import (LocalTimeGate, RETRIEVAL_POLICY, CURRENT_RETRIEVAL_POLICY,
                                   SUPPORTED_RETRIEVAL_POLICIES)


PROTOCOL = "env01-active-local-news-search-v1"
UTC_SEARCH_PROTOCOL = "env01-active-local-news-search-v2-utc-2h"
CURRENT_UTC_SEARCH_PROTOCOL = "env01-active-local-news-search-v3-utc-30m"
INDICATOR_PROTOCOL = "env01-active-technical-indicator-search-v1"
UTC_INDICATOR_PROTOCOL = "env01-active-technical-indicator-search-v2-utc-2h"
CURRENT_UTC_INDICATOR_PROTOCOL = "env01-active-technical-indicator-search-v3-utc-30m"
RETRIEVAL_TIME_POLICY = CURRENT_RETRIEVAL_POLICY
SUPPORTED_RETRIEVAL_TIME_POLICIES = SUPPORTED_RETRIEVAL_POLICIES
MAX_RESPONSE_BYTES = 45000
MAX_BODY_CHARACTERS = 4000
MAX_TOTAL_BODY_CHARACTERS = 10000
PUBLIC_CASE_FIELDS = frozenset((
    "case_id", "family", "contract_id", "contract_code", "currency", "unit",
    "input_date", "target_date", "current_price", "question", "news_cutoff_exclusive",
    "exchange_timezone", "price_basis", "mode", "current_bar", "series_type",
    "forecast_deadline", "live_deadline_at", "input_ready_at", "time_windows", "retrieval_time_policy",
    "evaluator_id", "evaluator_injection"))
SEARCH_TOOL = {
    "name": "search_news",
    "description": (
        "Search only the already crawled local news archive for this forecast. "
        "The archive is mainly Chinese: choose Chinese or English keywords, for example "
        "原油, OPEC, 库存, 需求, 制裁. Separate multiple keywords with spaces or commas; "
        "matching is OR-ranked. Every recalled article is checked by time gate agent "
        "against this question's frozen time boundary before release. The host time gate "
        "automatically supplies the cutoff exactly two hours before the forecast trading "
        "session starts, in UTC+0. Do not provide or calculate a cutoff argument. The archive "
        "first filters publication times at or before that cutoff, then time gate verifies "
        "the recalled news again. The response includes the precise UTC cutoff. Returned "
        "body text is the stored original text, possibly an explicitly marked excerpt. "
        "No web requests are made. At most 8 searches and 8 articles per search."
    ),
    "inputSchema": {"type": "object", "additionalProperties": False,
                    "properties": {"keywords": {"type": "string", "minLength": 1, "maxLength": 256},
                                   "limit": {"type": "integer", "minimum": 1, "maximum": 8, "default": 5}},
                    "required": ["keywords"]},
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
}
TWO_HOUR_SEARCH_TOOL = json.loads(json.dumps(SEARCH_TOOL))
SEARCH_TOOL["description"] = SEARCH_TOOL["description"].replace("two hours", "30 minutes")
LEGACY_SEARCH_TOOL = json.loads(json.dumps(SEARCH_TOOL))
LEGACY_SEARCH_TOOL["description"] = (
    "Search only the already crawled local news archive for this forecast. Choose Chinese or English "
    "keywords, for example 原油, OPEC, 库存. Matching is OR-ranked. Every recalled article is checked "
    "by time gate against this question's frozen boundary: legacy date rules or the frozen "
    "information_cutoff_utc. Returned text is stored original content or an explicitly marked excerpt. "
    "No web requests. At most 8 searches and 8 articles per search."
)
LEGACY_INDICATOR_SEARCH_TOOL = {
    "name": "search_indicators",
    "description": (
        "Search the frozen technical-indicator archive for this forecast. Query a time range and one indicator "
        "such as candles, price, volume, price_volume, price_trend, range_position, open_interest, member_ratio, "
        "member_net, pcr, ma_trend, returns, rsi14, williams_r14, adx14, cci20, atr14, bollinger, "
        "realized_vol20, or oi_change. Every returned point is checked by time gate agent and must be on or before the input "
        "date and strictly before the predicted target date. History starts 2026-01-01. Both start_date and "
        "end_date are mandatory. An illegal interval is rejected in full with an explanation and allowed dates; "
        "correct your dates and call again. No future or target-day indicator is released."
    ),
    "inputSchema": {"type": "object", "additionalProperties": False,
                    "properties": {
                        "indicator": {"type": "string", "enum": ["candles", "price", "volume", "price_volume",
                                                                    "price_trend", "range_position", "open_interest",
                                                                    "member_ratio", "member_net", "pcr", "ma_trend",
                                                                    "returns", "rsi14", "williams_r14", "adx14", "cci20",
                                                                    "atr14", "bollinger", "realized_vol20", "oi_change"]},
                        "start_date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
                        "end_date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 366, "default": 366}},
                    "required": ["indicator", "start_date", "end_date"]},
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
}

INDICATOR_SEARCH_TOOL = json.loads(json.dumps(LEGACY_INDICATOR_SEARCH_TOOL))
INDICATOR_SEARCH_TOOL["description"] = (
    "Search one indicator from the frozen technical archive. Choose start_time as an ISO 8601 "
    "UTC+0 timestamp with hours and minutes (for example 2026-09-01T08:30:00Z); "
    "optionally choose end_time in the same format. The host time gate automatically supplies "
    "the upper cutoff exactly two hours before the forecast trading session starts. "
    "If end_time is omitted it equals that cutoff; an explicit later end_time is rejected "
    "with the legal range for retry. Do not supply a cutoff argument. History starts "
    "2026-01-01T00:00:00Z. The archive first filters by each observation's precise available_at "
    "time, then time gate independently validates the recalled timestamps. Date-only or "
    "unknown availability is never released. At most 8 valid queries and 366 points per query."
)
_TIME_SCHEMA = {"type": "string", "format": "date-time",
                "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})$"}
INDICATOR_SEARCH_TOOL["inputSchema"] = {
    "type": "object", "additionalProperties": False,
    "properties": {"indicator": LEGACY_INDICATOR_SEARCH_TOOL["inputSchema"]["properties"]["indicator"],
                   "start_time": dict(_TIME_SCHEMA), "end_time": dict(_TIME_SCHEMA),
                   "limit": {"type": "integer", "minimum": 1, "maximum": 366, "default": 366}},
    "required": ["indicator", "start_time"],
}
TWO_HOUR_INDICATOR_SEARCH_TOOL = json.loads(json.dumps(INDICATOR_SEARCH_TOOL))
INDICATOR_SEARCH_TOOL["description"] = INDICATOR_SEARCH_TOOL["description"].replace("two hours", "30 minutes")


def indicator_search_tool(case):
    policy = case.get("retrieval_time_policy")
    return (INDICATOR_SEARCH_TOOL if policy == CURRENT_RETRIEVAL_POLICY else
            TWO_HOUR_INDICATOR_SEARCH_TOOL if policy == RETRIEVAL_POLICY else LEGACY_INDICATOR_SEARCH_TOOL)


def news_search_tool(case):
    policy = case.get("retrieval_time_policy")
    return (SEARCH_TOOL if policy == CURRENT_RETRIEVAL_POLICY else
            TWO_HOUR_SEARCH_TOOL if policy == RETRIEVAL_POLICY else LEGACY_SEARCH_TOOL)


def _utc_timestamp(value):
    """Independent retrieval-side parsing; never interpret a date as midnight."""
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ValueError("unverified_timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _utc_text(value):
    return value.isoformat().replace("+00:00", "Z")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _write_json(path, value):
    path = Path(path)
    _private_dir(path.parent)
    data = canonical(value).encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        os.chmod(temporary, 0o600)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest()}


def _safe_case(case):
    if not isinstance(case, dict) or set(case) - PUBLIC_CASE_FIELDS:
        raise ValueError("invalid_public_case")
    if not all(isinstance(case.get(key), str) and case[key] for key in
               ("case_id", "family", "input_date", "target_date")):
        raise ValueError("invalid_public_case")
    frozen = json.loads(canonical(case))
    audit = LocalTimeGate([], max_articles=0).select(frozen)["audit"]
    if "time_windows" in frozen and not audit.get("time_windows_validated"):
        raise ValueError("invalid_public_time_windows")
    if "retrieval_time_policy" in frozen and not LocalTimeGate.retrieval_boundary(frozen)["allowed"]:
        raise ValueError("invalid_retrieval_boundary")
    ZoneInfo(audit["exchange_timezone"])
    if frozen.get("news_cutoff_exclusive", frozen["target_date"]) != frozen["target_date"]:
        raise ValueError("conflicting_public_cutoff")
    return frozen


from newsdesk.environment_policy import environment_id, NEWS_SEARCH_ENVIRONMENTS


def load_context(path):
    """Validate the host-only context and canonical frozen-snapshot binding."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("absolute_context_required")
    context = json.loads(path.read_text(encoding="utf-8"))
    allowed = {"environment_id", "case", "news_snapshot_path", "news_snapshot_sha256", "indicator_snapshot_path",
               "indicator_snapshot_sha256"}
    if not isinstance(context, dict) or set(context) - allowed or not {"case", "news_snapshot_path", "news_snapshot_sha256"}.issubset(context):
        raise ValueError("invalid_search_context")
    if environment_id(context) not in NEWS_SEARCH_ENVIRONMENTS:
        raise ValueError("news_search_disabled")
    case = _safe_case(context["case"])
    snapshot_path = Path(context["news_snapshot_path"])
    if not snapshot_path.is_absolute():
        raise ValueError("absolute_snapshot_required")
    articles = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(articles, list) or any(not isinstance(row, dict) for row in articles):
        raise ValueError("invalid_news_snapshot")
    if not isinstance(context["news_snapshot_sha256"], str) or digest(articles) != context["news_snapshot_sha256"]:
        raise ValueError("news_snapshot_hash_mismatch")
    return case, articles


def load_indicator_context(path):
    """Load the optional host-bound technical-indicator snapshot."""
    context = json.loads(Path(path).read_text(encoding="utf-8"))
    if environment_id(context) not in ("env00", *NEWS_SEARCH_ENVIRONMENTS):
        raise ValueError("indicator_search_disabled")
    indicator_path = context.get("indicator_snapshot_path")
    if not indicator_path:
        return []
    snapshot_path = Path(indicator_path)
    if not snapshot_path.is_absolute():
        raise ValueError("absolute_indicator_snapshot_required")
    indicators = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(indicators, list) or any(not isinstance(row, dict) for row in indicators):
        raise ValueError("invalid_indicator_snapshot")
    if not isinstance(context.get("indicator_snapshot_sha256"), str) or digest(indicators) != context["indicator_snapshot_sha256"]:
        raise ValueError("indicator_snapshot_hash_mismatch")
    return indicators


def _query_terms(keywords):
    # Preserve whole Chinese phrases with greater weight. Overlapping bigrams
    # are weak fallbacks because the standard library has no CJK word segmenter.
    # Latin words are casefolded. No synonym expansion or generated text occurs.
    whole = re.findall(r"[\u3400-\u9fff]+|[^\W_]+(?:[-'][^\W_]+)*", keywords.casefold(), flags=re.UNICODE)
    terms = {}
    for term in whole:
        terms[term] = max(terms.get(term, 0), 4)
        if re.fullmatch(r"[\u3400-\u9fff]{3,}", term):
            for index in range(len(term) - 1):
                gram = term[index:index + 2]
                terms[gram] = max(terms.get(gram, 0), 1)
    return terms


def _score(article, terms):
    score = 0
    for field, weight in (("title", 5), ("body", 1), ("source_name", 2)):
        text = str(article.get(field) or "").casefold()
        score += sum(weight * strength for term, strength in terms.items() if term in text)
    return score


def _publication_sort_key(article):
    ingestion = article.get("ingestion")
    metadata_day = ingestion.get("published_date", "") if isinstance(ingestion, dict) else ""
    return str(article.get("published_at") or metadata_day or "")


class LocalNewsSearch:
    """Bound local search; private audit is a prerequisite for releasing results."""

    def __init__(self, case, articles, audit_dir, max_calls=8):
        self.case = _safe_case(case)
        self.boundary = LocalTimeGate.retrieval_boundary(self.case) if "retrieval_time_policy" in self.case else None
        self.protocol = (CURRENT_UTC_SEARCH_PROTOCOL if self.case.get("retrieval_time_policy") == CURRENT_RETRIEVAL_POLICY else
                         UTC_SEARCH_PROTOCOL if self.boundary else PROTOCOL)
        self.tool = news_search_tool(self.case)
        if not isinstance(articles, (list, tuple)) or any(not isinstance(row, dict) for row in articles):
            raise ValueError("invalid_news_snapshot")
        self.articles = json.loads(canonical(list(articles)))
        self.snapshot_sha256 = digest(self.articles)
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or not 1 <= max_calls <= 8:
            raise ValueError("invalid_call_limit")
        self.max_calls = max_calls
        self.audit_dir = _private_dir(Path(audit_dir).resolve())
        self._binding = digest({"case": self.case, "news_snapshot_sha256": self.snapshot_sha256})
        self._receipts = []
        self._returned_ids = set()
        self._call_count = 0
        self._error_call_count = 0
        self._returned_count = 0
        summary_path = self.audit_dir / "summary.json"
        if summary_path.exists():
            previous = json.loads(summary_path.read_text(encoding="utf-8"))
            if previous.get("context_sha256") != self._binding:
                raise ValueError("audit_context_mismatch")
            self._receipts = previous["receipts"]
            self._call_count = previous["call_count"]
            self._error_call_count = previous["error_call_count"]
            self._returned_count = previous["returned_article_count"]
            for receipt in self._receipts:
                self._returned_ids.update(canonical(identifier) for identifier in receipt.get("returned_article_ids", []))
        _write_json(summary_path, self.summary())

    def summary(self):
        return {"protocol": self.protocol, "case_id": self.case["case_id"],
                "context_sha256": self._binding, "news_snapshot_sha256": self.snapshot_sha256,
                "call_count": self._call_count, "successful_call_count": self._call_count,
                "execution_count": self._call_count, "error_call_count": self._error_call_count,
                "returned_article_count": self._returned_count,
                "unique_returned_article_count": len(self._returned_ids),
                "receipts": json.loads(canonical(self._receipts)),
                "calls": json.loads(canonical(self._receipts))}

    def _finish(self, directory, request, raw, gate, response, started_at, successful):
        finished_at = _now()
        if self.boundary:
            request["retrieval_boundary"] = self.boundary
            gate["retrieval_boundary"] = self.boundary
            response.update(cutoff_utc=self.boundary["cutoff_utc"],
                            forecast_start_utc=self.boundary["forecast_start_utc"],
                            retrieval_time_policy=self.boundary["policy"], timezone="UTC")
        files = {"request": _write_json(directory / "request.json", request),
                 "raw_recall": _write_json(directory / "raw-recall.json", raw),
                 "time_gate": _write_json(directory / "time-gate.json", gate),
                 "response": _write_json(directory / "response.json", response)}
        returned_ids = [article["id"] for article in response["articles"]]
        receipt = {"protocol": self.protocol, "call_id": directory.name, "case_id": self.case["case_id"],
                   "query": response["query"], "status": response["status"],
                   "successful": successful, "returned_count": response["returned_count"],
                   "returned_article_ids": returned_ids, "started_at": started_at,
                   "finished_at": finished_at, "context_sha256": self._binding,
                   "news_snapshot_sha256": self.snapshot_sha256,
                   "response_sha256": digest(response), "files": files}
        if self.boundary:
            receipt.update(retrieval_boundary=self.boundary, retrieval_time_policy=self.boundary["policy"],
                           cutoff_utc=self.boundary["cutoff_utc"], forecast_start_utc=self.boundary["forecast_start_utc"])
        receipt_file = _write_json(directory / "receipt.json", receipt)
        if successful:
            self._call_count += 1
            self._returned_count += response["returned_count"]
            self._returned_ids.update(canonical(identifier) for identifier in returned_ids)
        else:
            self._error_call_count += 1
        entry = dict(receipt, receipt_path=receipt_file["path"], receipt_sha256=receipt_file["sha256"])
        self._receipts.append(entry)
        _write_json(self.audit_dir / "summary.json", self.summary())
        return response

    def _failed(self, request, reason):
        started = _now()
        directory = _private_dir(self.audit_dir / ("{:04d}-{}".format(len(self._receipts) + 1, uuid.uuid4().hex[:12])))
        query = request.get("keywords") if isinstance(request, dict) else ""
        # Invalid values never get reflected into the model-visible response.
        query = query if isinstance(query, str) and len(query) <= 256 else ""
        response = {"status": "unavailable", "query": query, "articles": [], "returned_count": 0}
        if reason == "search_call_limit_reached":
            response["error"] = {"code": "search_call_limit_reached", "retryable": False,
                                 "message": "本题新闻搜索额度已用完，请根据已有召回内容作答。"}
            response["remaining_calls"] = 0
        return self._finish(directory, {"arguments": request, "started_at": started},
                            {"articles": [], "error": reason}, {"executed": False, "error": reason},
                            response, started, False)

    def invalid_call(self, arguments):
        """Audit an invalid MCP tool call without exposing details to the model."""
        return self._failed(arguments, "invalid_tool_arguments")

    def search(self, keywords, limit=5):
        request = {"keywords": keywords, "limit": limit}
        if (not isinstance(keywords, str) or not keywords.strip() or len(keywords) > 256
                or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8):
            return self._failed(request, "invalid_tool_arguments")
        terms = _query_terms(keywords)
        if not terms:
            return self._failed(request, "invalid_tool_arguments")
        if self._call_count >= self.max_calls:
            return self._failed(request, "search_call_limit_reached")
        started = _now()
        directory = _private_dir(self.audit_dir / ("{:04d}-{}".format(len(self._receipts) + 1, uuid.uuid4().hex[:12])))
        raw = {"articles": []}
        gate_audit = {"agent": LocalTimeGate.name, "protocol": LocalTimeGate.protocol,
                      "executed": True, "checks": [], "network_used": False}
        try:
            candidates, prefilter_checks = [], []
            cutoff = _utc_timestamp(self.boundary["cutoff_utc"]) if self.boundary else None
            for index, row in enumerate(self.articles):
                if cutoff is not None:
                    try:
                        published = _utc_timestamp(row.get("published_at"))
                        reason = "publication_after_retrieval_cutoff" if published > cutoff else "allowed"
                    except (ValueError, TypeError):
                        reason = "publication_timestamp_unverified"
                    prefilter_checks.append({"candidate_index": index, "id": row.get("id"), "reason": reason})
                    if reason != "allowed":
                        continue
                score = _score(row, terms)
                if not score:
                    continue
                candidates.append((index, score, row))
            candidates.sort(key=lambda item: (item[1], _utc_timestamp(item[2]["published_at"]) if self.boundary else _publication_sort_key(item[2]),
                                               str(item[2].get("id") or ""), -item[0]), reverse=True)
            raw = {"query": keywords, "matching": "casefolded OR; whole terms weight 4, CJK bigrams weight 1",
                   "terms": terms, "articles": [row for _, _, row in candidates]}
            if self.boundary:
                raw.update(retrieval_boundary=self.boundary, prefilter_checks=prefilter_checks)
            approved = []
            # A per-candidate gate avoids future/unknown matches displacing safe
            # matches under LocalTimeGate's normal selection cap.
            for index, score, candidate in candidates:
                checked = LocalTimeGate([candidate], max_articles=1).select(self.case)
                gate_audit["checks"].append({"candidate_index": index, "score": score, "audit": checked["audit"]})
                if checked["articles"]:
                    approved.append(checked["articles"][0])
            # Even an empty recall passes the question through the gate.
            if not candidates:
                gate_audit["checks"].append({"audit": LocalTimeGate([], max_articles=0).select(self.case)["audit"]})
            boundary_audit = gate_audit["checks"][0]["audit"]
            for key in ("protocol", "cutoff_utc", "time_windows_validated", "time_windows_error"):
                if key in boundary_audit:
                    gate_audit[key] = boundary_audit[key]
            public = []
            remaining = MAX_TOTAL_BODY_CHARACTERS
            for row in approved[:limit]:
                item = dict(row)
                original_body = str(item.get("body") or "")
                item["body"] = original_body[:min(MAX_BODY_CHARACTERS, remaining)]
                remaining -= len(item["body"])
                item["stored_body_characters"] = len(original_body)
                item["provided_content_scope"] = "excerpt" if len(item["body"]) < len(original_body) else "stored_body"
                # Empty excerpts give no news evidence and are not returned.
                if item["body"]:
                    public.append(item)
            response = {"status": "ok" if public else "no_results", "query": keywords,
                        "articles": public, "returned_count": len(public)}
            if self.boundary:
                response.update(cutoff_utc=self.boundary["cutoff_utc"],
                                forecast_start_utc=self.boundary["forecast_start_utc"],
                                retrieval_time_policy=self.boundary["policy"], timezone="UTC")
            # Metadata is original too; pathological metadata may require fewer
            # articles. Never cut JSON bytes or mislabel a body as complete.
            while len(canonical(response).encode("utf-8")) > MAX_RESPONSE_BYTES and response["articles"]:
                last = response["articles"][-1]
                excess = len(canonical(response).encode("utf-8")) - MAX_RESPONSE_BYTES
                if len(last["body"]) > excess + 1:
                    last["body"] = last["body"][:-(excess + 1)]
                    last["provided_content_scope"] = "excerpt"
                else:
                    response["articles"].pop()
                response["returned_count"] = len(response["articles"])
                response["status"] = "ok" if response["articles"] else "no_results"
            return self._finish(directory, {"arguments": request, "started_at": started}, raw,
                                gate_audit, response, started, True)
        except Exception as exc:
            # Private detail stays in the host audit; the model receives no
            # rejected titles, IDs, counts, paths, or exception messages.
            gate_audit["error"] = type(exc).__name__
            response = {"status": "unavailable", "query": keywords, "articles": [], "returned_count": 0}
            return self._finish(directory, {"arguments": request, "started_at": started}, raw,
                                gate_audit, response, started, False)


class LocalIndicatorSearch:
    """Time-gated search over the frozen daily technical-indicator snapshot."""

    def __init__(self, case, rows, audit_dir, max_calls=8):
        self.case = _safe_case(case)
        self.boundary = LocalTimeGate.retrieval_boundary(self.case) if "retrieval_time_policy" in self.case else None
        self.protocol = (CURRENT_UTC_INDICATOR_PROTOCOL if self.case.get("retrieval_time_policy") == CURRENT_RETRIEVAL_POLICY else
                         UTC_INDICATOR_PROTOCOL if self.boundary else INDICATOR_PROTOCOL)
        self.tool = indicator_search_tool(self.case)
        self.rows = json.loads(canonical(list(rows)))
        self.snapshot_sha256 = digest(self.rows)
        self.context_sha256 = digest({"case": self.case, "indicator_snapshot_sha256": self.snapshot_sha256})
        if type(max_calls) is not int or not 1 <= max_calls <= 8:
            raise ValueError("invalid_call_limit")
        self.max_calls = max_calls
        self.audit_dir = _private_dir(Path(audit_dir).resolve())
        self._calls = []
        self._count = 0
        if (self.audit_dir / "summary.json").is_file():
            previous = json.loads((self.audit_dir / "summary.json").read_text())
            if previous.get("case_id") != self.case["case_id"] or previous.get("indicator_snapshot_sha256") != self.snapshot_sha256:
                raise ValueError("indicator_audit_context_mismatch")
            if self.boundary and previous.get("context_sha256") != self.context_sha256:
                raise ValueError("indicator_audit_context_mismatch")
            self._calls = previous["receipts"]
            self._count = len(self._calls)
        _write_json(self.audit_dir / "summary.json", self.summary())

    def summary(self):
        return {"protocol": self.protocol, "case_id": self.case["case_id"],
                "context_sha256": self.context_sha256,
                "indicator_snapshot_sha256": self.snapshot_sha256,
                "call_count": self._count, "receipts": json.loads(canonical(self._calls))}

    def _audit(self, request, response, checks, prefilter_checks=None):
        directory = _private_dir(self.audit_dir / ("{:04d}-{}".format(len(self._calls) + 1, uuid.uuid4().hex[:12])))
        gate = {"executed": True, "agent": "time gate agent", "input_date": self.case["input_date"], "target_date": self.case["target_date"],
                "query_validation": response.get("query_validation"),
                "checks": checks, "accepted": sum(bool(item.get("accepted")) for item in checks),
                "rejected": sum(not item.get("accepted") for item in checks)}
        stored_request = request
        if self.boundary:
            stored_request = {"arguments": request, "retrieval_boundary": self.boundary}
            response.update(cutoff_utc=self.boundary["cutoff_utc"],
                            forecast_start_utc=self.boundary["forecast_start_utc"],
                            retrieval_time_policy=self.boundary["policy"], timezone="UTC")
            gate["retrieval_boundary"] = self.boundary
            if prefilter_checks is not None:
                gate["retrieval_checks"] = prefilter_checks
        files = {"request": _write_json(directory / "request.json", stored_request),
                 "time_gate": _write_json(directory / "time-gate.json", gate),
                 "response": _write_json(directory / "response.json", response)}
        receipt = {"protocol": self.protocol, "call_id": directory.name,
                   "case_id": self.case["case_id"], "indicator": request.get("indicator"),
                   "indicator_snapshot_sha256": self.snapshot_sha256,
                   "start_date": request.get("start_date"), "end_date": request.get("end_date"),
                   "returned_count": response.get("returned_count", 0), "time_gate": gate, "files": files,
                   "status": response.get("status"),
                   "response_sha256": digest(response)}
        if self.boundary:
            receipt.update(start_time=request.get("start_time"), end_time=request.get("end_time"),
                           retrieval_boundary=self.boundary, context_sha256=self.context_sha256,
                           retrieval_time_policy=self.boundary["policy"],
                           cutoff_utc=self.boundary["cutoff_utc"], forecast_start_utc=self.boundary["forecast_start_utc"])
        receipt_file = _write_json(directory / "receipt.json", receipt)
        entry = dict(receipt, receipt_path=receipt_file["path"], receipt_sha256=receipt_file["sha256"])
        self._calls.append(entry)
        self._count += 1
        _write_json(self.audit_dir / "summary.json", self.summary())
        return response

    def call(self, arguments):
        """Validate model arguments identically for MCP and both API transports."""
        schema = self.tool["inputSchema"]
        if (not isinstance(arguments, dict) or set(arguments) - set(schema["properties"])
                or not set(schema["required"]).issubset(arguments)
                or self.boundary and "end_time" in arguments and not isinstance(arguments["end_time"], str)):
            request = dict(arguments, limit=arguments.get("limit", 366)) if isinstance(arguments, dict) else {}
            return self._audit(request, {"status": "rejected", "rows": [], "returned_count": 0,
                "error": {"code": "invalid_arguments", "retryable": True,
                          "message": "请按工具定义提供检索参数；截止时间由 time gate agent 提供，不能自行覆盖。"}}, [])
        if self.boundary:
            return self.search(arguments["indicator"], start_time=arguments["start_time"],
                               end_time=arguments.get("end_time"), limit=arguments.get("limit", 366))
        return self.search(arguments["indicator"], arguments["start_date"], arguments["end_date"], arguments.get("limit", 366))

    def search(self, indicator, start_date=None, end_date=None, limit=366, *, start_time=None, end_time=None):
        if self.boundary:
            return self._search_time(indicator, start_time if start_time is not None else start_date,
                                     end_time if end_time is not None else end_date, limit)
        request = {"indicator": indicator, "start_date": start_date, "end_date": end_date, "limit": limit}
        query = LocalTimeGate.validate_indicator_query(self.case, start_date, end_date)
        if not query["allowed"]:
            return self._audit(request, {"status": "rejected", "indicator": indicator, "rows": [],
                                        "returned_count": 0, "query_validation": query, "error": query}, [])
        if indicator not in INDICATOR_SEARCH_TOOL["inputSchema"]["properties"]["indicator"]["enum"] or type(limit) is not int or not 1 <= limit <= 366:
            return self._audit(request, {"status": "rejected", "indicator": indicator, "rows": [], "returned_count": 0,
                                        "query_validation": query, "error": {"code": "invalid_arguments", "retryable": True,
                                        "message": "请选择支持的 indicator，limit 必须为 1–366 的整数，然后重新查询。"}}, [])
        if sum(call.get("status") in ("ok", "no_results") for call in self._calls) >= self.max_calls or self._count >= 16:
            return self._audit(request, {"status": "unavailable", "indicator": indicator, "rows": [], "returned_count": 0,
                                        "query_validation": query, "error": {"code": "query_budget_exhausted", "retryable": False,
                                        "message": "本题技术指标查询额度已用完，请根据已有信息作答。"}}, [])
        gated = LocalTimeGate.select_indicator_rows(self.case, self.rows, start_date, end_date)
        checks, approved = gated["checks"], gated["rows"]
        total = len(approved)
        approved = approved[-limit:]
        visible = []
        for row in approved:
            if indicator == "candles":
                value = {key: row.get(key) for key in ("open", "high", "low", "close", "volume")}
            else:
                value = row.get(indicator)
            visible.append({"date": row.get("date"), "indicator": indicator, "value": value,
                            "status": "available" if value is not None else "unavailable"})
        response = {"status": "ok" if visible else "no_results", "indicator": indicator,
                    "start_date": start_date, "end_date": end_date, "rows": approved,
                    "query_validation": query, "matched_count": total, "truncated": total > limit,
                    "available_range": {"start_date": min((row.get("date", "") for row in self.rows if query["allowed_start_date"] <= row.get("date", "") <= query["allowed_end_date"]), default=None),
                                        "end_date": max((row.get("date", "") for row in self.rows if query["allowed_start_date"] <= row.get("date", "") <= query["allowed_end_date"]), default=None)},
                    "returned_count": len(visible),
                    "time_gate": {"checked": len(checks), "accepted": len(visible), "rejected": len(checks) - len(visible),
                                  "input_date": self.case["input_date"], "target_date": self.case["target_date"]}}
        response["rows"] = visible
        return self._audit(request, response, checks)

    def _search_time(self, indicator, start_time, end_time, limit):
        request = {"indicator": indicator, "start_time": start_time, "limit": limit}
        if end_time is not None:
            request["end_time"] = end_time
        query = LocalTimeGate.validate_indicator_time_query(self.case, start_time, end_time)
        if not query["allowed"]:
            return self._audit(request, {"status": "rejected", "indicator": indicator, "rows": [],
                "returned_count": 0, "query_validation": query, "error": query}, [])
        if (indicator not in self.tool["inputSchema"]["properties"]["indicator"]["enum"]
                or type(limit) is not int or not 1 <= limit <= 366):
            return self._audit(request, {"status": "rejected", "indicator": indicator, "rows": [],
                "returned_count": 0, "query_validation": query,
                "error": {"code": "invalid_arguments", "retryable": True,
                          "message": "请选择支持的 indicator，limit 必须为 1–366 的整数。"}}, [])
        if sum(call.get("status") in ("ok", "no_results") for call in self._calls) >= self.max_calls or self._count >= 16:
            return self._audit(request, {"status": "unavailable", "indicator": indicator, "rows": [],
                "returned_count": 0, "query_validation": query,
                "error": {"code": "query_budget_exhausted", "retryable": False,
                          "message": "本题技术指标查询额度已用完，请根据已有信息作答。"}}, [])
        start, end = _utc_timestamp(query["start_time"]), _utc_timestamp(query["end_time"])
        recalled, prefilter = [], []
        for index, row in enumerate(self.rows):
            try:
                indicator_times = row.get("indicator_available_at")
                if isinstance(indicator_times, dict) and indicator in indicator_times:
                    if not indicator_times.get(indicator):
                        raise ValueError("indicator_dependency_time_unverified")
                    timestamps = [_utc_timestamp(indicator_times[indicator])]
                else:
                    timestamps = [_utc_timestamp(row[key]) for key in ("available_at", "timestamp") if row.get(key) is not None]
                if row.get("availability_dependency_unverified") and not indicator_times:
                    raise ValueError("indicator_dependency_time_unverified")
                if not timestamps:
                    raise ValueError("indicator_timestamp_unverified")
                timestamps.extend(_utc_timestamp(row[key]) for key in
                                  ("source_modified_at", "article_modified_at", "modified_at") if row.get(key))
                available = max(timestamps)
                reason = "allowed" if start <= available <= end else "outside_requested_time_range"
            except (ValueError, TypeError, OverflowError):
                reason = "indicator_timestamp_unverified"
            prefilter.append({"row_index": index, "reason": reason})
            if reason == "allowed":
                recalled.append(row)
        gated = LocalTimeGate.select_indicator_time_rows(self.case, recalled, query["start_time"], query["end_time"])
        approved = sorted(gated["rows"], key=lambda row: _utc_timestamp(row["available_at"]))
        total = len(approved)
        visible = []
        for row in approved[-limit:]:
            value = ({key: row.get(key) for key in ("open", "high", "low", "close", "volume")}
                     if indicator == "candles" else row.get(indicator))
            visible.append({"date": row.get("date") or row.get("trading_date"),
                            "available_at": row["available_at"], "indicator": indicator, "value": value,
                            "time_basis": row.get("time_basis") or row.get("available_at_basis"),
                            "status": "available" if value is not None else "unavailable"})
        times = [row["available_at"] for row in approved]
        response = {"status": "ok" if visible else "no_results", "indicator": indicator,
                    "start_time": query["start_time"], "end_time": query["end_time"],
                    "rows": visible, "query_validation": query, "matched_count": total,
                    "truncated": total > limit, "returned_count": len(visible),
                    "available_range": {"start_time": times[0] if times else None, "end_time": times[-1] if times else None},
                    "time_gate": {"checked": len(gated["checks"]), "accepted": total,
                                  "rejected": len(gated["checks"]) - total, "cutoff_utc": self.boundary["cutoff_utc"]}}
        # Retrieval checks remain private; independently checked rows are the only public data.
        return self._audit(request, response, gated["checks"], prefilter_checks=prefilter)


def serve(search, indicator_search=None, input_stream=None, output_stream=None):
    """JSONL JSON-RPC MCP; stdout contains protocol messages only."""
    # Backward-compatible call shape used by the news-only unit tests:
    # serve(search, input_stream, output_stream).
    if indicator_search is not None and hasattr(indicator_search, "read"):
        output_stream = input_stream
        input_stream = indicator_search
        indicator_search = None
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    for line in input_stream:
        try:
            message = json.loads(line)
        except (ValueError, TypeError):
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            if not isinstance(message, dict):
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
            elif "id" not in message:
                continue
            else:
                identifier = message["id"]
                method = message.get("method")
                params = message.get("params", {})
                reply = {"jsonrpc": "2.0", "id": identifier}
                if message.get("jsonrpc") != "2.0" or not isinstance(params, dict):
                    reply["error"] = {"code": -32600, "message": "Invalid request"}
                elif method == "initialize":
                    reply["result"] = {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                                       "capabilities": {"tools": {"listChanged": False}},
                                       "serverInfo": {"name": "env01 news search" if search is not None else "env00 indicators", "version": "1.0.0"}}
                elif method == "ping":
                    reply["result"] = {}
                elif method == "tools/list":
                    reply["result"] = {"tools": ([search.tool] if search is not None else []) + ([indicator_search.tool] if indicator_search is not None else [])}
                elif method == "tools/call":
                    arguments = params.get("arguments", {})
                    name = params.get("name")
                    if (name == "search_news" and search is not None and (set(params) - {"name", "arguments", "_meta"}
                            or not isinstance(arguments, dict) or set(arguments) - {"keywords", "limit"}
                            or "keywords" not in arguments)):
                        response = search.invalid_call(arguments)
                    elif name == "search_news" and search is not None and isinstance(arguments, dict):
                        response = search.search(arguments["keywords"], arguments.get("limit", 5))
                    elif name == "search_indicators" and indicator_search is not None:
                        response = indicator_search.call(arguments if not set(params) - {"name", "arguments", "_meta"} else None)
                    else:
                        response = {"status": "unavailable", "rows": [], "returned_count": 0,
                                    "error": {"code": "unauthorized_tool", "message": "该环境未开放此工具。"}}
                    reply["result"] = {"content": [{"type": "text", "text": canonical(response)}],
                                       "isError": response["status"] == "unavailable"}
                else:
                    reply["error"] = {"code": -32601, "message": "Method not found"}
        output_stream.write(canonical(reply) + "\n")
        output_stream.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Audited local env01 news search MCP server")
    parser.add_argument("--context", required=True)
    parser.add_argument("--audit-dir", required=True)
    args = parser.parse_args(argv)
    try:
        if not Path(args.audit_dir).is_absolute():
            raise ValueError("absolute_audit_directory_required")
        context = json.loads(Path(args.context).read_text(encoding="utf-8"))
        if environment_id(context) not in ("env00", *NEWS_SEARCH_ENVIRONMENTS):
            raise ValueError("all_search_disabled")
        if environment_id(context) == "env00":
            if not Path(args.context).is_absolute() or not context.get("indicator_snapshot_path"):
                raise ValueError("invalid_indicator_context")
            case, search = _safe_case(context["case"]), None
        else:
            case, articles = load_context(args.context)
            search = LocalNewsSearch(case, articles, args.audit_dir)
        indicators = load_indicator_context(args.context)
        indicator_search = (LocalIndicatorSearch(case, indicators, Path(args.audit_dir).parent / "indicators")
                            if indicators or case.get("retrieval_time_policy") in SUPPORTED_RETRIEVAL_POLICIES else None)
        serve(search, indicator_search)
    except Exception:
        # The host can inspect its context; private paths and data never appear
        # in MCP stdout or an exception traceback delivered to the model.
        sys.stderr.write("env01 news search could not start or continue\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
