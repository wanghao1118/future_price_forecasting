"""Host orchestration for the ``time gate agent``.

Publication reviews, fetched evidence and callback traces are private host
artifacts.  Only the derived policy and the original Guard responses are safe
to expose to a prediction model.  Callbacks receive public task fields only;
they must run independently of predictors, labels and scoring state.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import secrets
import threading

from . import boundary, guard


AGENT_NAME = "time gate agent"


def _clone(value):
    return json.loads(boundary.canonical(value))


def _write_private(path, value):
    """Create, rather than replace, a private record with mode 0600."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(boundary.canonical(value))
    path.chmod(0o600)
    return {"path": str(path.resolve()), "sha256": boundary.file_sha256(path)}


def publication_quote_supported(quote, text, metadata):
    """Match the original runner's exact publication-date quote checks.

    Whitespace is normalized. A joined quote is permitted only when every
    semicolon/newline-separated span is itself a contiguous original span of
    at least eight characters; this never treats a paraphrase as evidence.
    """
    if not all(isinstance(value, str) for value in (quote, text, metadata)):
        return False
    normalize = lambda value: " ".join(value.split())
    raw_quote = quote
    quote = normalize(quote)
    body = normalize(text + "\n" + metadata)
    if not quote:
        return False
    if quote in body:
        return True
    spans = [normalize(value) for value in re.split(r"\s*;\s*|\s*\n\s*", raw_quote) if value.strip()]
    return len(spans) > 1 and all(len(span) >= 8 and span in body for span in spans)


