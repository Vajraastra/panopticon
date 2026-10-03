"""Private HTTP contracts: no credentials, network or production catalog."""
import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from third_party.cherry_dl.templates import patreon
from third_party.cherry_dl.auth import patreon as auth


def page(ids=('1',), next_url=None):
    return dict(data=[dict(type='post', id=i, attributes={}) for i in ids],
                included=[], links=dict(next=next_url))


class PatreonContract(unittest.IsolatedAsyncioTestCase):
    async def consume(self, responses, *, limit=8):
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) > limit:
                raise AssertionError('pagination did not stop')
            status, body = responses[min(len(calls) - 1, len(responses) - 1)]
            return httpx.Response(status, json=body, headers={'Retry-After': '0'})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        template = patreon.PatreonTemplate(None)
        template._http = client
        self.calls = calls
        with patch.object(patreon.random, 'uniform', return_value=0), patch.object(auth, 'clear_patreon_session'):
            try:
                return [post async for post in template._iter_posts('42')]
            finally:
                await client.aclose()

    async def test_two_pages_keep_internal_api_and_campaign_filter(self):
        posts = await self.consume([(200, page(next_url='https://www.patreon.com/api/posts?page%5Bcursor%5D=next')),
                                    (200, page(('2',)))])
        self.assertEqual([p[0]['id'] for p in posts], ['1', '2'])
        self.assertEqual(len(self.calls), 2)
        for request in self.calls:
            self.assertEqual(request.url.host, 'www.patreon.com')
            self.assertEqual(request.url.path, '/api/posts')
            self.assertEqual(request.url.params['filter[campaign_id]'], '42')
            self.assertNotIn('authorization', request.headers)

    async def test_empty_page_is_valid_end(self):
        self.assertEqual(await self.consume([(200, page(()))]), [])

    async def test_missing_data_does_not_confirm_empty_scan(self):
        with self.assertRaises(RuntimeError):
            await self.consume([(200, {'meta': {'error': 'unavailable'}})])

    async def test_error_payload_does_not_confirm_empty_scan(self):
        with self.assertRaises(RuntimeError):
            await self.consume([(200, dict(data=[], errors=[{'detail': 'no access'}]))])

    async def test_bad_next_does_not_confirm_completion(self):
        with self.assertRaises(RuntimeError):
            await self.consume([(200, page(next_url='https://www.patreon.com/api/posts?page=2'))])

    async def test_duplicate_cursor_stops(self):
        with self.assertRaises(RuntimeError):
            await self.consume([(200, page(next_url='?page%5Bcursor%5D=same'))])
        self.assertLessEqual(len(self.calls), 2)

    async def test_cursor_cycle_stops(self):
        with self.assertRaises(RuntimeError):
            await self.consume([(200, page(next_url='?page%5Bcursor%5D=a')),
                                (200, page(next_url='?page%5Bcursor%5D=b')),
                                (200, page(next_url='?page%5Bcursor%5D=a'))])
        self.assertEqual(len(self.calls), 3)

    async def test_empty_with_continuation_is_not_completion(self):
        with self.assertRaises(RuntimeError):
            await self.consume([(200, page((), '?page%5Bcursor%5D=next'))])

    async def test_foreign_continuation_is_rejected(self):
        with self.assertRaises(RuntimeError):
            await self.consume([(200, page(next_url='https://example.test/api/posts?page%5Bcursor%5D=x'))])

    async def test_malformed_post_is_rejected(self):
        with self.assertRaises(RuntimeError):
            await self.consume([(200, dict(data=[{}]))])

    async def test_rejected_auth_is_not_empty_scan(self):
        for status in (401, 403):
            with self.subTest(status=status), self.assertRaises(auth.NeedsManualAuth):
                await self.consume([(status, {})])

    async def test_rate_limit_recovers_same_page(self):
        result = await self.consume([(429, {}), (200, page())])
        self.assertEqual(len(result), 1)
        self.assertEqual(str(self.calls[0].url), str(self.calls[1].url))

    async def test_persistent_rate_limit_is_bounded(self):
        with self.assertRaises(RuntimeError):
            await self.consume([(429, {})])
        self.assertLessEqual(len(self.calls), 3)

    async def test_cancel_propagates(self):
        with patch.object(patreon.asyncio, 'sleep', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.consume([(200, page())])


if __name__ == '__main__':
    unittest.main()
