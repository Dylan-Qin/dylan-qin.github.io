"""Refresh citation data without replacing a valid snapshot when Scholar is unavailable."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timedelta, timezone


FETCH_TIMEOUT_SECONDS = 180
MAX_CACHE_AGE = timedelta(days=7)
SOURCE_UNAVAILABLE = 75


def fetch_author(scholar_id):
    # Both package imports in tests and direct CLI execution are supported.
    if __package__:
        from . import profile_api
    else:
        import profile_api
    try:
        author = profile_api.fetch_author(scholar_id, os.environ['SERPER_API_KEY'].strip())
    except profile_api.SourceUnavailable as error:
        print(str(error), file=sys.stderr)
        return SOURCE_UNAVAILABLE
    except profile_api.ConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(author, ensure_ascii=False))
    return 0


def load_local_environment():
    """Read only the two supported values from the ignored local .env file."""
    configuration = Path(__file__).resolve().parent / '.env'
    if not configuration.exists():
        return
    for line in configuration.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, separator, value = line.partition('=')
        name = name.strip()
        if not separator or name not in ('GOOGLE_SCHOLAR_ID', 'SERPER_API_KEY'):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if not os.environ.get(name, '').strip():
            os.environ[name] = value


def validate_author(author, scholar_id):
    if author.get('scholar_id') != scholar_id:
        raise ValueError('Citation data belongs to a different Google Scholar author.')
    if type(author.get('citedby')) is not int or author['citedby'] < 0:
        raise ValueError('Citation data must contain a non-negative integer citedby.')
    if not isinstance(author.get('publications'), dict):
        raise ValueError('Citation data must contain a publication mapping.')
    updated = datetime.fromisoformat(author['updated'])
    # Older snapshots used naive UTC timestamps from GitHub's Linux runner.
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return updated


def report(updated, message):
    print(message)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
            output.write(f'updated={str(updated).lower()}\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
            summary.write(f'{message}\n')


def keep_cached_results(results, scholar_id, reason):
    # A failed refresh must neither create a zero badge nor claim a new timestamp.
    author = json.loads((results / 'gs_data.json').read_text(encoding='utf-8'))
    updated = validate_author(author, scholar_id)
    badge = json.loads((results / 'gs_data_shieldsio.json').read_text(encoding='utf-8'))
    if (badge.get('schemaVersion') != 1 or badge.get('label') != 'citations'
            or badge.get('message') != str(author['citedby'])):
        raise ValueError('Cached citation badge does not match the author data.')
    message = (
        f'Google Scholar refresh unavailable ({reason}). No new data was published. '
        f'Keeping {author["citedby"]} citations last fetched at {author["updated"]}.'
    )
    report(False, message)
    if datetime.now(timezone.utc) - updated > MAX_CACHE_AGE:
        raise RuntimeError(f'{message} The snapshot is over 7 days old; investigate Scholar access.')
    print(f'::warning::{message}')


def update_results(results, scholar_id):
    try:
        fetched = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), '--fetch'],
            capture_output=True, text=True, encoding='utf-8',
            timeout=FETCH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        keep_cached_results(results, scholar_id, '180-second fetch deadline reached')
        return
    if fetched.returncode == SOURCE_UNAVAILABLE:
        reason = fetched.stderr.strip().replace('\n', ' ') or 'profile request failed'
        keep_cached_results(results, scholar_id, reason)
        return
    # Dependency, parsing, and programming errors should still fail the workflow.
    if fetched.returncode != 0:
        print(fetched.stderr, file=sys.stderr)
        fetched.check_returncode()
    author = json.loads(fetched.stdout)
    validate_author(author, scholar_id)
    # Keep any historical per-paper data, with its original timestamp. The new
    # request refreshes only profile totals and does not refresh this mapping.
    if author.get('data_source') == 'serper':
        previous = {}
        try:
            previous = json.loads((results / 'gs_data.json').read_text(encoding='utf-8'))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        if (isinstance(previous, dict) and previous.get('scholar_id') == scholar_id
                and isinstance(previous.get('publications'), dict)):
            author['publications'] = previous['publications']
            author['publications_updated'] = previous.get('publications_updated', previous.get('updated'))
    badge = {
        'schemaVersion': 1,
        'label': 'citations',
        'message': str(author['citedby']),
    }
    results.mkdir(parents=True, exist_ok=True)
    for filename, data in [('gs_data.json', author), ('gs_data_shieldsio.json', badge)]:
        destination = results / filename
        temporary = destination.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        temporary.replace(destination)
    report(True, f'Fetched {author["citedby"]} profile citations via Serper at {author["updated"]}.')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Refresh the personal-profile citation badge via Serper.')
    parser.add_argument('--fetch', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parent / 'results')
    arguments = parser.parse_args(argv)
    load_local_environment()
    scholar_id = os.environ.get('GOOGLE_SCHOLAR_ID', '').strip()
    if not scholar_id:
        raise ValueError('Set the GOOGLE_SCHOLAR_ID repository secret before running the crawler.')
    if not os.environ.get('SERPER_API_KEY', '').strip():
        raise ValueError('Set SERPER_API_KEY in the local .env file or GitHub Actions secrets.')
    if arguments.fetch:
        return fetch_author(scholar_id)
    update_results(arguments.output_dir, scholar_id)
    return 0


if __name__ == '__main__':
    sys.exit(main())
