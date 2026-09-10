"""Politeness and resumability. These are what let a 549-broker sweep run."""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataremoval import verify


def test_host_gate_serializes_one_host():
    """Two workers must never be inside the same host at once."""
    gate = verify.HostGate(min_gap=0)
    inside = []
    peak = [0]
    lock = threading.Lock()

    def worker():
        with gate.hold("example.com"):
            with lock:
                inside.append(1)
                peak[0] = max(peak[0], len(inside))
            time.sleep(0.02)
            with lock:
                inside.pop()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak[0] == 1, "host gate let concurrent requests through to one host"


def test_host_gate_allows_different_hosts_in_parallel():
    """Serializing per host must not serialize the whole sweep - that would
    make 549 brokers take hours."""
    gate = verify.HostGate(min_gap=0)
    start = time.monotonic()
    threads = []
    for i in range(6):
        t = threading.Thread(target=lambda i=i: _hold(gate, f"host{i}.com"))
        threads.append(t)
        t.start()
    [t.join() for t in threads]
    assert time.monotonic() - start < 0.20, "different hosts were serialized"


def _hold(gate, host):
    with gate.hold(host):
        time.sleep(0.05)


def test_host_gate_enforces_minimum_gap():
    gate = verify.HostGate(min_gap=0.15)
    start = time.monotonic()
    for _ in range(3):
        with gate.hold("example.com"):
            pass
    # Two gaps between three requests.
    assert time.monotonic() - start >= 0.28


def test_rate_limit_caps_throughput():
    limiter = verify.RateLimit(per_second=20)
    start = time.monotonic()
    for _ in range(10):
        limiter.acquire()
    assert time.monotonic() - start >= 0.40, "global rate cap was not enforced"


def test_rate_limit_zero_is_uncapped():
    limiter = verify.RateLimit(per_second=0)
    start = time.monotonic()
    for _ in range(100):
        limiter.acquire()
    assert time.monotonic() - start < 0.05


def test_rate_limit_is_shared_across_threads():
    limiter = verify.RateLimit(per_second=20)
    start = time.monotonic()
    threads = [threading.Thread(target=limiter.acquire) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert time.monotonic() - start >= 0.40, "workers each got their own budget"


def test_retry_after_seconds_and_garbage():
    assert verify.parse_retry_after("120") == 120.0
    assert verify.parse_retry_after("  30 ") == 30.0
    assert verify.parse_retry_after(None) is None
    assert verify.parse_retry_after("whenever") is None


def test_retry_after_http_date_is_relative_to_now():
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    soon = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60))
    got = verify.parse_retry_after(soon)
    assert got is not None and 50 <= got <= 70


def test_gate_penalty_backs_off_a_whole_host():
    gate = verify.HostGate(min_gap=0)
    gate.penalise("example.com", 0.2)
    start = time.monotonic()
    with gate.hold("example.com"):
        pass
    assert time.monotonic() - start >= 0.18


def test_results_stream_as_they_complete(monkeypatch):
    """cmd_verify persists from this callback, so it must fire per result and
    not once at the end - that is what makes an interrupted sweep resumable."""
    monkeypatch.setattr(verify, "check_broker",
                        lambda k, n, u, timeout=15.0, retries=1: verify.Result(k, n, u, "ok"))
    seen = []
    items = [(f"k{i}", f"N{i}", f"https://h{i}.example/o") for i in range(5)]
    out = verify.check_many(items, workers=3, rate=0, host_gap=0,
                            on_result=lambda r: seen.append(r.key))
    assert len(seen) == 5, "callback did not fire per result"
    assert [r.key for r in out] == [f"k{i}" for i in range(5)], "order not preserved"


def test_interrupt_returns_completed_work_not_an_exception(monkeypatch):
    """Ctrl+C mid-sweep must hand back what finished, so the caller can report
    real progress instead of losing it."""
    done = []

    def flaky(k, n, u, timeout=15.0, retries=1):
        if len(done) >= 3:
            raise KeyboardInterrupt
        done.append(k)
        return verify.Result(k, n, u, "ok")

    monkeypatch.setattr(verify, "check_broker", flaky)
    items = [(f"k{i}", f"N{i}", f"https://h{i}.example/o") for i in range(10)]
    out = verify.check_many(items, workers=1, rate=0, host_gap=0)
    assert 0 < len(out) < 10
    assert all(r.status == "ok" for r in out)


def test_skipped_brokers_cost_no_rate_budget(monkeypatch):
    """A catalog entry with no URL should not consume a slot in the rate cap."""
    items = [("k", "N", None)] * 5
    start = time.monotonic()
    out = verify.check_many(items, workers=2, rate=1000, host_gap=5.0)
    assert len(out) == 5 and all(r.status == "skipped" for r in out)
    assert time.monotonic() - start < 0.5, "empty URLs were gated as if they were hosts"


def test_interrupt_stops_queued_work_promptly(monkeypatch):
    """A cancelled sweep must not keep working through its queue. With 500
    brokers queued behind a rate limit, 'wait for it to drain' is minutes of
    requests the user already told us to stop making."""
    started = []

    def slow(k, n, u, timeout=15.0, retries=1):
        started.append(k)
        if len(started) == 2:
            raise KeyboardInterrupt
        time.sleep(0.01)
        return verify.Result(k, n, u, "ok")

    monkeypatch.setattr(verify, "check_broker", slow)
    items = [(f"k{i}", f"N{i}", f"https://h{i}.example/o") for i in range(200)]
    start = time.monotonic()
    out = verify.check_many(items, workers=2, rate=50, host_gap=0)
    elapsed = time.monotonic() - start

    assert len(started) < 20, f"kept working after interrupt: {len(started)} started"
    assert elapsed < 2.0, f"took {elapsed:.1f}s to abandon a cancelled sweep"
    assert len(out) < len(items)
