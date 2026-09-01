"""
Simulates N independent dialer workers (as separate threads) all operating
on the SAME shared store/campaign concurrently - this is the harness that
proves the "two workers try to reserve the same agent" scenario is handled
correctly, without needing real separate machines/processes. The workers
share nothing except the InMemoryStore, which is exactly the boundary a
real distributed deployment would put behind a database.
"""

import threading
import time
import logging

logger = logging.getLogger("smartdialer.worker")


class DialerWorkerPool:
    def __init__(self, dialer, num_workers: int = 4, poll_interval: float = 0.05):
        """`dialer` is anything with a `.run_once() -> int` method
        (ProgressiveDialer, or a predictive-mode equivalent)."""
        self._dialer = dialer
        self.num_workers = num_workers
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.total_calls_placed = 0
        self._lock = threading.Lock()

    def _worker_loop(self, worker_id: int):
        while not self._stop.is_set():
            placed = self._dialer.run_once()
            if placed:
                with self._lock:
                    self.total_calls_placed += placed
            time.sleep(self.poll_interval)

    def start(self):
        for i in range(self.num_workers):
            t = threading.Thread(target=self._worker_loop, args=(i,), daemon=True)
            t.start()
            self._threads.append(t)
        logger.info(f"Started {self.num_workers} dialer workers")

    def stop(self, join_timeout: float = 2.0):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=join_timeout)
