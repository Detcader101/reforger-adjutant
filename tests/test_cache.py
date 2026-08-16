"""Behaviour spec for cache.TTLCache: TTL expiry, single-flight dedupe, and
'failures are not cached' semantics.

TTL-expiry tests drive a fake `time.monotonic()` via monkeypatch instead
of sleeping, so they're deterministic and instant. Single-flight tests
use a short real `asyncio.sleep` inside the factory so concurrent
callers genuinely interleave rather than running one-after-another.
"""

from __future__ import annotations

import asyncio

import pytest

from adjutant import cache as cache_module
from adjutant.cache import TTLCache


def _fake_clock(monkeypatch, start: float = 1_000.0) -> dict:
    # Patch the `time` name inside cache_module, NOT the stdlib time module:
    # cache_module.time is the shared stdlib module, and freezing
    # time.monotonic globally freezes the asyncio event loop's clock too —
    # any real `await asyncio.sleep()` then never elapses (deadlock).
    state = {"now": start}

    class _FakeTime:
        monotonic = staticmethod(lambda: state["now"])

    monkeypatch.setattr(cache_module, "time", _FakeTime)
    return state


# --------------------------------------------------------------------------- #
# basic caching + TTL                                                         #
# --------------------------------------------------------------------------- #


async def test_first_call_for_a_key_invokes_the_factory():
    cache = TTLCache()
    calls = []

    async def factory():
        calls.append(1)
        return "value"

    result = await cache.get_or_fetch("k", factory)
    assert result == "value"
    assert len(calls) == 1


async def test_second_call_within_ttl_returns_cached_value_without_refetching(monkeypatch):
    _fake_clock(monkeypatch)
    cache = TTLCache()
    calls = []

    async def factory():
        calls.append(1)
        return "value"

    await cache.get_or_fetch("k", factory, ttl_s=60.0)
    result = await cache.get_or_fetch("k", factory, ttl_s=60.0)
    assert result == "value"
    assert len(calls) == 1


async def test_call_after_ttl_has_elapsed_refetches(monkeypatch):
    state = _fake_clock(monkeypatch)
    cache = TTLCache()
    calls = []

    async def factory():
        calls.append(1)
        return "value"

    await cache.get_or_fetch("k", factory, ttl_s=60.0)
    state["now"] += 61.0
    await cache.get_or_fetch("k", factory, ttl_s=60.0)
    assert len(calls) == 2


async def test_force_refresh_bypasses_a_fresh_cache_entry(monkeypatch):
    _fake_clock(monkeypatch)
    cache = TTLCache()
    calls = []

    async def factory():
        calls.append(1)
        return "value"

    await cache.get_or_fetch("k", factory, ttl_s=60.0)
    await cache.get_or_fetch("k", factory, ttl_s=60.0, force_refresh=True)
    assert len(calls) == 2


async def test_different_keys_have_independent_cache_entries():
    cache = TTLCache()
    calls = {"a": 0, "b": 0}

    async def factory_a():
        calls["a"] += 1
        return "a-value"

    async def factory_b():
        calls["b"] += 1
        return "b-value"

    assert await cache.get_or_fetch("a", factory_a) == "a-value"
    assert await cache.get_or_fetch("b", factory_b) == "b-value"
    assert calls == {"a": 1, "b": 1}


async def test_invalidate_forces_the_next_call_to_refetch(monkeypatch):
    _fake_clock(monkeypatch)
    cache = TTLCache()
    calls = []

    async def factory():
        calls.append(1)
        return "value"

    await cache.get_or_fetch("k", factory, ttl_s=60.0)
    cache.invalidate("k")
    await cache.get_or_fetch("k", factory, ttl_s=60.0)
    assert len(calls) == 2


async def test_clear_forces_every_key_to_refetch(monkeypatch):
    _fake_clock(monkeypatch)
    cache = TTLCache()
    calls = []

    async def factory():
        calls.append(1)
        return "value"

    await cache.get_or_fetch("a", factory, ttl_s=60.0)
    await cache.get_or_fetch("b", factory, ttl_s=60.0)
    cache.clear()
    await cache.get_or_fetch("a", factory, ttl_s=60.0)
    assert len(calls) == 3


# --------------------------------------------------------------------------- #
# single-flight dedupe                                                        #
# --------------------------------------------------------------------------- #


async def test_concurrent_calls_for_the_same_key_invoke_the_factory_once():
    cache = TTLCache()
    calls = []

    async def factory():
        calls.append(1)
        await asyncio.sleep(0.05)
        return "value"

    results = await asyncio.gather(
        cache.get_or_fetch("k", factory),
        cache.get_or_fetch("k", factory),
        cache.get_or_fetch("k", factory),
    )
    assert len(calls) == 1
    assert results == ["value", "value", "value"]


async def test_concurrent_callers_all_receive_the_same_exception_on_failure():
    cache = TTLCache()

    async def factory():
        await asyncio.sleep(0.02)
        raise RuntimeError("boom")

    async def call():
        with pytest.raises(RuntimeError, match="boom"):
            await cache.get_or_fetch("k", factory)

    await asyncio.gather(call(), call(), call())


async def test_force_refresh_still_single_flights_concurrent_callers(monkeypatch):
    _fake_clock(monkeypatch)
    cache = TTLCache()
    calls = []

    async def factory():
        calls.append(1)
        await asyncio.sleep(0.02)
        return "fresh"

    await cache.get_or_fetch("k", factory, ttl_s=60.0)  # warm the cache
    calls.clear()

    results = await asyncio.gather(
        cache.get_or_fetch("k", factory, ttl_s=60.0, force_refresh=True),
        cache.get_or_fetch("k", factory, ttl_s=60.0, force_refresh=True),
    )
    assert len(calls) == 1
    assert results == ["fresh", "fresh"]


# --------------------------------------------------------------------------- #
# failures are not cached                                                     #
# --------------------------------------------------------------------------- #


async def test_a_failed_fetch_is_not_cached_and_the_next_call_retries():
    cache = TTLCache()
    calls = []

    async def failing_factory():
        calls.append("fail")
        raise RuntimeError("boom")

    async def succeeding_factory():
        calls.append("ok")
        return "value"

    with pytest.raises(RuntimeError):
        await cache.get_or_fetch("k", failing_factory)

    result = await cache.get_or_fetch("k", succeeding_factory)
    assert result == "value"
    assert calls == ["fail", "ok"]
