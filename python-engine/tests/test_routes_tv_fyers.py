"""
Acceptance tests for POST /internal/tv-fyers/signal (routes_tv_fyers.py).

Mirrors test_routes_reconciliation.py's TestClient pattern. This route is
the python-engine side of the two-hop trust boundary: node-gateway has
already validated TradingView's shared secret before forwarding here with
X-Internal-Secret -- so this test exercises THAT gate (the existing
engine_auth._check_internal_secret chokepoint every internal route uses),
not a TradingView-specific one.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from main import app


def _client() -> TestClient:
    return TestClient(app)


def _payload(**overrides) -> dict:
    base = {
        "signal_id": "sig-route-1",
        "symbol": "NIFTY",
        "direction": "CE",
        "underlying_price": 24500.5,
        "strategy": "orb_momentum_v1",
        # Fresh (wall-clock-relative) timestamp -- a fixed past literal
        # would age past TV_FYERS_SIGNAL_MAX_AGE_SEC and be rejected as
        # stale before ever reaching the orchestration path this test
        # exercises.
        "signal_time": datetime.now(timezone.utc).isoformat(),
    }
    base.update(overrides)
    return base


class TestTvFyersSignalRoute:
    def test_accepts_valid_forwarded_signal_with_internal_secret(self):
        client = _client()
        resp = client.post(
            "/internal/tv-fyers/signal",
            json=_payload(),
            headers={"X-Internal-Secret": "test_secret"},  # set by conftest.patch_settings
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["received"] is True
        assert body["signal_id"] == "sig-route-1"
        # `_main.fyers` has no token armed in this test environment (no
        # real Fyers credentials configured), so orchestration fails
        # gracefully rather than crashing the request -- see
        # routes_tv_fyers.py's try/except around handle_tv_entry_signal.
        assert body["executed"] is False
        assert body["reason"] == "orchestration_error"

    def test_rejects_missing_internal_secret(self):
        client = _client()
        resp = client.post("/internal/tv-fyers/signal", json=_payload())
        assert resp.status_code == 403

    def test_rejects_wrong_internal_secret(self):
        client = _client()
        resp = client.post(
            "/internal/tv-fyers/signal",
            json=_payload(),
            headers={"X-Internal-Secret": "not-the-right-secret"},
        )
        assert resp.status_code == 403

    def test_rejects_malformed_payload(self):
        client = _client()
        resp = client.post(
            "/internal/tv-fyers/signal",
            json=_payload(direction="LONG"),
            headers={"X-Internal-Secret": "test_secret"},
        )
        assert resp.status_code == 422


class TestTvFyersTokenRoutes:
    """`main.fyers` is a module-level singleton shared across the whole
    test process. Both reading it back via a fresh `import main as _main`
    AND monkeypatching it that way are fragile under the full suite:
    other test files (test_penny_health.py, test_operator_status.py)
    temporarily swap `sys.modules['main']` for a fake and then `del` it
    in a `finally` block, so a later `import main` anywhere in the
    process re-executes main.py from scratch -- a fresh module object
    with a fresh `fyers` instance, DIFFERENT from the one this test
    file's `app` (bound once at collection time via `from main import
    app`) actually dispatches requests through. Confirmed via
    reproduction with both approaches: the route demonstrably ran
    correctly each time (200, correct JSON, and the right log line all
    fired), but reaching into `main`'s current globals afterward -- or
    monkeypatching them beforehand -- landed on a different object than
    the one in the request's actual call path.

    Fix: assert only on the route's own HTTP-visible contract (status +
    JSON body), which is unaffected by which `main` object the route's
    internal `import main as _main` happens to resolve to. The route
    only ever returns {"armed": True/False} AFTER its internal
    `fyers.set_token(...)` call completes without raising, so the
    response itself is sufficient evidence of correct behavior --
    reaching further into module internals buys no additional
    assurance, only fragility, in this specific test-isolation
    environment."""

    def test_arm_token_requires_internal_secret(self):
        client = _client()
        resp = client.post("/internal/tv-fyers/token", json={"access_token": "tok-1"})
        assert resp.status_code == 403

    def test_arm_token_rejects_missing_body_field(self):
        client = _client()
        resp = client.post(
            "/internal/tv-fyers/token", json={},
            headers={"X-Internal-Secret": "test_secret"},
        )
        assert resp.status_code == 422

    def test_arm_token_succeeds(self):
        client = _client()
        resp = client.post(
            "/internal/tv-fyers/token", json={"access_token": "tok-1"},
            headers={"X-Internal-Secret": "test_secret"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"armed": True}

    def test_invalidate_token_requires_internal_secret(self):
        client = _client()
        resp = client.post("/internal/tv-fyers/token/invalidate")
        assert resp.status_code == 403

    def test_invalidate_token_succeeds(self):
        client = _client()
        resp = client.post(
            "/internal/tv-fyers/token/invalidate",
            headers={"X-Internal-Secret": "test_secret"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"armed": False}
