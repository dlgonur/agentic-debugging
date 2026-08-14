import time

import pytest

from agentic_debugger.cancellation import (
    CancellationError,
    CancellationReason,
    CancellationToken,
)


class TestCancellationError:
    def test_reason_is_validated(self):
        error = CancellationError(CancellationReason.CANCELLED)
        assert error.reason is CancellationReason.CANCELLED
        assert "cancelled" in str(error)
        with pytest.raises(TypeError):
            CancellationError("cancelled")


class TestCancellationToken:
    def test_initial_state_is_quiet(self):
        token = CancellationToken()
        assert token.is_cancelled is False
        assert token.reason is None
        assert token.deadline is None
        token.check()  # must not raise

    def test_request_sets_reason_and_check_raises(self):
        token = CancellationToken()
        token.request()
        assert token.is_cancelled is True
        assert token.reason is CancellationReason.CANCELLED
        with pytest.raises(CancellationError) as raised:
            token.check()
        assert raised.value.reason is CancellationReason.CANCELLED

    def test_first_reason_wins(self):
        token = CancellationToken()
        token.request(CancellationReason.TIMED_OUT)
        token.request(CancellationReason.CANCELLED)
        assert token.reason is CancellationReason.TIMED_OUT
        with pytest.raises(CancellationError) as raised:
            token.check()
        assert raised.value.reason is CancellationReason.TIMED_OUT

    def test_request_reason_is_validated(self):
        token = CancellationToken()
        with pytest.raises(TypeError):
            token.request("cancelled")

    def test_deadline_fires_timed_out(self):
        token = CancellationToken(deadline_monotonic=time.monotonic() + 0.05)
        time.sleep(0.1)
        with pytest.raises(CancellationError) as raised:
            token.check()
        assert raised.value.reason is CancellationReason.TIMED_OUT
        assert token.is_cancelled is True
        assert token.reason is CancellationReason.TIMED_OUT

    def test_explicit_request_wins_over_deadline(self):
        token = CancellationToken(deadline_monotonic=time.monotonic() + 0.05)
        token.request(CancellationReason.CANCELLED)
        time.sleep(0.1)
        assert token.reason is CancellationReason.CANCELLED
        with pytest.raises(CancellationError) as raised:
            token.check()
        assert raised.value.reason is CancellationReason.CANCELLED

    def test_deadline_validation(self):
        with pytest.raises(TypeError):
            CancellationToken(deadline_monotonic="later")
        with pytest.raises(ValueError):
            CancellationToken(deadline_monotonic=0)
        with pytest.raises(ValueError):
            CancellationToken(deadline_monotonic=-1.0)

    def test_check_before_deadline_is_quiet(self):
        token = CancellationToken(deadline_monotonic=time.monotonic() + 30.0)
        token.check()

    def test_shared_token_across_threads(self):
        import threading

        token = CancellationToken()
        seen = []
        stop = time.monotonic() + 5.0

        def worker():
            while time.monotonic() < stop:
                try:
                    token.check()
                except CancellationError as exc:
                    seen.append(exc.reason)
                    return
                time.sleep(0.01)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        time.sleep(0.05)
        token.request()
        for thread in threads:
            thread.join(timeout=5.0)
        assert len(seen) == 4
        assert all(reason is CancellationReason.CANCELLED for reason in seen)
