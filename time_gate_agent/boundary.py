"""Answer-free daily boundaries: three verified reviews or news stays closed.

No model calls, network access or label reads occur in this module.  A separate
boundary reviewer supplies quarantined public-source reviews; the runner shares
one resulting day per question across all three methods.  This boundary review
is separate from each method's own pre-exposure news checker.
"""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROTOCOL = 'experiment05-three-method-first-publication-fail-closed-v1'
PUBLIC_TASK_FIELDS = {
    'answer_type', 'benchmark', 'benchmark_uid', 'case_id', 'cutoff',
    'cutoff_basis', 'dataset_revision', 'dedup_key', 'options', 'question',
    'resolution_rule', 'source', 'target_date', 'title', 'topic',
}
BOUNDARY_SYSTEM = (
    'You are an isolated publication-date reviewer, not a predictor or scorer. '
    'Using only the supplied public question and public original sources, '
    'establish the earliest actual calendar date when an official release or '
    'contemporaneous news already determined the target result. Inspect early '
    'announcements, weekend/holiday prior-close rules and data publication lags. '
    'A settlement date, observation date, scheduled date or benchmark forecast '
    'cutoff alone is not proof of actual publication. Dates are YYYY-MM-DD; '
    'hours, minutes and seconds are not required. Prefer the official original '
    'actual result release; a forecast/preview or scheduled release is not an actual result. Verify the original visible '
    'publication date or a reliable old archive and search for earlier result '
    'announcements. If the actual publication day cannot be verified, return '
    'unresolved and null; the host disables all news for this question without '
    'counting a model failure. It NEVER uses settlement-day fallback to allow news. Never read labels, previous predictions, scores '
    'or other cases. Output date evidence only: no actual answer value, winning '
    'option, yes/no outcome, increase/decrease direction or forecast. Evidence '
    'quotes must state publication dates only. Return the strict JSON schema. '
    'The private review and its sources never enter prediction-model contexts.'
)
REVIEW_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'status': {'type': 'string', 'enum': ['verified', 'unresolved']},
        'case_id': {'type': 'string', 'minLength': 1},
        'public_task_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'answer_first_publication_date': {
            'type': ['string', 'null'], 'pattern': '^\\d{4}-\\d{2}-\\d{2}$'},
        'actual_publication_verified': {'type': 'boolean'},
        'earlier_result_search_completed': {'type': 'boolean'},
        'review_reason': {'type': 'string', 'enum': [
            'verified_actual_publication_day', 'unresolved_publication_day',
            'unresolved_ambiguous_source', 'unresolved_no_matching_publication']},
        'evidence': {
            'type': 'array', 'maxItems': 12,
            'items': {
                'type': 'object', 'additionalProperties': False,
                'properties': {
                    'source_kind': {'type': 'string', 'enum': [
                        'official_original_release', 'contemporaneous_original_report']},
                    'url': {'type': 'string', 'minLength': 1},
                    'determines_target_result': {'type': 'boolean'},
                    'publication_basis': {'type': 'string', 'enum': [
                        'original_visible_publication_date', 'verified_original_archive']},
                    'date_evidence_quote': {'type': 'string', 'minLength': 1, 'maxLength': 240},
                    'answer_publication_date': {'type': 'string', 'pattern': '^\\d{4}-\\d{2}-\\d{2}$'},
                },
                'required': ['source_kind', 'url', 'determines_target_result',
                             'publication_basis', 'date_evidence_quote',
                             'answer_publication_date'],
            },
        },
    },
    'required': ['status', 'case_id', 'public_task_sha256',
                 'answer_first_publication_date', 'actual_publication_verified',
                 'earlier_result_search_completed', 'review_reason', 'evidence'],
}


class BoundaryReviewError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def calendar_day(value: str) -> date:
    if not isinstance(value, str) or len(value) != 10:
        raise BoundaryReviewError('invalid_calendar_day')
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BoundaryReviewError('invalid_calendar_day') from exc


def settlement_day(task: dict) -> date:
    try:
        return calendar_day(task['target_date'][:10])
    except (KeyError, TypeError) as exc:
        raise BoundaryReviewError('invalid_public_target_date') from exc


def build_review_material(task: dict) -> dict:
    if set(task) - PUBLIC_TASK_FIELDS:
        raise BoundaryReviewError('private_or_unrecognized_public_task_fields')
    settlement_day(task)
    return {
        'case_id': task['case_id'], 'public_task_sha256': digest(task),
        'public_task': task,
        'boundary_rule': 'Verify the actual first result-publication day. All three independent method reviews must pass before any news is allowed. Exclude the whole earlier day of settlement or the earliest verified result publication. Unresolved review disables news; never substitute settlement day.',
        'date_precision': 'calendar_day_only',
        'host_fallback': 'None. Unverified publication date blocks news, not prediction and not a model failure.',
    }


