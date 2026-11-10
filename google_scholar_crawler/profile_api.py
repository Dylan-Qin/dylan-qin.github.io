"""Read a Scholar profile's statistics with one Serper Scrape request."""

from datetime import datetime, timezone
import json
import re
from socket import timeout as SocketTimeout
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


class SourceUnavailable(Exception):
    """A temporary API failure for which cached statistics may be used."""


class ConfigurationError(Exception):
    """A credential or request problem requiring configuration changes."""


def clean_cell(value):
    value = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', value)
    return value.replace('*', '').replace('`', '').strip()


def number(value):
    # Explicit cells only: never infer a total from article counts or snippets.
    value = clean_cell(value)
    if not re.fullmatch(r'[0-9]+(?:[,\u00a0\u202f][0-9]{3})*', value):
        raise ValueError('The profile statistics contain an invalid number.')
    return int(re.sub(r'[,\u00a0\u202f]', '', value))


def extract_statistics(content):
    content = content.translate(str.maketrans({'\u2010': '-', '\u2011': '-', '\u2013': '-'}))
    fields = {'citations': 'citedby', 'h-index': 'hindex', 'i10-index': 'i10index'}
    statistics = {}

    def record(label, total, recent):
        field = fields[label.lower()]
        values = (number(total), number(recent))
        if field in statistics and values != (statistics[field], statistics[field + '5y']):
            raise ValueError('The profile contains conflicting statistics tables.')
        statistics[field], statistics[field + '5y'] = values

    columns = None
    for line in content.splitlines():
        if '|' not in line:
            columns = None
            continue
        cells = [clean_cell(cell) for cell in line.strip().strip('|').split('|')]
        lower = [cell.lower() for cell in cells]
        if 'all' in lower and any(re.fullmatch(r'since\s*\d{4}', cell) for cell in lower):
            if len(cells) == 2:
                cells.insert(0, '')
                lower.insert(0, '')
            columns = (lower.index('all'), next(
                index for index, cell in enumerate(lower) if re.fullmatch(r'since\s*\d{4}', cell)
            ))
            continue
        if columns and cells and cells[0].lower() in fields:
            record(cells[0], cells[columns[0]], cells[columns[1]])

    if 'citedby' not in statistics:
        # Serper also returns plain text. Require the profile table header, then
        # two distinct numeric cells on each labelled statistics row.
        header = re.search(r'\bAll\s+Since\s*\d{4}\b', content, re.IGNORECASE)
        if header:
            table = content[header.end():]
            integer = r'[0-9]+(?:[,\u00a0\u202f][0-9]{3})*'
            row = re.compile(
                rf'^\s*(Citations|h-index|i10-index)\s*:?\s+({integer})\s+({integer})\s*$',
                re.MULTILINE | re.IGNORECASE,
            )
            for match in row.finditer(table):
                record(*match.groups())
    if 'citedby' not in statistics:
        raise ValueError('The fetched page does not contain a readable profile statistics table.')
    return statistics


def parse_profile(data, scholar_id):
    metadata = data.get('metadata') or {}
    for field in ('url', 'canonical'):
        actual_url = metadata.get(field)
        if isinstance(actual_url, str):
            users = parse_qs(urlparse(actual_url).query).get('user')
            if users and users != [scholar_id]:
                raise ValueError('Serper returned a different Google Scholar author.')
    statistics = None
    for field in ('markdown', 'text'):
        content = data.get(field)
        if not isinstance(content, str) or not content.strip():
            continue
        try:
            statistics = extract_statistics(content)
            break
        except (ValueError, IndexError):
            continue
    if statistics is None:
        raise SourceUnavailable('Serper did not return a readable Scholar statistics table; the page may be blocked.')
    title = metadata.get('title') or ''
    title = re.sub(r'[\u200e\u200f\u202a-\u202e\u2066-\u2069]', '', title)
    name = re.sub(r'\s*[-\u2013|]\s*Google Scholar.*$', '', title, flags=re.IGNORECASE).strip()
    return {
        'scholar_id': scholar_id,
        'name': name or scholar_id,
        **statistics,
        'updated': datetime.now(timezone.utc).isoformat(),
        'data_source': 'serper',
        'publications': {},
    }


def fetch_author(scholar_id, api_key):
    profile_url = 'https://scholar.google.com/citations?' + urlencode({'user': scholar_id, 'hl': 'en'})
    request = Request(
        'https://scrape.serper.dev',
        data=json.dumps({'url': profile_url, 'includeMarkdown': True}).encode('utf-8'),
        headers={'X-API-KEY': api_key, 'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urlopen(request, timeout=60) as response:
            data = json.load(response)
    except HTTPError as error:
        # Response bodies and exception messages can contain credential details.
        if error.code in (401, 403):
            raise ConfigurationError('Serper rejected access. Check SERPER_API_KEY and Scrape permissions.') from None
        if error.code in (402, 429):
            raise SourceUnavailable('Serper credit or rate limit reached; check your dashboard.') from None
        if error.code >= 500:
            raise SourceUnavailable(f'Serper returned HTTP {error.code}.') from None
        raise ConfigurationError(f'Serper rejected the request (HTTP {error.code}).') from None
    except (URLError, TimeoutError, SocketTimeout):
        raise SourceUnavailable('Serper connection failed or exceeded the 60-second deadline.') from None
    except json.JSONDecodeError:
        raise SourceUnavailable('Serper returned invalid JSON.') from None
    if not isinstance(data, dict) or data.get('error') or data.get('statusCode', 200) != 200:
        raise SourceUnavailable('Serper did not return a successful scrape; check your dashboard.')
    return parse_profile(data, scholar_id)
