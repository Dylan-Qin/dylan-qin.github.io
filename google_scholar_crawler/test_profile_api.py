"""Verify Serper scrapes one profile and accepts only explicit profile metrics."""

import contextlib
from datetime import datetime
import io
import json
import traceback
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from google_scholar_crawler import profile_api


SCHOLAR_ID = '0KTz65wAAAAJ'
PROFILE_URL = f'https://scholar.google.com/citations?user={SCHOLAR_ID}&hl=en'
DUMMY_KEY = 'unit-test-private-key-never-log'
MARKDOWN = '''# Chuan Qin

| | All | Since 2021 |
| --- | --- | --- |
| Citations | 5,711 | 5,000 |
| h-index | 35 | 30 |
| i10-index | 55 | 50 |

## Publications
This article has 999999 citations. These are not the profile metrics.
'''


class ProfileApiTests(unittest.TestCase):
    def payload(self, markdown=MARKDOWN):
        return {
            'markdown': markdown,
            'metadata': {'title': 'Chuan Qin - Google Scholar', 'url': PROFILE_URL},
            # Neither a search result nor a pagination token should be followed.
            'organic': [{'citedBy': 999999}],
            'next': 'https://untrusted.example/next',
        }

    def fetch(self, payload):
        response = io.StringIO(json.dumps(payload))
        with mock.patch.object(profile_api, 'urlopen', return_value=response) as request:
            author = profile_api.fetch_author(SCHOLAR_ID, DUMMY_KEY)
        return author, request

    def assert_profile_totals(self, author, total=5711, recent=5000):
        self.assertEqual(author['scholar_id'], SCHOLAR_ID)
        self.assertEqual(author['citedby'], total)
        self.assertEqual(author['citedby5y'], recent)
        self.assertEqual(author['hindex'], 35)
        self.assertEqual(author['hindex5y'], 30)
        self.assertEqual(author['i10index'], 55)
        self.assertEqual(author['i10index5y'], 50)
        self.assertEqual(author['publications'], {})
        self.assertEqual(author['data_source'], 'serper')
        self.assertNotIn(DUMMY_KEY, json.dumps(author))
        self.assertIsNotNone(datetime.fromisoformat(author['updated']).tzinfo)

    def test_one_post_scrapes_only_the_requested_profile(self):
        author, request = self.fetch(self.payload())
        request.assert_called_once()
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, 'https://scrape.serper.dev')
        self.assertEqual(sent.get_method(), 'POST')
        self.assertEqual(json.loads(sent.data), {'url': PROFILE_URL, 'includeMarkdown': True})
        headers = {name.lower(): value for name, value in sent.header_items()}
        self.assertEqual(headers['x-api-key'], DUMMY_KEY)
        self.assertIn('application/json', headers['content-type'])
        self.assertEqual(request.call_args.kwargs['timeout'], 60)
        self.assert_profile_totals(author)
        self.assertNotIn('organic', author)
        self.assertNotIn('next', author)

    def test_markdown_links_and_bold_labels_are_accepted(self):
        markdown = MARKDOWN.replace('Citations', f'**[Citations]({PROFILE_URL})**')
        markdown = markdown.replace('h-index', '**h-index**').replace('i10-index', '**i10-index**')
        author, _ = self.fetch(self.payload(markdown))
        self.assert_profile_totals(author)

    def test_nonbreaking_space_grouped_numbers_are_accepted(self):
        markdown = MARKDOWN.replace('5,711', '5\u00a0711').replace('5,000', '5\u202f000')
        author, _ = self.fetch(self.payload(markdown))
        self.assert_profile_totals(author)

    def test_multiline_plaintext_metrics_are_accepted(self):
        data = self.payload()
        data.pop('markdown')
        data['text'] = (
            'Chuan Qin\nAll\nSince 2021\nCitations\n5,711\n5,000\n'
            'h-index\n35\n30\ni10-index\n55\n50\nPublications\n'
            'A paper with Cited by 999999\n'
        )
        author, _ = self.fetch(data)
        self.assert_profile_totals(author)

    def test_single_line_plaintext_rows_are_accepted(self):
        data = self.payload()
        data.pop('markdown')
        data['text'] = (
            'Chuan Qin\nAll Since 2021\nCitations 5711 5000\n'
            'h-index 35 30\ni10-index 55 50\n'
        )
        author, _ = self.fetch(data)
        self.assert_profile_totals(author)

    def test_valid_plaintext_is_available_when_markdown_is_empty(self):
        data = self.payload(markdown='')
        data['text'] = 'All Since 2021\nCitations 5711 5000\nh-index 35 30\ni10-index 55 50'
        author, _ = self.fetch(data)
        self.assert_profile_totals(author)

    def test_real_zero_is_not_treated_as_a_fetch_failure(self):
        markdown = MARKDOWN.replace('5,711', '0').replace('5,000', '0')
        author, _ = self.fetch(self.payload(markdown))
        self.assert_profile_totals(author, total=0, recent=0)

    def test_recent_column_year_is_not_hardcoded(self):
        author, _ = self.fetch(self.payload(MARKDOWN.replace('Since 2021', 'Since 2030')))
        self.assert_profile_totals(author)

    def test_no_table_or_no_all_since_headers_is_not_accepted_as_profile_data(self):
        for content in (
            'Chuan Qin. Cited by 5711.',
            'Citations 5711 5000\nh-index 35 30\ni10-index 55 50',
            '| Citations | 5711 | 5000 |\n| h-index | 35 | 30 |\n| i10-index | 55 | 50 |',
            'Our systems have detected unusual traffic. Please verify you are not a robot.',
            'Sign in to continue to Google Scholar',
        ):
            with self.subTest(content=content):
                with self.assertRaises(profile_api.SourceUnavailable):
                    self.fetch(self.payload(content))

    def test_invalid_and_ambiguous_counts_are_not_replaced_with_zero(self):
        for value in ('unknown', '-1', '5.7k', '5,71'):
            with self.subTest(value=value):
                with self.assertRaises(profile_api.SourceUnavailable):
                    self.fetch(self.payload(MARKDOWN.replace('5,711', value)))
        with self.assertRaises(profile_api.SourceUnavailable):
            self.fetch(self.payload(MARKDOWN + '\n' + MARKDOWN.replace('5,711', '6000')))

    def test_metadata_must_not_identify_a_different_scholar_author(self):
        for field in ('url', 'canonical'):
            with self.subTest(field=field):
                data = self.payload()
                data['metadata'][field] = 'https://scholar.google.com/citations?user=someone-else&hl=en'
                with self.assertRaises(ValueError):
                    self.fetch(data)

    def test_metadata_is_optional_for_valid_profile_metrics(self):
        data = self.payload()
        data.pop('metadata')
        author, _ = self.fetch(data)
        self.assert_profile_totals(author)

    def test_live_scholar_title_direction_marks_are_removed(self):
        data = self.payload()
        data['metadata']['title'] = '\u202aChuan Qin\u202c - \u202aGoogle Scholar\u202c'
        author, _ = self.fetch(data)
        self.assertEqual(author['name'], 'Chuan Qin')

    def test_service_error_is_not_published_or_echoed(self):
        with self.assertRaises(profile_api.SourceUnavailable) as raised:
            self.fetch({'error': f'upstream failure with {DUMMY_KEY}'})
        self.assertNotIn(DUMMY_KEY, str(raised.exception))

    def test_transport_failures_do_not_log_private_header_or_error_body(self):
        cases = [
            (HTTPError('https://scrape.serper.dev', 401, DUMMY_KEY, {}, io.BytesIO(DUMMY_KEY.encode())),
             profile_api.ConfigurationError),
            (HTTPError('https://scrape.serper.dev', 403, DUMMY_KEY, {}, io.BytesIO(DUMMY_KEY.encode())),
             profile_api.ConfigurationError),
            (HTTPError('https://scrape.serper.dev', 400, DUMMY_KEY, {}, io.BytesIO(DUMMY_KEY.encode())),
             profile_api.ConfigurationError),
            (HTTPError('https://scrape.serper.dev', 402, DUMMY_KEY, {}, io.BytesIO(DUMMY_KEY.encode())),
             profile_api.SourceUnavailable),
            (HTTPError('https://scrape.serper.dev', 429, DUMMY_KEY, {}, io.BytesIO(DUMMY_KEY.encode())),
             profile_api.SourceUnavailable),
            (HTTPError('https://scrape.serper.dev', 503, DUMMY_KEY, {}, io.BytesIO(DUMMY_KEY.encode())),
             profile_api.SourceUnavailable),
            (URLError(DUMMY_KEY), profile_api.SourceUnavailable),
            (TimeoutError(DUMMY_KEY), profile_api.SourceUnavailable),
        ]
        for error, expected in cases:
            with self.subTest(error_type=type(error).__name__, code=getattr(error, 'code', None)):
                with mock.patch.object(profile_api, 'urlopen', side_effect=error) as request:
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        with contextlib.redirect_stderr(io.StringIO()) as errors:
                            try:
                                profile_api.fetch_author(SCHOLAR_ID, DUMMY_KEY)
                            except expected as caught:
                                formatted = ''.join(traceback.format_exception(
                                    type(caught), caught, caught.__traceback__,
                                ))
                            else:
                                self.fail('Expected a sanitized API exception')
                request.assert_called_once()
                self.assertNotIn(DUMMY_KEY, output.getvalue() + errors.getvalue() + formatted)

    def test_invalid_json_is_temporary_failure_without_echoing_response(self):
        with mock.patch.object(profile_api, 'urlopen', return_value=io.StringIO(DUMMY_KEY)):
            with self.assertRaises(profile_api.SourceUnavailable) as raised:
                profile_api.fetch_author(SCHOLAR_ID, DUMMY_KEY)
        self.assertNotIn(DUMMY_KEY, str(raised.exception))


if __name__ == '__main__':
    unittest.main()