def retained_receipt(value: dict) -> bool:
    try:
        if not isinstance(value, dict) or set(value) != {'path', 'sha256'}:
            return False
        path = Path(value['path'])
        return path.is_absolute() and path.is_file() and file_sha256(path) == value['sha256']
    except (TypeError, ValueError, OSError):
        return False


def verified_publication_day(task: dict, review: dict | None) -> tuple[date | None, str]:
    """Safe reason enums never copy source/result text into predictor policy."""
    if not isinstance(review, dict) or review.get('status') != 'verified':
        return None, 'answer_publication_day_unverified'
    if review.get('case_id') != task['case_id'] or review.get('public_task_sha256') != digest(task):
        return None, 'review_task_binding_unverified'
    if review.get('actual_publication_verified') is not True or review.get('earlier_result_search_completed') is not True:
        return None, 'actual_publication_checks_incomplete'
    try:
        declared = calendar_day(review.get('answer_first_publication_date'))
    except BoundaryReviewError:
        return None, 'answer_publication_day_unverified'
    evidence = review.get('evidence')
    if not isinstance(evidence, list) or not evidence:
        return None, 'source_proof_unverified'
    days = []
    for item in evidence:
        if not isinstance(item, dict):
            return None, 'source_proof_unverified'
        if item.get('source_kind') not in ('official_original_release', 'contemporaneous_original_report'):
            return None, 'source_proof_unverified'
        if item.get('publication_basis') not in ('original_visible_publication_date', 'verified_original_archive'):
            return None, 'original_publication_day_unverified'
        try:
            url = urlsplit(item.get('url', ''))
            observed_day = calendar_day(item.get('answer_publication_date'))
        except (TypeError, ValueError):
            return None, 'source_proof_unverified'
        if url.scheme not in ('https', 'http') or not url.netloc or item.get('determines_target_result') is not True:
            return None, 'source_proof_unverified'
        # The host fetches the source independently and sets this flag only
        # after checking the original page date against the reviewer claim.
        # This flag and source_receipt are added outside the model's schema.
        if item.get('original_publication_date_verified') is not True:
            return None, 'original_publication_day_unverified'
        if not isinstance(item.get('date_evidence_quote'), str) or not item['date_evidence_quote'].strip():
            return None, 'original_publication_day_unverified'
        if not retained_receipt(item.get('source_receipt')):
            return None, 'source_receipt_unverified'
        days.append(observed_day)
    if declared != min(days):
        return None, 'earliest_publication_day_unverified'
    receipt = review.get('review_receipt')
    if not isinstance(receipt, dict) or receipt.get('labels_read') is not False or receipt.get('prior_predictions_or_scores_read') is not False:
        return None, 'isolated_review_receipt_unverified'
    if not receipt.get('reviewer_model') or not receipt.get('reviewer_protocol'):
        return None, 'isolated_review_receipt_unverified'
    if not all(retained_receipt(receipt.get(label)) for label in ('request', 'response', 'tool_trace')):
        return None, 'isolated_review_receipt_unverified'
    return declared, ''


ARMS = ('strategy', 'original_search', 'llm_base')


def derive_boundary(task: dict, reviews: dict) -> dict:
    """Every method independently reviews the date; the common boundary fails closed."""
    settlement = settlement_day(task)
    checks = {arm: verified_publication_day(task, reviews.get(arm)) for arm in ARMS}
    allowed = all(day is not None for day, reason in checks.values())
    publication = min(day for day, reason in checks.values()) if allowed else None
    # This day only makes requests well-formed when news is blocked; the Guard
    # returns empty before discovery/fetch and never uses this as permission.
    blocked_day = date.fromisoformat(task['cutoff'][:10])
    excluded = min(settlement, publication) if allowed else blocked_day
    return {
        'protocol': PROTOCOL, 'case_id': task['case_id'],
        'public_task_sha256': digest(task), 'target_date_unchanged': task['target_date'],
        'status': 'verified_publication' if allowed else 'news_blocked_unverified_publication',
        'basis': 'three_method_publication_review' if allowed else 'no_news_until_publication_verified',
        'news_allowed': allowed, 'excluded_from_date': excluded.isoformat(),
        'cutoff_exclusive': excluded.isoformat() + 'T00:00:00+00:00',
        'last_allowed_calendar_date': (excluded - timedelta(days=1)).isoformat() if allowed else None,
        'date_precision': 'calendar_day_only', 'counts_as_model_failure': False,
        'method_review_statuses': {arm: ('verified' if day is not None else reason) for arm, (day, reason) in checks.items()},
        'boundary_reviews_sha256': digest(reviews),
    }


def predictor_policy(boundary: dict) -> dict:
    return {'news_allowed': boundary['news_allowed'], 'excluded_from_date': boundary['excluded_from_date'],
            'cutoff_exclusive': boundary['cutoff_exclusive'],
            'last_allowed_calendar_date': boundary['last_allowed_calendar_date'],
            'rule': 'No verified common first-publication boundary means no external news; still choose exactly one option. With a verified boundary, use only independently approved pre-boundary articles containing no target result. Unavailable news is not an outcome signal.'}
