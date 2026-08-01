import time

import pytest

from app import circuit_breaker as cb


def test_starts_closed(fake_redis):
    assert cb.get_state("svc") == cb.CLOSED
    cb.before_call("svc")  # should not raise


def test_trips_open_after_threshold_failures(fake_redis, monkeypatch):
    monkeypatch.setattr(cb.settings, "cb_failure_threshold", 3)

    for _ in range(3):
        cb.record_failure("svc")

    assert cb.get_state("svc") == cb.OPEN
    with pytest.raises(cb.CircuitOpenError):
        cb.before_call("svc")


def test_transitions_to_half_open_after_timeout(fake_redis, monkeypatch):
    monkeypatch.setattr(cb.settings, "cb_failure_threshold", 1)
    monkeypatch.setattr(cb.settings, "cb_recovery_timeout_seconds", 5)

    cb.record_failure("svc")
    assert cb.get_state("svc") == cb.OPEN

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 6)

    assert cb.get_state("svc") == cb.HALF_OPEN
    cb.before_call("svc")  # one trial call allowed


def test_success_in_half_open_closes_circuit(fake_redis, monkeypatch):
    monkeypatch.setattr(cb.settings, "cb_failure_threshold", 1)
    monkeypatch.setattr(cb.settings, "cb_recovery_timeout_seconds", 5)

    cb.record_failure("svc")
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 6)
    cb.get_state("svc")  # trigger half-open transition

    cb.record_success("svc")
    assert cb.get_state("svc") == cb.CLOSED


def test_failure_in_half_open_reopens_circuit(fake_redis, monkeypatch):
    monkeypatch.setattr(cb.settings, "cb_failure_threshold", 1)
    monkeypatch.setattr(cb.settings, "cb_recovery_timeout_seconds", 5)

    cb.record_failure("svc")
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 6)
    cb.get_state("svc")  # -> half-open

    cb.record_failure("svc")
    assert cb.get_state("svc") == cb.OPEN
