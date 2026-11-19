"""Combine independent public history channels without hiding their gaps."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from newsdesk.ingest import write_json


def merge_channel_reports(source_id, start, end, output_dir, channels):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    candidates, errors = {}, []
    for name, report in channels.items():
        for error in report.get('errors', []):
            errors.append({'channel': name, 'detail': error})
        path = report.get('candidates_path')
        if not path or not Path(path).exists():
            continue
        for number, line in enumerate(Path(path).read_text(encoding='utf-8').split('\n'), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or row.get('source_id') != source_id:
                    raise ValueError('候选来源与历史渠道不一致')
                key = row.get('candidate_id') or row.get('source_url')
                if not isinstance(key, str):
                    key = hashlib.sha256(line.encode()).hexdigest()
                old = candidates.get(key)
                if old is None or len(str(row.get('body') or '')) > len(str(old.get('body') or '')):
                    candidates[key] = row
            except (ValueError, TypeError) as error:
                errors.append({'channel': name, 'candidate_file': str(path), 'line': number, 'error': str(error)})
    destination = output / 'combined-candidates.jsonl'
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=output, delete=False) as handle:
        for row in candidates.values():
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
        temporary = handle.name
    os.replace(temporary, destination)
    complete = bool(channels) and not errors and all(
        report.get('status') == 'complete' or report.get('coverage', {}).get('complete') is True
        for report in channels.values())
    terminal = bool(channels) and all(
        report.get('terminal') is True or report.get('coverage', {}).get('terminal') is True
        or report.get('status') in {'complete', 'blocked', 'failed'}
        or report.get('coverage', {}).get('complete') is True for report in channels.values())
    blocked = terminal and not candidates and all(report.get('status') in {'blocked', 'failed'} for report in channels.values())
    report = {'source_id': source_id, 'status': 'complete' if complete else 'blocked' if blocked else 'incomplete',
              'terminal': terminal, 'candidate_count': len(candidates), 'candidates_path': str(destination.resolve()),
              'channels': channels, 'errors': errors,
              'coverage': {'requested_start': start, 'requested_end': end, 'complete': complete,
                           'scope': '分别回溯已声明的公开渠道，任何渠道未完整均保留缺口',
                           'gap_dates': sorted({day for value in channels.values()
                               for day in (list(value.get('coverage', {}).get('gap_dates', []))
                                   + [day for day, item in value.get('days', {}).items() if item.get('status') != 'complete'])})}}
    # Channel-specific frontiers remain in their reports. The primary calendar
    # frontier is also retained for the compact per-source overview.
    for key in ('attempted_days', 'completed_days', 'frontier_date', 'processed_dates'):
        values = [value[key] for value in channels.values() if key in value]
        if values:
            report[key] = values[0]
    remaining = [value.get('retry_attempts_remaining') for value in channels.values()
                 if value.get('retry_pending') is True and type(value.get('retry_attempts_remaining')) is int
                 and 0 < value['retry_attempts_remaining'] <= 2]
    report['retry_pending'] = bool(remaining)
    report['retry_attempts_remaining'] = max(remaining, default=0)
    write_json(output / 'combined-report.json', report)
    return report
