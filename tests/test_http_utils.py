"""Tests for common.http_utils (P2-14)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from common.http_utils import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    request_with_retry,
)


def _make_response(status: int = 200, json_body: dict | None = None) -> httpx.Response:
    """Build a Response that has a request attached (required by raise_for_status)."""
    req = httpx.Request("GET", "http://test/")
    return httpx.Response(status, json=json_body or {}, request=req)


# ---------------------------------------------------------------------------
# request_with_retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_with_retry_success_first_try():
    mock_resp = _make_response(200)
    with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp) as m:
        resp = await request_with_retry("GET", "http://test/health", max_retries=3)
        assert resp.status_code == 200
        assert m.call_count == 1


@pytest.mark.asyncio
async def test_request_with_retry_retries_on_failure():
    fail_resp = _make_response(500)
    ok_resp = _make_response(200)

    call_count = 0

    async def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return fail_resp
        return ok_resp

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock, side_effect=side_effect):
        resp = await request_with_retry(
            "GET", "http://test/health", max_retries=3, backoff_base=0.01
        )
        assert resp.status_code == 200
        assert call_count == 3


@pytest.mark.asyncio
async def test_request_with_retry_exhausted_raises():
    fail_resp = _make_response(502)
    with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=fail_resp):
        with pytest.raises(httpx.HTTPStatusError):
            await request_with_retry(
                "GET", "http://test/health", max_retries=2, backoff_base=0.01
            )


@pytest.mark.asyncio
async def test_request_with_retry_no_retry_on_4xx():
    """4xx 是永久错误，不重试，立即抛。"""
    fail_resp = _make_response(404)
    call_count = 0

    async def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return fail_resp

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock, side_effect=side_effect):
        with pytest.raises(httpx.HTTPStatusError):
            await request_with_retry(
                "GET", "http://test/health", max_retries=3, backoff_base=0.01
            )
    assert call_count == 1


@pytest.mark.asyncio
async def test_request_with_retry_transport_error():
    call_count = 0

    async def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise httpx.ConnectError("connection refused")
        return _make_response(200)

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock, side_effect=side_effect):
        resp = await request_with_retry(
            "GET", "http://test/health", max_retries=3, backoff_base=0.01
        )
        assert resp.status_code == 200
        assert call_count == 2


@pytest.mark.asyncio
async def test_request_with_retry_circuit_breaker_open():
    cb = CircuitBreaker(name="test", fail_threshold=1, cooldown_sec=60)
    cb.record_failure()

    with pytest.raises(CircuitBreakerOpenError):
        await request_with_retry("GET", "http://test/", circuit_breaker=cb)


@pytest.mark.asyncio
async def test_request_with_retry_4xx_does_not_trip_breaker():
    """4xx 是客户端错误，依赖本身健康，不应计入 circuit breaker（防误熔断）。"""
    cb = CircuitBreaker(name="t4xx", fail_threshold=2, cooldown_sec=60)
    fail_resp = _make_response(404)
    with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=fail_resp):
        for _ in range(5):
            with pytest.raises(httpx.HTTPStatusError):
                await request_with_retry(
                    "GET", "http://test/", circuit_breaker=cb, max_retries=0
                )
    assert cb._consecutive_failures == 0
    assert not cb.is_open()


@pytest.mark.asyncio
async def test_request_with_retry_closes_breaker_on_success():
    cb = CircuitBreaker(name="test", fail_threshold=5, cooldown_sec=1)
    cb.record_failure()
    cb.record_failure()
    assert cb._consecutive_failures == 2

    with patch(
        "httpx.AsyncClient.request",
        new_callable=AsyncMock,
        return_value=_make_response(200),
    ):
        await request_with_retry("GET", "http://test/", circuit_breaker=cb)
    assert cb._consecutive_failures == 0


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


def test_circuit_breaker_stays_closed_below_threshold():
    cb = CircuitBreaker(name="t", fail_threshold=3, cooldown_sec=1)
    cb.record_failure()
    cb.record_failure()
    assert not cb.is_open()


def test_circuit_breaker_opens_at_threshold():
    cb = CircuitBreaker(name="t", fail_threshold=3, cooldown_sec=60)
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open()


def test_circuit_breaker_half_open_after_cooldown():
    cb = CircuitBreaker(name="t", fail_threshold=2, cooldown_sec=0.01)
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open()
    time.sleep(0.02)
    assert not cb.is_open()


def test_circuit_breaker_success_resets():
    cb = CircuitBreaker(name="t", fail_threshold=2, cooldown_sec=60)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    assert not cb.is_open()
    assert cb._consecutive_failures == 0


def test_circuit_breaker_env_override():
    with patch.dict("os.environ", {"CB_MY_SVC_THRESHOLD": "2", "CB_MY_SVC_COOLDOWN": "5"}):
        cb = CircuitBreaker(name="my_svc", fail_threshold=10, cooldown_sec=100)
        assert cb.fail_threshold == 2
        assert cb.cooldown_sec == 5.0
