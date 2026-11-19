"""Source evidence shared by offline storage fixtures."""
from pathlib import Path
from newsdesk.publication_time import AUDIT_FIELDS, audit_publication


def verified_metadata():
    raw = str(Path(__file__).parent / 'fixtures/publication-time.html')
    result = audit_publication({'source_id':'fixture', 'raw_path':raw})
    return {'raw_path':raw, 'publication_precision':result['publication_precision'],
            **{field:result[field] for field in AUDIT_FIELDS}}
