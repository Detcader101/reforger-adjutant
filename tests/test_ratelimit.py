"""Token-bucket rate limiter: pure logic, driven by an injectable clock."""

from adjutant.services.ratelimit import TokenBucketLimiter


def _clock(start=0.0):
    """Returns a callable clock plus a setter, so tests control elapsed time."""
    state = {"now": start}

    def now():
        return state["now"]

    def advance(seconds):
        state["now"] += seconds

    return now, advance


def test_first_call_for_a_key_is_allowed():
    now, _ = _clock()
    limiter = TokenBucketLimiter(capacity=3, refill_seconds=10, time_fn=now)
    assert limiter.allow("user-1") is True


def test_calls_within_capacity_are_allowed():
    now, _ = _clock()
    limiter = TokenBucketLimiter(capacity=3, refill_seconds=10, time_fn=now)
    assert limiter.allow("user-1") is True
    assert limiter.allow("user-1") is True
    assert limiter.allow("user-1") is True


def test_call_exceeding_capacity_is_denied():
    now, _ = _clock()
    limiter = TokenBucketLimiter(capacity=2, refill_seconds=10, time_fn=now)
    assert limiter.allow("user-1") is True
    assert limiter.allow("user-1") is True
    assert limiter.allow("user-1") is False


def test_tokens_refill_after_elapsed_time():
    now, advance = _clock()
    limiter = TokenBucketLimiter(capacity=1, refill_seconds=10, time_fn=now)
    assert limiter.allow("user-1") is True
    assert limiter.allow("user-1") is False
    advance(10)
    assert limiter.allow("user-1") is True


def test_partial_elapsed_time_does_not_grant_a_full_token():
    now, advance = _clock()
    limiter = TokenBucketLimiter(capacity=1, refill_seconds=10, time_fn=now)
    assert limiter.allow("user-1") is True
    advance(5)
    assert limiter.allow("user-1") is False


def test_refill_never_exceeds_capacity():
    now, advance = _clock()
    limiter = TokenBucketLimiter(capacity=2, refill_seconds=10, time_fn=now)
    assert limiter.allow("user-1") is True
    assert limiter.allow("user-1") is True
    advance(1000)  # long idle period
    assert limiter.allow("user-1") is True
    assert limiter.allow("user-1") is True
    assert limiter.allow("user-1") is False


def test_different_keys_have_independent_buckets():
    now, _ = _clock()
    limiter = TokenBucketLimiter(capacity=1, refill_seconds=10, time_fn=now)
    assert limiter.allow("user-1") is True
    assert limiter.allow("user-2") is True
    assert limiter.allow("user-1") is False
