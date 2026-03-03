"""Tests for the Watchdog Loop Detector."""

from uuid import uuid4

import pytest

from src.watchdog.loop_detector import LoopDetectedError, LoopDetector, LoopSignal


@pytest.fixture()
def detector() -> LoopDetector:
    return LoopDetector()


def test_create_context(detector: LoopDetector) -> None:
    sid = uuid4()
    ctx = detector.create_context(sid, agent_type="finance")
    assert ctx.session_id == sid
    assert ctx.agent_type == "finance"
    assert not ctx.loop_detected


def test_record_step_with_progress(detector: LoopDetector) -> None:
    sid = uuid4()
    detector.create_context(sid, agent_type="general")
    ctx = detector.record_step(sid, token_delta=100, signal=LoopSignal.PROGRESS)
    assert len(ctx.steps) == 1
    assert ctx.total_tokens == 100


def test_no_loop_within_step_limit(detector: LoopDetector) -> None:
    sid = uuid4()
    detector.create_context(sid, agent_type="general")
    for _ in range(5):
        detector.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)
    ctx = detector.get_context(sid)
    assert ctx is not None
    assert not ctx.loop_detected


def test_loop_detected_after_max_steps(detector: LoopDetector) -> None:
    sid = uuid4()
    detector.create_context(sid, agent_type="general")
    with pytest.raises(LoopDetectedError):
        for _ in range(15):  # exceeds default max_agent_steps=10
            detector.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)


def test_progress_signal_resets_streak(detector: LoopDetector) -> None:
    sid = uuid4()
    detector.create_context(sid, agent_type="general")
    for _ in range(9):
        detector.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)
    # A progress signal should reset the streak
    detector.record_step(sid, token_delta=10, signal=LoopSignal.PROGRESS)
    # Now we can go another 9 steps without triggering
    for _ in range(9):
        detector.record_step(sid, token_delta=10, signal=LoopSignal.NO_PROGRESS)
    ctx = detector.get_context(sid)
    assert ctx is not None
    assert not ctx.loop_detected


def test_token_velocity_exceeded_raises(detector: LoopDetector) -> None:
    sid = uuid4()
    detector.create_context(sid, agent_type="it")
    with pytest.raises(LoopDetectedError):
        detector.record_step(sid, token_delta=100_000, signal=LoopSignal.PROGRESS)


def test_get_context_returns_none_for_unknown(detector: LoopDetector) -> None:
    assert detector.get_context(uuid4()) is None


def test_record_step_unknown_session_raises(detector: LoopDetector) -> None:
    with pytest.raises(KeyError):
        detector.record_step(uuid4(), token_delta=10)
