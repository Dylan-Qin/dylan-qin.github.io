"""Exercise publishing decisions without making requests to Google Scholar."""

import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

from google_scholar_crawler import main as crawler


SCHOLAR_ID = '0KTz65wAAAAJ'


class CitationRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.results = self.directory / 'results'
        self.output = self.directory / 'github-output'
        self.summary = self.directory / 'github-summary'
        environment = mock.patch.dict(os.environ, {
            'GOOGLE_SCHOLAR_ID': SCHOLAR_ID,
            'GITHUB_OUTPUT': str(self.output),
            'GITHUB_STEP_SUMMARY': str(self.summary),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.log = io.StringIO()
        redirect = contextlib.redirect_stdout(self.log)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def author(self, **overrides):
        author = {
            'scholar_id': SCHOLAR_ID,
            'name': 'Chuan Qin (秦川)',
            'citedby': 5711,
            'updated': datetime.now(timezone.utc).isoformat(),
            'publications': {'author:paper': {
                'author_pub_id': 'author:paper',
                'num_citations': 42,
            }},
        }
        author.update(overrides)
        return author

    def cache(self, author=None, badge=None):
        author = self.author() if author is None else author
        badge = {
            'schemaVersion': 1,
            'label': 'citations',
            'message': str(author['citedby']),
        } if badge is None else badge
        self.results.mkdir()
        for filename, value in [
            ('gs_data.json', author), ('gs_data_shieldsio.json', badge),
        ]:
            (self.results / filename).write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8',
            )
        return self.snapshot()

    def snapshot(self):
        return {
            path.name: path.read_bytes()
            for path in self.results.iterdir()
        }

    def unavailable(self):
        return subprocess.CompletedProcess(['fetch'], crawler.SOURCE_UNAVAILABLE,
                                           stdout='', stderr='Scholar unavailable')

    def test_success_creates_author_and_matching_badge_and_reports_update(self):
        author = self.author(citedby=5800)
        fetched = subprocess.CompletedProcess(['fetch'], 0,
                                             stdout=json.dumps(author), stderr='')
        with mock.patch.object(crawler.subprocess, 'run', return_value=fetched):
            crawler.update_results(self.results, SCHOLAR_ID)

        self.assertEqual(json.loads((self.results / 'gs_data.json').read_text()), author)
        self.assertEqual(json.loads((self.results / 'gs_data_shieldsio.json').read_text()), {
            'schemaVersion': 1, 'label': 'citations', 'message': '5800',
        })
        self.assertEqual(self.output.read_text(), 'updated=true\n')
        self.assertIn('Fetched 5800 citations', self.summary.read_text())

    def test_source_unavailable_keeps_cache_byte_for_byte(self):
        before = self.cache()
        with mock.patch.object(crawler.subprocess, 'run', return_value=self.unavailable()):
            crawler.update_results(self.results, SCHOLAR_ID)

        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.output.read_text(), 'updated=false\n')
        self.assertIn('No new data was published', self.summary.read_text())
        self.assertIn('::warning::', self.log.getvalue())

    def test_fetch_timeout_keeps_cache_byte_for_byte(self):
        before = self.cache()
        with mock.patch.object(crawler.subprocess, 'run',
                               side_effect=subprocess.TimeoutExpired(['fetch'], 180)):
            crawler.update_results(self.results, SCHOLAR_ID)

        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.output.read_text(), 'updated=false\n')
        self.assertIn('deadline reached', self.summary.read_text())

    def test_stale_cache_fails_without_changing_existing_data(self):
        timestamp = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        before = self.cache(author=self.author(updated=timestamp))
        with mock.patch.object(crawler.subprocess, 'run', return_value=self.unavailable()):
            with self.assertRaisesRegex(RuntimeError, 'over 7 days old'):
                crawler.update_results(self.results, SCHOLAR_ID)

        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.output.read_text(), 'updated=false\n')

    def test_missing_cache_does_not_turn_unavailable_source_into_success(self):
        with mock.patch.object(crawler.subprocess, 'run', return_value=self.unavailable()):
            with self.assertRaises(FileNotFoundError):
                crawler.update_results(self.results, SCHOLAR_ID)
        self.assertFalse(self.results.exists())
        self.assertFalse(self.output.exists())

    def test_invalid_cached_author_fails_without_rewriting_files(self):
        before = self.cache(author=self.author(scholar_id='another-author'))
        with mock.patch.object(crawler.subprocess, 'run', return_value=self.unavailable()):
            with self.assertRaisesRegex(ValueError, 'different Google Scholar author'):
                crawler.update_results(self.results, SCHOLAR_ID)
        self.assertEqual(self.snapshot(), before)

    def test_inconsistent_cached_badge_fails_without_rewriting_files(self):
        before = self.cache(badge={
            'schemaVersion': 1, 'label': 'citations', 'message': '9999',
        })
        with mock.patch.object(crawler.subprocess, 'run', return_value=self.unavailable()):
            with self.assertRaisesRegex(ValueError, 'badge does not match'):
                crawler.update_results(self.results, SCHOLAR_ID)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_fetched_data_cannot_replace_a_good_snapshot(self):
        before = self.cache()
        for overrides in [
            {'scholar_id': 'another-author'},
            {'citedby': -1},
            {'citedby': True},
            {'publications': []},
            {'updated': 'not-a-timestamp'},
        ]:
            with self.subTest(overrides=overrides):
                fetched = subprocess.CompletedProcess(
                    ['fetch'], 0, stdout=json.dumps(self.author(**overrides)), stderr='',
                )
                with mock.patch.object(crawler.subprocess, 'run', return_value=fetched):
                    with self.assertRaises(ValueError):
                        crawler.update_results(self.results, SCHOLAR_ID)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(self.output.exists())

    def test_programming_error_in_child_is_not_hidden_by_valid_cache(self):
        before = self.cache()
        fetched = subprocess.CompletedProcess(['fetch'], 1, stdout='',
                                             stderr='TypeError: unexpected parser result')
        with mock.patch.object(crawler.subprocess, 'run', return_value=fetched):
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(subprocess.CalledProcessError):
                    crawler.update_results(self.results, SCHOLAR_ID)
        self.assertIn('TypeError', errors.getvalue())
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.output.exists())

    def test_legacy_naive_utc_cache_timestamp_is_accepted(self):
        timestamp = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(' ')
        before = self.cache(author=self.author(updated=timestamp))
        with mock.patch.object(crawler.subprocess, 'run', return_value=self.unavailable()):
            crawler.update_results(self.results, SCHOLAR_ID)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.output.read_text(), 'updated=false\n')

    def test_missing_author_secret_fails_before_starting_a_worker(self):
        with mock.patch.dict(os.environ, {'GOOGLE_SCHOLAR_ID': '  '}):
            with mock.patch.object(crawler.subprocess, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'GOOGLE_SCHOLAR_ID'):
                    crawler.main()
        run.assert_not_called()


