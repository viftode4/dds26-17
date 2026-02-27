import collections
import time


class LatencyHistogram:
    """Fixed-bucket histogram for p50/p95/p99 latency tracking."""

    # Bucket boundaries in milliseconds
    BUCKETS_MS = [5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]

    def __init__(self):
        self._counts = {b: 0 for b in self.BUCKETS_MS}
        self._overflow = 0  # > 10s
        self._sum_ms = 0.0
        self._total = 0

    def observe(self, duration_seconds: float):
        ms = duration_seconds * 1000
        self._sum_ms += ms
        self._total += 1
        for bucket in self.BUCKETS_MS:
            if ms <= bucket:
                self._counts[bucket] += 1
                return
        self._overflow += 1

    def prometheus_lines(self, metric_name: str, labels: str = "") -> list[str]:
        """Emit Prometheus histogram-format lines (cumulative buckets)."""
        lines = []
        cumulative = 0
        for bucket_ms, count in self._counts.items():
            cumulative += count
            le = bucket_ms / 1000.0
            if labels:
                lines.append(
                    f'{metric_name}_bucket{{{labels},le="{le}"}} {cumulative}'
                )
            else:
                lines.append(f'{metric_name}_bucket{{le="{le}"}} {cumulative}')
        total = cumulative + self._overflow
        if labels:
            lines.append(f'{metric_name}_bucket{{{labels},le="+Inf"}} {total}')
            lines.append(f'{metric_name}_sum{{{labels}}} {self._sum_ms / 1000:.6f}')
            lines.append(f'{metric_name}_count{{{labels}}} {total}')
        else:
            lines.append(f'{metric_name}_bucket{{le="+Inf"}} {total}')
            lines.append(f'{metric_name}_sum {self._sum_ms / 1000:.6f}')
            lines.append(f'{metric_name}_count {total}')
        return lines


class MetricsCollector:
    """Sliding window metrics for adaptive protocol selection + latency histograms."""

    def __init__(self, window_size: int = 100):
        self.window_size = window_size
        self._results: collections.deque = collections.deque(maxlen=window_size)
        self.current_protocol = "2pc"  # Start with 2PC (faster happy path)
        self.total_success = 0
        self.total_failure = 0
        # Per-protocol latency histograms (proves adaptive switching value)
        self.latency_2pc = LatencyHistogram()
        self.latency_saga = LatencyHistogram()

    def record(self, success: bool, protocol: str, duration: float = 0.0):
        """Record a transaction result and latency."""
        self._results.append({
            "success": success,
            "protocol": protocol,
            "timestamp": time.time(),
        })
        if success:
            self.total_success += 1
        else:
            self.total_failure += 1
        if duration > 0:
            if protocol == "2pc":
                self.latency_2pc.observe(duration)
            else:
                self.latency_saga.observe(duration)

    def sliding_abort_rate(self) -> float:
        """Calculate abort rate over the sliding window."""
        if not self._results:
            return 0.0
        failures = sum(1 for r in self._results if not r["success"])
        return failures / len(self._results)
