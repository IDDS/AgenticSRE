import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from orchestrator.daemon import Daemon


class DaemonPendingSignalsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cfg = SimpleNamespace(daemon=SimpleNamespace(
            poll_interval_seconds=1, dedup_ttl_seconds=10,
            max_concurrent_pipelines=1, default_namespace='testbed'))
        self.pipeline = Mock()
        self.pipeline._signals_to_trigger.side_effect = lambda signals: list(signals)
        with patch('orchestrator.daemon.Pipeline', return_value=self.pipeline):
            self.daemon = Daemon(cfg)
        self.triggered = []

        async def run(trigger):
            self.triggered.append(trigger)

        self.daemon._run_pipeline = run

    async def test_busy_signal_survives_quiet_cycle_and_expired_dedup(self):
        sig = SimpleNamespace(fingerprint='target-a')
        self.pipeline.detection_agent.detect.side_effect = [[sig], [], []]
        self.daemon._active_pipelines['busy'] = object()
        await self.daemon._tick()
        self.assertEqual(list(self.daemon._pending_signals), ['target-a'])
        self.daemon._dedup_map['target-a'].last_seen = time.time() - 100
        await self.daemon._tick()
        self.assertIn('target-a', self.daemon._dedup_map)
        self.daemon._active_pipelines.clear()
        await self.daemon._tick()
        await asyncio.sleep(0)
        self.assertEqual(self.triggered, [[sig]])
        self.assertFalse(self.daemon._pending_signals)

    async def test_pending_dispatches_when_next_detection_fails(self):
        sig = SimpleNamespace(fingerprint='target-a')
        self.pipeline.detection_agent.detect.side_effect = [[sig], RuntimeError('unavailable')]
        self.daemon._active_pipelines['busy'] = object()
        await self.daemon._tick()
        self.daemon._active_pipelines.clear()
        await self.daemon._tick()
        await asyncio.sleep(0)
        self.assertEqual(self.triggered, [[sig]])

    async def test_full_queue_never_deduplicates_unaccepted_signal(self):
        self.daemon._max_pending_signals = 1
        a, b = SimpleNamespace(fingerprint='a'), SimpleNamespace(fingerprint='b')
        self.pipeline.detection_agent.detect.side_effect = [[a, b], [b], [b]]
        self.daemon._active_pipelines['busy'] = object()
        await self.daemon._tick()
        self.assertIn('a', self.daemon._pending_signals)
        self.assertNotIn('b', self.daemon._dedup_map)
        self.daemon._active_pipelines.clear()
        await self.daemon._tick()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(self.triggered, [[a]])
        await self.daemon._tick()
        await asyncio.sleep(0)
        self.assertEqual(self.triggered, [[a], [b]])

    async def test_trigger_failure_retains_pending_signal(self):
        sig = SimpleNamespace(fingerprint='target-a')
        self.pipeline.detection_agent.detect.side_effect = [[sig], []]
        self.pipeline._signals_to_trigger.side_effect = [RuntimeError('format failed'), [sig]]
        await self.daemon._tick()
        self.assertIn('target-a', self.daemon._pending_signals)
        await self.daemon._tick()
        await asyncio.sleep(0)
        self.assertEqual(self.triggered, [[sig]])

    async def test_detection_does_not_block_event_loop(self):
        scheduled = threading.Event()
        seen = []
        asyncio.get_running_loop().call_later(.05, scheduled.set)

        def slow_detection(namespace):
            scheduled.wait(.5)
            seen.append(scheduled.is_set())
            return []

        self.pipeline.detection_agent.detect.side_effect = slow_detection
        await self.daemon._tick()
        self.assertEqual(seen, [True])

    async def test_daemon_marks_pipeline_as_detection_trigger(self):
        self.pipeline.run = AsyncMock(return_value=SimpleNamespace(status='completed', duration_s=1))
        await Daemon._run_pipeline(self.daemon, 'detected incident')
        self.pipeline.run.assert_awaited_once_with(
            'detected incident', 'testbed', None, trigger_source='detection')

    async def test_dispatches_large_backlog_in_bounded_batches(self):
        signals = [SimpleNamespace(fingerprint=str(i)) for i in range(6)]
        self.pipeline.detection_agent.detect.side_effect = [signals, []]
        await self.daemon._tick()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(self.triggered, [signals[:5]])
        self.assertEqual(list(self.daemon._pending_signals.values()), signals[5:])
        await self.daemon._tick()
        await asyncio.sleep(0)
        self.assertEqual(self.triggered, [signals[:5], signals[5:]])


if __name__ == '__main__':
    unittest.main()
