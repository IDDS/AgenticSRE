"""
AgenticSRE 7×24 Daemon
Continuous monitoring daemon with:
- Periodic detection scans
- Signal deduplication (fingerprint + TTL)
- Concurrent pipeline execution
- Graceful shutdown
- Health check endpoint
"""

import asyncio
import hashlib
import logging
import signal
import time
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from configs.config_loader import get_config
from orchestrator.pipeline import Pipeline

logger = logging.getLogger(__name__)


@dataclass
class _SignalRecord:
    """Dedup record for a detection signal."""
    fingerprint: str
    first_seen: float
    last_seen: float
    count: int = 1


class Daemon:
    """
    7×24 continuous monitoring daemon.
    
    Features:
    - Poll-based detection with configurable interval
    - MD5 fingerprint dedup with configurable TTL
    - Thread-pool for concurrent RCA pipelines
    - Graceful shutdown via SIGINT/SIGTERM
    - SSE-compatible log streaming
    """

    def __init__(self, config=None, log_callback: Optional[Callable] = None,
                 signal_callback: Optional[Callable] = None):
        self.cfg = config or get_config()
        self.pipeline = Pipeline(self.cfg)
        self.log_callback = log_callback
        self.signal_callback = signal_callback

        # Daemon settings
        self.poll_interval = self.cfg.daemon.poll_interval_seconds
        self.dedup_ttl = self.cfg.daemon.dedup_ttl_seconds
        self.max_concurrent = self.cfg.daemon.max_concurrent_pipelines
        self.namespace = self.cfg.daemon.default_namespace

        # State
        self._running = False
        self._dedup_map: Dict[str, _SignalRecord] = {}
        # Keep accepted signals until a pipeline slot is available. This queue
        # is intentionally bounded and in-memory, not a durable event store.
        self._pending_signals: Dict[str, Any] = {}
        self._max_pending_signals = 256
        self._active_pipelines: Dict[str, asyncio.Task] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._cycle_count = 0
        self._start_time: Optional[float] = None

    # ── Lifecycle ──

    async def start(self):
        """Start the daemon loop."""
        self._running = True
        self._start_time = time.time()
        self._loop = asyncio.get_event_loop()
        
        # Register signals (only works in main thread)
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                self._loop.add_signal_handler(sig, lambda: asyncio.ensure_future(self.stop()))
        except (RuntimeError, NotImplementedError):
            pass  # Not in main thread or platform doesn't support it

        self._log("🚀 AgenticSRE Daemon started")
        self._log(f"  Poll interval: {self.poll_interval}s")
        self._log(f"  Dedup TTL: {self.dedup_ttl}s")
        self._log(f"  Max concurrent: {self.max_concurrent}")
        self._log(f"  Namespace: {self.namespace or 'all'}")

        try:
            while self._running:
                await self._tick()
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            self._log("Daemon loop cancelled")
        finally:
            await self._cleanup()

    async def stop(self):
        """Graceful shutdown."""
        self._log("🛑 Shutdown signal received, stopping daemon...")
        self._running = False

    async def _cleanup(self):
        """Wait for active pipelines, then exit."""
        if self._active_pipelines:
            self._log(f"  Waiting for {len(self._active_pipelines)} active pipelines...")
            for task in self._active_pipelines.values():
                task.cancel()
            await asyncio.gather(*self._active_pipelines.values(), return_exceptions=True)
        self._log("✅ Daemon stopped cleanly.")

    # ── Core Loop ──

    async def _tick(self):
        """Single daemon tick: detect → dedup → dispatch."""
        self._cycle_count += 1
        self._purge_expired_dedup()

        try:
            # Run detection
            try:
                # Detection performs synchronous network I/O; running it on the
                # event loop stalls every active diagnosis and shutdown task.
                signals = await asyncio.to_thread(
                    self.pipeline.detection_agent.detect, namespace=self.namespace)
            except Exception as exc:
                logger.error("Daemon detection failed: %s", exc, exc_info=True)
                self._log("  ❌ Detection failed; retaining and processing pending signals")
                signals = []
            
            # Dedup signals
            new_signals = self._dedup_signals(signals)
            if signals and not new_signals:
                self._log(f"  [cycle {self._cycle_count}] {len(signals)} signals; none newly accepted")
            elif new_signals:
                self._log(f"  [cycle {self._cycle_count}] {len(new_signals)}/{len(signals)} new signals")

            # Push signals to SSE stream
            if self.signal_callback:
                for s in new_signals:
                    self.signal_callback(s)

            if not self._pending_signals:
                if not signals and self._cycle_count % 10 == 0:
                    self._log(f"  [cycle {self._cycle_count}] No anomalies detected")
                return

            # Dispatch pipeline (if capacity). Pending signals survive quiet
            # cycles and must not be discarded merely because the pool is full.
            active_count = len(self._active_pipelines)
            if active_count >= self.max_concurrent:
                self._log(f"  ⚠️ Max concurrent pipelines reached ({active_count}); "
                          f"{len(self._pending_signals)} signals pending in memory")
                return

            # The pipeline formats at most five signals per trigger. Leave the
            # remainder queued for later cycles rather than silently dropping it.
            pending = list(self._pending_signals.items())[:5]
            trigger = self.pipeline._signals_to_trigger([item[1] for item in pending])
            task = asyncio.create_task(self._run_pipeline(trigger))
            task_id = f"pipe-{uuid.uuid4().hex}"
            self._active_pipelines[task_id] = task
            for fingerprint, _ in pending:
                self._pending_signals.pop(fingerprint, None)
            task.add_done_callback(lambda t, tid=task_id: self._pipeline_done(tid))

        except Exception as e:
            logger.error(f"Daemon tick error: {e}", exc_info=True)
            self._log(f"  ❌ Detection error: {e}")

    async def _run_pipeline(self, trigger: str):
        """Execute a single pipeline in the background."""
        self._log(f"\n{'='*60}")
        self._log(f"🔔 New incident detected, starting pipeline...")
        result = await self.pipeline.run(
            trigger, self.namespace, self.log_callback, trigger_source="detection")
        self._log(f"📊 Pipeline completed: {result.status} ({result.duration_s:.1f}s)")
        return result

    def _pipeline_done(self, task_id: str):
        """Callback when a pipeline task finishes."""
        self._active_pipelines.pop(task_id, None)

    # ── Signal Deduplication ──

    def _dedup_signals(self, signals: List) -> List:
        """Filter out signals already seen within TTL."""
        new_signals = []
        now = time.time()
        
        for s in signals:
            fp = self._fingerprint(s)
            if fp in self._dedup_map:
                record = self._dedup_map[fp]
                record.last_seen = now
                record.count += 1
            else:
                if len(self._pending_signals) >= self._max_pending_signals:
                    self._log("  ⚠️ Pending signal limit reached; signal was not queued or deduplicated")
                    continue
                self._dedup_map[fp] = _SignalRecord(
                    fingerprint=fp,
                    first_seen=now,
                    last_seen=now,
                )
                self._pending_signals[fp] = s
                new_signals.append(s)
        
        return new_signals

    def _fingerprint(self, signal_obj) -> str:
        """Generate MD5 fingerprint for dedup."""
        if hasattr(signal_obj, "fingerprint"):
            return signal_obj.fingerprint
        
        raw = str(signal_obj)
        return hashlib.md5(raw.encode()).hexdigest()

    def _purge_expired_dedup(self):
        """Remove dedup entries older than TTL."""
        now = time.time()
        expired = [fp for fp, rec in self._dedup_map.items()
                   if fp not in self._pending_signals and now - rec.last_seen > self.dedup_ttl]
        for fp in expired:
            del self._dedup_map[fp]

    # ── Status & Health ──

    def status(self) -> Dict:
        """Current daemon status."""
        uptime = time.time() - self._start_time if self._start_time else 0
        return {
            "running": self._running,
            "uptime_s": round(uptime, 1),
            "cycles": self._cycle_count,
            "active_pipelines": len(self._active_pipelines),
            "dedup_entries": len(self._dedup_map),
            "pending_signals": len(self._pending_signals),
            "pipeline_stats": self.pipeline.get_stats(),
        }

    def health_check(self) -> Dict:
        """Health check for monitoring."""
        return {
            "healthy": self._running,
            "uptime_s": round(time.time() - self._start_time, 1) if self._start_time else 0,
            "cycles": self._cycle_count,
            "last_cycle_time": self.poll_interval,
        }

    # ── Logging ──

    def _log(self, msg: str):
        logger.info(msg)
        if self.log_callback:
            self.log_callback(msg)


def run_daemon(config=None, log_callback=None, signal_callback=None):
    """Entry-point: create and run the daemon."""
    daemon = Daemon(config, log_callback, signal_callback)
    asyncio.run(daemon.start())
    return daemon
