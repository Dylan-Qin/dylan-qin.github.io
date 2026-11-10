"""Exercise publishing decisions without making requests to Google Scholar."""

import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from google_scholar_crawler import main as crawler
from google_scholar_crawler import profile_api


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
        self.assertIn('5800 profile citations via Serper', self.summary.read_text())

    def test_profile_refresh_retains_historical_publications_and_original_timestamp(self):
        previous = self.author(updated='2026-11-08 14:56:22.945315')
        self.cache(author=previous)
        author = self.author(citedby=5800, publications={}, data_source='serper')
        fetched = subprocess.CompletedProcess(['fetch'], 0, stdout=json.dumps(author), stderr='')
        with mock.patch.object(crawler.subprocess, 'run', return_value=fetched):
            crawler.update_results(self.results, SCHOLAR_ID)
        saved = json.loads((self.results / 'gs_data.json').read_text())
        self.assertEqual(saved['citedby'], 5800)
        self.assertEqual(saved['publications'], previous['publications'])
        self.assertEqual(saved['publications_updated'], previous['updated'])

    def test_repeated_profile_refresh_does_not_advance_publication_timestamp(self):
        previous = self.author(publications_updated='2025-12-01T00:00:00+00:00')
        self.cache(author=previous)
        author = self.author(citedby=5800, publications={}, data_source='serper')
        fetched = subprocess.CompletedProcess(['fetch'], 0, stdout=json.dumps(author), stderr='')
        with mock.patch.object(crawler.subprocess, 'run', return_value=fetched):
            crawler.update_results(self.results, SCHOLAR_ID)
        saved = json.loads((self.results / 'gs_data.json').read_text())
        self.assertEqual(saved['publications_updated'], previous['publications_updated'])

    def test_new_profile_initializes_empty_publication_mapping(self):
        author = self.author(publications={}, data_source='serper')
        fetched = subprocess.CompletedProcess(['fetch'], 0, stdout=json.dumps(author), stderr='')
        with mock.patch.object(crawler.subprocess, 'run', return_value=fetched):
            crawler.update_results(self.results, SCHOLAR_ID)
        saved = json.loads((self.results / 'gs_data.json').read_text())
        self.assertEqual(saved['publications'], {})
        self.assertNotIn('publications_updated', saved)

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
            with mock.patch.object(crawler, 'load_local_environment'):
                with mock.patch.object(crawler.subprocess, 'run') as run:
                    with self.assertRaisesRegex(ValueError, 'GOOGLE_SCHOLAR_ID'):
                        crawler.main([])
        run.assert_not_called()

    def test_missing_api_key_fails_before_any_network_or_worker(self):
        with mock.patch.dict(os.environ, {'SERPER_API_KEY': '  '}):
            with mock.patch.object(crawler, 'load_local_environment'):
                with mock.patch.object(crawler.subprocess, 'run') as run:
                    with mock.patch.object(profile_api, 'urlopen') as request:
                        with self.assertRaisesRegex(ValueError, 'SERPER_API_KEY'):
                            crawler.main([])
        run.assert_not_called()
        request.assert_not_called()

    def test_cli_output_directory_is_passed_to_coordinator(self):
        with mock.patch.dict(os.environ, {'SERPER_API_KEY': 'dummy-test-key'}):
            with mock.patch.object(crawler, 'load_local_environment'):
                with mock.patch.object(crawler, 'update_results') as update:
                    self.assertEqual(crawler.main(['--output-dir', str(self.results)]), 0)
        update.assert_called_once_with(self.results, SCHOLAR_ID)


class ProfileWorkerTests(unittest.TestCase):
    def test_worker_calls_profile_api_once_and_emits_only_normalized_profile(self):
        author = {'scholar_id': SCHOLAR_ID, 'citedby': 5711, 'publications': {}}
        with mock.patch.dict(os.environ, {'SERPER_API_KEY': 'dummy-test-key'}):
            with mock.patch.object(profile_api, 'fetch_author', return_value=author) as fetch:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(crawler.fetch_author(SCHOLAR_ID), 0)
        self.assertEqual(json.loads(output.getvalue()), author)
        fetch.assert_called_once_with(SCHOLAR_ID, 'dummy-test-key')

    def test_worker_marks_temporary_service_failure_for_cached_fallback(self):
        with mock.patch.dict(os.environ, {'SERPER_API_KEY': 'dummy-test-key'}):
            with mock.patch.object(profile_api, 'fetch_author',
                                   side_effect=profile_api.SourceUnavailable('Serper quota reached')):
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(crawler.fetch_author(SCHOLAR_ID), crawler.SOURCE_UNAVAILABLE)
        self.assertIn('quota', errors.getvalue())
        self.assertNotIn('dummy-test-key', errors.getvalue())

    def test_worker_authentication_failure_remains_fatal(self):
        with mock.patch.dict(os.environ, {'SERPER_API_KEY': 'dummy-test-key'}):
            with mock.patch.object(profile_api, 'fetch_author',
                                   side_effect=profile_api.ConfigurationError('Check SERPER_API_KEY')):
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(crawler.fetch_author(SCHOLAR_ID), 2)
        self.assertIn('SERPER_API_KEY', errors.getvalue())
        self.assertNotIn('dummy-test-key', errors.getvalue())


class LocalEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def load(self, content, environment):
        (self.directory / '.env').write_text(content, encoding='utf-8')
        with mock.patch.object(crawler, '__file__', str(self.directory / 'main.py')):
            with mock.patch.dict(os.environ, environment, clear=True):
                crawler.load_local_environment()
                return dict(os.environ)

    def test_reads_supported_keys_and_unquotes_values_without_executing_text(self):
        loaded = self.load(
            '# Local settings\nGOOGLE_SCHOLAR_ID = "0KTz65wAAAAJ"\n'
            "SERPER_API_KEY='literal-$(not-executed)-key'\nUNRELATED=value\n", {},
        )
        self.assertEqual(loaded, {
            'GOOGLE_SCHOLAR_ID': SCHOLAR_ID,
            'SERPER_API_KEY': 'literal-$(not-executed)-key',
        })

    def test_nonempty_environment_has_priority_over_local_file(self):
        loaded = self.load('SERPER_API_KEY=file-key\nGOOGLE_SCHOLAR_ID=file-id\n', {
            'SERPER_API_KEY': 'environment-key', 'GOOGLE_SCHOLAR_ID': 'environment-id',
        })
        self.assertEqual(loaded['SERPER_API_KEY'], 'environment-key')
        self.assertEqual(loaded['GOOGLE_SCHOLAR_ID'], 'environment-id')

    def test_empty_environment_uses_local_file_and_ignores_malformed_lines(self):
        loaded = self.load('invalid-line\nSERPER_API_KEY="file-key"\n', {'SERPER_API_KEY': ' '})
        self.assertEqual(loaded['SERPER_API_KEY'], 'file-key')

    def test_absent_local_configuration_is_allowed(self):
        with mock.patch.object(crawler, '__file__', str(self.directory / 'main.py')):
            with mock.patch.dict(os.environ, {}, clear=True):
                crawler.load_local_environment()
                self.assertEqual(dict(os.environ), {})


if __name__ == '__main__':
    unittest.main()
