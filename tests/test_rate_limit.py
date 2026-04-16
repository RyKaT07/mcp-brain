"""
Tests for mcp_brain.rate_limit.RateLimiter.

conftest.py stubs get_access_token to return None (god-mode / no limit).
These tests override that stub per-test to exercise the per-token limit path.
"""

import sys
import time
import types
from unittest.mock import patch

import pytest

# conftest.py has already wired in the mcp stubs before this module is imported.


def _make_token(client_id: str = "tok-abc"):
    """Build a minimal AccessToken-like object."""
    tok = types.SimpleNamespace(client_id=client_id, scopes=["*"], token="secret")
    return tok


def _patch_token(tok):
    """Return a context manager that overrides get_access_token inside rate_limit.py."""
    return patch(
        "mcp_brain.rate_limit.get_access_token",
        return_value=tok,
    )


# ---------------------------------------------------------------------------


def test_god_mode_always_passes():
    """No access token → stdio/god-mode → rate limiter never blocks."""
    from mcp_brain.rate_limit import RateLimiter

    rl = RateLimiter("test_tool", interval_seconds=0.01)
    # Even called many times in rapid succession, god-mode should always pass.
    for _ in range(5):
        assert rl.check() is None


def test_first_call_passes():
    """First call for a token is always allowed."""
    from mcp_brain.rate_limit import RateLimiter

    rl = RateLimiter("test_tool", interval_seconds=5.0)
    tok = _make_token("first-caller")
    with _patch_token(tok):
        assert rl.check() is None


def test_second_call_within_interval_blocked():
    """Second call within the interval is blocked with an informative message."""
    from mcp_brain.rate_limit import RateLimiter

    rl = RateLimiter("my_tool", interval_seconds=60.0)
    tok = _make_token("repeat-caller")
    with _patch_token(tok):
        assert rl.check() is None  # first call — allowed
        err = rl.check()           # second call — should be blocked

    assert err is not None
    assert "my_tool" in err
    assert "60 seconds" in err
    assert "Retry in" in err


def test_call_after_interval_passes():
    """Call after the rate limit window has elapsed is allowed."""
    from mcp_brain.rate_limit import RateLimiter

    rl = RateLimiter("test_tool", interval_seconds=0.05)
    tok = _make_token("timer-caller")
    with _patch_token(tok):
        assert rl.check() is None
        time.sleep(0.06)
        assert rl.check() is None


def test_different_tokens_independent():
    """Each token has its own rate limit bucket — one blocked token doesn't affect others."""
    from mcp_brain.rate_limit import RateLimiter

    rl = RateLimiter("test_tool", interval_seconds=60.0)

    tok_a = _make_token("token-A")
    tok_b = _make_token("token-B")

    # Saturate token-A
    with _patch_token(tok_a):
        assert rl.check() is None
        assert rl.check() is not None  # blocked

    # token-B should be clean
    with _patch_token(tok_b):
        assert rl.check() is None


def test_error_message_contains_retry_estimate():
    """Blocked response includes a positive retry time estimate."""
    from mcp_brain.rate_limit import RateLimiter

    rl = RateLimiter("some_tool", interval_seconds=10.0)
    tok = _make_token("check-retry")
    with _patch_token(tok):
        rl.check()  # first — passes
        err = rl.check()  # second — blocked

    assert err is not None
    assert "Retry in" in err
    # Extract the number and verify it's positive
    import re
    match = re.search(r"Retry in (\d+\.\d+)s", err)
    assert match, f"Expected 'Retry in X.Xs' in: {err!r}"
    assert float(match.group(1)) > 0
