"""`voiceagent.runtime.conversation_persistence` -- hermetic (no database, no
SaaS-OS): `ConversationPersistence`/`_CallWorker` are exercised with a fake
`persist_fn` of the same signature
`voiceagent.conversations.service.persist_conversation_turn()` has, driven
through a real `voiceagent.runtime.db.DatabaseBoundary` (a bounded thread
pool with no database behind it in this file).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from voiceagent.runtime.conversation_persistence import ConversationPersistence, PendingTurn
from voiceagent.runtime.db import DatabaseBoundary
from voiceagent.tenancy import TenantContext


def _context() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), membership_id=uuid.uuid4())


@pytest.fixture
def db():
    boundary = DatabaseBoundary(max_workers=4)
    try:
        yield boundary
    finally:
        boundary.close()


def test_persisted_turns_reach_the_persist_function_in_fifo_order(db) -> None:
    calls: list[str] = []

    def fake_persist(context, call_session_id, *, event_id, role, content, tool_payload):
        calls.append(event_id)

    persistence = ConversationPersistence(db, persist_fn=fake_persist)
    context = _context()
    call_session_id = uuid.uuid4()

    async def scenario() -> None:
        persistence.start(context, call_session_id)
        for i in range(5):
            persistence.enqueue(
                call_session_id, PendingTurn(event_id=f"e{i}", role="user", content="hi")
            )
        await persistence.finish(call_session_id)

    asyncio.run(scenario())

    assert calls == ["e0", "e1", "e2", "e3", "e4"]


def test_tool_call_turn_is_persisted_before_its_tool_result() -> None:
    """Phase 2.5 brief section 5's ordering requirement, at this module's
    own level: two turns enqueued for the same call, in order, are
    persisted in that same order -- a single FIFO queue and consumer is
    what makes this true independent of any database-level sequence
    assignment."""
    db = DatabaseBoundary(max_workers=4)
    calls: list[tuple[str, str]] = []

    def fake_persist(context, call_session_id, *, event_id, role, content, tool_payload):
        calls.append((event_id, role))

    persistence = ConversationPersistence(db, persist_fn=fake_persist)
    context = _context()
    call_session_id = uuid.uuid4()

    async def scenario() -> None:
        persistence.start(context, call_session_id)
        persistence.enqueue(
            call_session_id,
            PendingTurn(event_id="tc-1", role="tool_call", tool_payload={"name": "x"}),
        )
        persistence.enqueue(
            call_session_id,
            PendingTurn(event_id="tc-1", role="tool_result", tool_payload={"value": {}}),
        )
        await persistence.finish(call_session_id)

    try:
        asyncio.run(scenario())
    finally:
        db.close()

    assert calls == [("tc-1", "tool_call"), ("tc-1", "tool_result")]


def test_enqueue_before_start_is_dropped_not_raised(db) -> None:
    persistence = ConversationPersistence(db)
    dropped = persistence.enqueue(
        uuid.uuid4(), PendingTurn(event_id="e1", role="user", content="hi")
    )
    assert dropped is False


def test_queue_full_drops_the_turn_and_never_blocks_the_caller(db) -> None:
    """Brief section 19: never an unbounded queue, and `enqueue()` itself
    must never block -- the audio-adjacent pump's whole reason for calling
    it synchronously."""
    import threading

    release = threading.Event()

    def fake_persist(context, call_session_id, *, event_id, role, content, tool_payload):
        # Runs on DatabaseBoundary's own thread pool -- block that thread
        # (not the event loop) until the test releases it, so the queue
        # backs up behind the one in-flight item.
        release.wait(timeout=5)

    persistence = ConversationPersistence(db, queue_maxsize=2, persist_fn=fake_persist)
    context = _context()
    call_session_id = uuid.uuid4()

    async def scenario() -> tuple[list[bool], int]:
        persistence.start(context, call_session_id)
        results = [
            persistence.enqueue(
                call_session_id, PendingTurn(event_id=f"e{i}", role="user", content="hi")
            )
            for i in range(5)
        ]
        stats = persistence.stats_for(call_session_id)
        assert stats is not None
        dropped_before_release = stats.dropped
        release.set()
        await persistence.finish(call_session_id)
        return results, dropped_before_release

    results, dropped_before_release = asyncio.run(scenario())

    assert False in results  # at least one enqueue was dropped, not blocked
    assert dropped_before_release == results.count(False)
    assert persistence.stats_for(call_session_id) is None  # finish() already removed the worker


def test_a_persistently_failing_write_is_retried_a_bounded_number_of_times(db) -> None:
    attempts: list[str] = []

    def always_fails(context, call_session_id, *, event_id, role, content, tool_payload):
        attempts.append(event_id)
        raise RuntimeError("db unavailable")

    persistence = ConversationPersistence(
        db,
        max_retries=2,
        retry_backoff_seconds=0.01,
        persist_fn=always_fails,
    )
    context = _context()
    call_session_id = uuid.uuid4()

    async def scenario() -> None:
        persistence.start(context, call_session_id)
        persistence.enqueue(call_session_id, PendingTurn(event_id="e1", role="user", content="hi"))
        await persistence.finish(call_session_id)

    asyncio.run(scenario())

    # One initial attempt plus max_retries=2 retries -- never unbounded.
    assert attempts == ["e1", "e1", "e1"]


def test_a_transient_failure_that_later_succeeds_is_not_lost(db) -> None:
    attempts: list[str] = []

    def fails_once_then_succeeds(
        context, call_session_id, *, event_id, role, content, tool_payload
    ):
        attempts.append(event_id)
        if len(attempts) == 1:
            raise RuntimeError("transient")

    persistence = ConversationPersistence(
        db, max_retries=3, retry_backoff_seconds=0.01, persist_fn=fails_once_then_succeeds
    )
    context = _context()
    call_session_id = uuid.uuid4()

    async def scenario() -> None:
        persistence.start(context, call_session_id)
        persistence.enqueue(call_session_id, PendingTurn(event_id="e1", role="user", content="hi"))
        await persistence.finish(call_session_id)

    asyncio.run(scenario())

    assert attempts == ["e1", "e1"]


def test_finish_is_bounded_even_when_the_worker_is_permanently_stuck(db) -> None:
    """Brief section 6: deterministic shutdown -- `finish()` must return
    within its configured drain timeout even if the persist function never
    returns."""

    def stuck(context, call_session_id, *, event_id, role, content, tool_payload):
        import time

        time.sleep(1.5)

    persistence = ConversationPersistence(db, drain_timeout_seconds=0.2, persist_fn=stuck)
    context = _context()
    call_session_id = uuid.uuid4()

    async def scenario() -> float:
        persistence.start(context, call_session_id)
        persistence.enqueue(call_session_id, PendingTurn(event_id="e1", role="user", content="hi"))
        start = asyncio.get_running_loop().time()
        await persistence.finish(call_session_id)
        return asyncio.get_running_loop().time() - start

    elapsed = asyncio.run(scenario())
    assert elapsed < 1.0  # bounded by drain_timeout_seconds, not persist's own 1.5s


def test_finish_for_a_call_never_started_is_a_no_op(db) -> None:
    persistence = ConversationPersistence(db)

    async def scenario() -> None:
        await persistence.finish(uuid.uuid4())

    asyncio.run(scenario())  # must not raise


def test_finish_is_idempotent(db) -> None:
    def fake_persist(context, call_session_id, *, event_id, role, content, tool_payload):
        return None

    persistence = ConversationPersistence(db, persist_fn=fake_persist)
    context = _context()
    call_session_id = uuid.uuid4()

    async def scenario() -> None:
        persistence.start(context, call_session_id)
        persistence.enqueue(call_session_id, PendingTurn(event_id="e1", role="user", content="hi"))
        await persistence.finish(call_session_id)
        await persistence.finish(call_session_id)  # second call: no-op, must not raise

    asyncio.run(scenario())


def test_stats_track_persisted_dropped_and_failed(db) -> None:
    def fake_persist(context, call_session_id, *, event_id, role, content, tool_payload):
        return None

    persistence = ConversationPersistence(db, persist_fn=fake_persist)
    context = _context()
    call_session_id = uuid.uuid4()

    async def scenario() -> None:
        persistence.start(context, call_session_id)
        persistence.enqueue(call_session_id, PendingTurn(event_id="e1", role="user", content="hi"))
        await asyncio.sleep(0.05)
        stats = persistence.stats_for(call_session_id)
        assert stats is not None
        assert stats.persisted == 1
        assert stats.dropped == 0
        assert stats.failed == 0
        await persistence.finish(call_session_id)

    asyncio.run(scenario())


def test_start_is_idempotent_and_does_not_orphan_already_queued_turns(db) -> None:
    calls: list[str] = []

    def fake_persist(context, call_session_id, *, event_id, role, content, tool_payload):
        calls.append(event_id)

    persistence = ConversationPersistence(db, persist_fn=fake_persist)
    context = _context()
    call_session_id = uuid.uuid4()

    async def scenario() -> None:
        persistence.start(context, call_session_id)
        persistence.enqueue(call_session_id, PendingTurn(event_id="e1", role="user", content="hi"))
        persistence.start(context, call_session_id)  # idempotent -- must not replace the worker
        persistence.enqueue(call_session_id, PendingTurn(event_id="e2", role="user", content="hi"))
        await persistence.finish(call_session_id)

    asyncio.run(scenario())

    assert calls == ["e1", "e2"]
