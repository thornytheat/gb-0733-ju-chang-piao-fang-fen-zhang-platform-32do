import hashlib
import json
from datetime import UTC, datetime

from app.config import ALL_SOURCES, RULE_VERSION, SOURCE_COLUMNS
from app.errors import Problem


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def input_manifest(sources):
    if not set(SOURCE_COLUMNS) <= set(sources) or not set(sources) <= set(ALL_SOURCES):
        raise Problem(422, 'SOURCE_SET', {'expected': list(SOURCE_COLUMNS)})
    return [{'source': name, 'sha256': checksum(sources[name]), 'bytes': len(sources[name])}
            for name in ALL_SOURCES if name in sources]


def run_identifier(book, inputs):
    return 'run_' + checksum(canonical([book, RULE_VERSION, inputs]))[:24]


def publication_digest(tickets, shows, rights):
    def project(row, fields):
        return [row[field] for field in fields]

    material = {
        'tickets': [project(tickets[key], ('ticket', 'paid_at', 'paid_cents')) for key in sorted(tickets)],
        'shows': [project(shows[key], ('show', 'title', 'venue', 'publication_status', 'opens', 'closes'))
                  for key in sorted(shows)],
        'rights': [project(row, ('ticket', 'show', 'weight')) for row in rights],
    }
    return checksum(canonical(material))


def utc_now():
    return datetime.now(UTC).isoformat(timespec='seconds')
