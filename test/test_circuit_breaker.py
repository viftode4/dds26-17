"""Tests for orchestrator.executor.CircuitBreaker state machine."""
from unittest.mock import patch

from orchestrator.executor import CircuitBreaker


class TestCircuitBreaker:

    def test_closed_by_default(self):
        cb = CircuitBreaker()
        assert cb._state == "closed"
        assert cb.is_open() is False

    def test_opens_at_threshold(self):
        cb = CircuitBreaker(failure_threshold=5)
        for _ in range(5):
            cb.record_failure()
        assert cb._state == "open"
        assert cb.is_open() is True

    def test_does_not_open_below_threshold(self):
        cb = CircuitBreaker(failure_threshold=5)
        for _ in range(4):
            cb.record_failure()
        assert cb._state == "closed"
        assert cb.is_open() is False

    @patch("time.monotonic")
    def test_open_to_half_open_after_recovery_timeout(self, mock_time):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=10.0)
        mock_time.return_value = 100.0
        cb.record_failure()
        cb.record_failure()
        assert cb._state == "open"

        # Before timeout
        mock_time.return_value = 109.0
        assert cb.is_open() is True

        # After timeout — transitions to half-open, allows probe
        mock_time.return_value = 111.0
        assert cb.is_open() is False
        assert cb._state == "half-open"

    def test_half_open_success_closes(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb._state = "half-open"
        cb._failure_count = 2
        cb.record_success()
        assert cb._state == "closed"
        assert cb._failure_count == 0

    def test_failure_while_open_stays_open(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure()
        cb.record_failure()
        assert cb._state == "open"
        # Extra failures don't change state
        cb.record_failure()
        cb.record_failure()
        assert cb._state == "open"

    def test_record_success_from_closed_resets_count(self):
        cb = CircuitBreaker(failure_threshold=5)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb._failure_count == 3
        cb.record_success()
        assert cb._failure_count == 0
        assert cb._state == "closed"
