"""Wake timed quota waits on released reservations, without real window sleeps."""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'third_party'))
from cherry_dl.site_throttle import SiteThrottle


class ReservationWakeup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sleeping = asyncio.Event()
        self.timer_cancelled = asyncio.Event()
        self.waits = []

        async def sleep(seconds):
            self.waits.append(seconds)
            self.sleeping.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.timer_cancelled.set()

        self.gate = SiteThrottle(
            'pawchive', limit=500, byte_limit=100, window_seconds=7200,
            failure_threshold=5, failure_window_seconds=120,
            cooldowns=(900,), path=Path(self.temp.name)/'throttle.json',
            clock=lambda: 1_000_000., sleeper=sleep,
        )
        permit = await self.gate.acquire(expected_bytes=40)
        await self.gate.finish(permit, success=True, transferred_bytes=40)
        self.permit = await self.gate.acquire()
        self.assertEqual(self.permit.reserved_bytes, 60)
        self.waiter = asyncio.create_task(self.gate.acquire(expected_bytes=10))
        self.addAsyncCleanup(self.drain)
        await asyncio.wait_for(self.sleeping.wait(), 1)
        self.assertEqual(self.waits, [7200])
        self.assertFalse(self.waiter.done())

    async def drain(self):
        if not self.waiter.done():
            self.waiter.cancel()
        await asyncio.gather(self.waiter, return_exceptions=True)
        if not self.waiter.cancelled() and self.waiter.exception() is None:
            await self.gate.abandon(self.waiter.result())
        await self.gate.abandon(self.permit)

    async def resumed(self):
        second = await asyncio.wait_for(self.waiter, .2)
        self.assertEqual(second.reserved_bytes, 10)
        self.assertTrue(second.active)
        self.assertTrue(self.timer_cancelled.is_set())
        self.assertLessEqual(self.gate.snapshot().used_bytes
                             + self.gate._reserved_bytes, 100)

    async def test_headers_resize_wakes_before_window_expiry(self):
        self.assertEqual(await self.gate.resize(self.permit, 20), 20)
        await self.resumed()

    async def test_failed_transfer_wakes_before_window_expiry(self):
        await self.gate.finish(self.permit, success=False)
        await self.resumed()

    async def test_completed_transfer_releases_unused_reserved_bytes(self):
        await self.gate.finish(self.permit, success=True, transferred_bytes=20)
        await self.resumed()
        self.assertEqual(self.gate.snapshot().used_bytes, 60)

    async def test_abandoned_transfer_wakes_before_window_expiry(self):
        await self.gate.abandon(self.permit)
        await self.resumed()

    async def test_waiter_cancellation_drains_timer(self):
        self.waiter.cancel()
        await asyncio.gather(self.waiter, return_exceptions=True)
        self.assertTrue(self.timer_cancelled.is_set())
        self.assertEqual(self.gate.snapshot().active_files, 1)
        self.assertEqual(self.gate._reserved_bytes, 60)


if __name__ == '__main__':
    unittest.main()
