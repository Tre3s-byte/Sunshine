import time
from collections import deque
from threading import Lock


class MetricsTracker:
    """Thread-safe ring buffer of timed operations.

    Records duration_ms of every wrapped call so we can identify hot paths
    that may trigger anti-cheat detection (e.g. process_iter spikes correlating
    with NTE stutters).
    """

    def __init__(self, max_samples: int = 5000):
        self._samples: deque = deque(maxlen=max_samples)
        self._lock = Lock()

    def record(self, operation: str, duration_ms: float, **details) -> None:
        sample = {"ts": time.time(), "op": operation, "ms": round(duration_ms, 3)}
        if details:
            sample["d"] = details
        with self._lock:
            self._samples.append(sample)

    def time_call(self, operation: str, **details) -> "_Timer":
        return _Timer(self, operation, details)

    def recent(self, since_seconds: int = 300, op: str | None = None, limit: int = 500) -> list:
        cutoff = time.time() - since_seconds
        with self._lock:
            samples = [s for s in self._samples if s["ts"] >= cutoff]
        if op:
            samples = [s for s in samples if s["op"] == op]
        return samples[-limit:]

    def stats(self, since_seconds: int = 300) -> dict:
        cutoff = time.time() - since_seconds
        with self._lock:
            samples = [s for s in self._samples if s["ts"] >= cutoff]

        by_op: dict[str, list[float]] = {}
        for s in samples:
            by_op.setdefault(s["op"], []).append(s["ms"])

        result = {}
        for op, times in by_op.items():
            times_sorted = sorted(times)
            n = len(times_sorted)
            result[op] = {
                "count": n,
                "avg_ms": round(sum(times) / n, 3),
                "max_ms": round(times_sorted[-1], 3),
                "p50_ms": round(times_sorted[n // 2], 3),
                "p99_ms": round(times_sorted[min(int(n * 0.99), n - 1)], 3),
            }
        return result


class _Timer:
    __slots__ = ("tracker", "operation", "details", "_start")

    def __init__(self, tracker: MetricsTracker, operation: str, details: dict):
        self.tracker = tracker
        self.operation = operation
        self.details = details
        self._start = 0.0

    def __enter__(self) -> "_Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_exc) -> None:
        duration_ms = (time.perf_counter() - self._start) * 1000
        self.tracker.record(self.operation, duration_ms, **self.details)