class ScholarWorkerTests(unittest.TestCase):
    def scholarly_modules(self, author=None, error=None):
        scholarly_api = SimpleNamespace(
            set_timeout=mock.Mock(),
            set_retries=mock.Mock(),
            search_author_id=mock.Mock(return_value=author, side_effect=error),
            fill=mock.Mock(),
        )
        package = ModuleType('scholarly')
        package.scholarly = scholarly_api
        proxy = ModuleType('scholarly._proxy_generator')
        proxy.MaxTriesExceededException = SourceUnavailable
        return {'scholarly': package, 'scholarly._proxy_generator': proxy}, scholarly_api

    def test_worker_preserves_publications_as_mapping_and_adds_timestamp(self):
        author = {
            'scholar_id': SCHOLAR_ID, 'citedby': 5711,
            'publications': [
                {'author_pub_id': 'author:one', 'num_citations': 30},
                {'author_pub_id': 'author:two', 'num_citations': 12},
            ],
        }
        modules, api = self.scholarly_modules(author=author)
        with mock.patch.dict(sys.modules, modules):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(crawler.fetch_author(SCHOLAR_ID), 0)
        fetched = json.loads(output.getvalue())
        self.assertEqual(set(fetched['publications']), {'author:one', 'author:two'})
        self.assertEqual(fetched['publications']['author:two']['num_citations'], 12)
        self.assertIsNotNone(datetime.fromisoformat(fetched['updated']).tzinfo)
        api.search_author_id.assert_called_once_with(SCHOLAR_ID)

    def test_worker_marks_scholar_access_rejection_as_source_unavailable(self):
        modules, _ = self.scholarly_modules(error=SourceUnavailable('blocked'))
        with mock.patch.dict(sys.modules, modules):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(crawler.fetch_author(SCHOLAR_ID), crawler.SOURCE_UNAVAILABLE)


class SourceUnavailable(Exception):
    pass


if __name__ == '__main__':
    unittest.main()