class TimeGateAgent:
    """Lazy three-review boundary followed by independent per-article review.

    Each callback has signature ``(material, schema, work: Path) -> dict``.
    ``boundary_reviewer`` is called separately for each ``boundary.ARMS`` entry
    with ``material['review_arm']`` identifying the independent review.  A
    callback may retain its actual external-tool trace at
    ``work / 'tool-trace.json'``. Missing traces are explicitly recorded as
    callback-supplied results, never represented as model/tool credentials.
    """

    name = AGENT_NAME

    def __init__(self, task, private_dir, boundary_reviewer, discover, reviewer,
                 fetcher=guard.fetch_public):
        if not isinstance(task, dict):
            raise boundary.BoundaryReviewError("public_task_must_be_an_object")
        for field in ("case_id", "question"):
            if not isinstance(task.get(field), str) or not task[field].strip():
                raise boundary.BoundaryReviewError("invalid_public_" + field)
        # Validate before selecting fields: silently dropping an unknown field
        # could conceal an accidentally supplied label or previous prediction.
        self._review_material = boundary.build_review_material(task)
        self._task = _clone(task)
        self._review_material = _clone(self._review_material)
        try:
            boundary.calendar_day(self._task["cutoff"][:10])
        except (KeyError, TypeError) as exc:
            raise boundary.BoundaryReviewError("invalid_public_cutoff") from exc
        for callback in (boundary_reviewer, discover, reviewer, fetcher):
            if not callable(callback):
                raise TypeError("time gate agent callbacks must be callable")
        self.private_dir = Path(private_dir).expanduser().resolve()
        self.private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.private_dir.chmod(0o700)
        self.boundary_reviewer = boundary_reviewer
        self.discover = discover
        self.reviewer = reviewer
        self.fetcher = fetcher
        self._lock = threading.Lock()
        self._boundary = None
        self._boundary_reviews = None
        self._guard = None
        self._session = secrets.token_hex(8)

    def _unresolved(self):
        return {
            "status": "unresolved", "case_id": self._task["case_id"],
            "public_task_sha256": boundary.digest(self._task),
            "answer_first_publication_date": None,
            "actual_publication_verified": False,
            "earlier_result_search_completed": False,
            "review_reason": "unresolved_publication_day", "evidence": [],
        }

    def _verify_evidence(self, item, index, work):
        """Independently fetch every claimed source and retain its receipt."""
        checked = dict(item) if isinstance(item, dict) else {}
        checked["original_publication_date_verified"] = False
        # A callback cannot supply its own host proof or source receipt.
        checked.pop("source_receipt", None)
        source_record = {"requested_url": checked.get("url"), "fetched_at": guard.utc_now()}
        try:
            day = boundary.calendar_day(checked.get("answer_publication_date")).isoformat()
            quote = checked.get("date_evidence_quote")
            page = self.fetcher(checked.get("url"))
            if not isinstance(page, guard.FetchedPage):
                raise ValueError("source_fetch_did_not_return_a_complete_page")
            source_record["page"] = page.material()
            metadata = guard.canonical(page.metadata_dates)
            known = set()
            for entry in page.metadata_dates:
                if isinstance(entry, dict) and entry.get("kind") == "published":
                    known.update(guard._dates_in(str(entry.get("value", ""))))
            supported = (
                not page.truncated
                and publication_quote_supported(quote, page.text, metadata)
                and day in guard._dates_in(quote)
                and (not known or day in known)
            )
            checked["original_publication_date_verified"] = bool(supported)
            source_record["original_publication_date_verified"] = bool(supported)
        except Exception as exc:
            source_record["private_error"] = {
                "type": type(exc).__name__, "message": str(exc)[:1800],
            }
        checked["source_receipt"] = _write_private(
            work / ("source-{:03d}.json".format(index)), source_record)
        return checked

    def _review_arm(self, arm, root):
        work = root / arm
        work.mkdir(mode=0o700)
        callback_work = work / "callback"
        callback_work.mkdir(mode=0o700)
        material = {
            **_clone(self._review_material),
            "instruction": boundary.BOUNDARY_SYSTEM,
            "review_arm": arm,
        }
        request_receipt = _write_private(work / "request.json", material)
        callback_error = None
        try:
            response = self.boundary_reviewer(
                _clone(material), _clone(boundary.REVIEW_SCHEMA), callback_work)
            # Reject unserializable/non-JSON callback output before retaining it.
            response = _clone(response)
            if not isinstance(response, dict):
                raise ValueError("boundary_callback_must_return_an_object")
        except Exception as exc:
            callback_error = {"type": type(exc).__name__, "message": str(exc)[:1800]}
            response = {"callback_error": callback_error}
        response_receipt = _write_private(work / "response.json", response)
        trace = callback_work / "tool-trace.json"
        if trace.is_file() and not trace.is_symlink():
            trace.chmod(0o600)
            trace_receipt = {"path": str(trace.resolve()), "sha256": boundary.file_sha256(trace)}
        else:
            trace_receipt = _write_private(work / "tool-trace.json", {
                "source": "callback supplied/no external trace",
                "external_tool_trace_supplied": False,
                "review_arm": arm,
                "request_sha256": request_receipt["sha256"],
                "response_sha256": response_receipt["sha256"],
            })
        review = _clone(response) if callback_error is None else self._unresolved()
        evidence = review.get("evidence")
        if not isinstance(evidence, list) or len(evidence) > 12:
            review = self._unresolved()
            evidence = []
        review["evidence"] = [self._verify_evidence(item, index, work)
                              for index, item in enumerate(evidence, 1)]
        # This receipt describes the public-only host invocation, without
        # inventing a model identity when the callback does not supply one.
        model = getattr(self.boundary_reviewer, "model", None) or "callback"
        review["review_receipt"] = {
            "labels_read": False, "prior_predictions_or_scores_read": False,
            "reviewer_model": str(model), "reviewer_protocol": boundary.PROTOCOL,
            "review_arm": arm,
            "request": request_receipt, "response": response_receipt,
            "tool_trace": trace_receipt,
        }
        _write_private(work / "result.json", review)
        return review

    def verify_boundary(self):
        """Run three private reviews once and return their safe common boundary.

        Any failed/unresolved review closes news. The settlement day is never
        substituted for missing first-publication evidence.
        """
        with self._lock:
            if self._boundary is not None:
                return _clone(self._boundary)
            root = self.private_dir / ("boundary-" + self._session)
            root.mkdir(mode=0o700)
            reviews = {}
            with ThreadPoolExecutor(max_workers=len(boundary.ARMS)) as executor:
                futures = {arm: executor.submit(self._review_arm, arm, root)
                           for arm in boundary.ARMS}
                for arm, future in futures.items():
                    try:
                        reviews[arm] = future.result()
                    except Exception as exc:
                        # Fail closed even when a callback or evidence artifact
                        # cannot be retained. Error details stay host-private.
                        reviews[arm] = self._unresolved()
                        reviews[arm]["private_error"] = {
                            "type": type(exc).__name__, "message": str(exc)[:1800],
                        }
            common = boundary.derive_boundary(self._task, reviews)
            _write_private(root / "reviews.json", reviews)
            _write_private(root / "boundary.json", common)
            self._boundary_reviews = reviews
            self._guard = guard.Guard(
                {**self._task, "_news_allowed": common["news_allowed"]},
                common["excluded_from_date"],
                self.private_dir / ("news-" + self._session),
                self.discover, self.reviewer, fetcher=self.fetcher)
            self._boundary = common
            return _clone(common)

    @property
    def policy(self):
        """The answer-free policy intended for the prediction model."""
        return boundary.predictor_policy(self.verify_boundary())

    def search(self, query):
        self.verify_boundary()
        return self._guard.search(query)

    def open(self, url):
        self.verify_boundary()
        return self._guard.open(url)
