"""
httpx 统一 timeout / retry / circuit-breaker helper (P2-14)

所有 edge 服务的 HTTP 调用应走此模块，避免每个文件各自硬编码 timeout 且无 retry。

用法::

    from common.http_utils import request_with_retry, CircuitBreaker

    # 简单重试
    resp = await request_with_retry("GET", "http://edge-api:8000/health")

    # 带 circuit breaker
    cb = CircuitBreaker(name="cloud", fail_threshold=5, cooldown_sec=30)
    resp = await request_with_retry("POST", url, json=body, circuit_breaker=cb)
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Timeout defaults (seconds) — env-overridable
# ---------------------------------------------------------------------------
DEFAULT_TIMEOUT = float(os.getenv("HTTP_DEFAULT_TIMEOUT", "10"))
SHORT_TIMEOUT = float(os.getenv("HTTP_SHORT_TIMEOUT", "5"))
LONG_TIMEOUT = float(os.getenv("HTTP_LONG_TIMEOUT", "15"))

# ---------------------------------------------------------------------------
# Retry defaults
# ---------------------------------------------------------------------------
DEFAULT_MAX_RETRIES = int(os.getenv("HTTP_MAX_RETRIES", "3"))
DEFAULT_BACKOFF_BASE = float(os.getenv("HTTP_BACKOFF_BASE", "1.0"))
DEFAULT_BACKOFF_MAX = float(os.getenv("HTTP_BACKOFF_MAX", "10.0"))


async def request_with_retry(
    method: str,
    url: str,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
    backoff_max: float = DEFAULT_BACKOFF_MAX,
    timeout: float = DEFAULT_TIMEOUT,
    circuit_breaker: CircuitBreaker | None = None,
    client: httpx.AsyncClient | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """发一次 HTTP 请求，失败时指数退避重试。

    Parameters
    ----------
    method, url, **kwargs : 传给 ``httpx.AsyncClient.request`` 的参数。
    max_retries : 最大重试次数（不含首次请求）。0 = 不重试。
    backoff_base : 退避基数（秒）。
    backoff_max : 退避上限（秒）。
    timeout : 单次请求超时（秒）。
    circuit_breaker : 可选；breaker open 时直接抛 ``CircuitBreakerOpenError``。
    client : 可选复用的 ``httpx.AsyncClient``；不传则内部创建单一 client 并在结束时关闭。

    Returns
    -------
    httpx.Response — 成功响应（不抛 HTTPStatusError）。

    Raises
    ------
    httpx.HTTPStatusError : 重试耗尽后仍失败。
    CircuitBreakerOpenError : breaker 处于 open 状态。
    """
    if circuit_breaker and circuit_breaker.is_open():
        raise CircuitBreakerOpenError(circuit_breaker.name)

    last_exc: Exception | None = None
    own_client = client is None
    active_client = client or httpx.AsyncClient()
    try:
        for attempt in range(1 + max_retries):
            try:
                resp = await active_client.request(method, url, timeout=timeout, **kwargs)
                resp.raise_for_status()
                if circuit_breaker:
                    circuit_breaker.record_success()
                return resp
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                # 4xx 是客户端错误（依赖本身是健康的，正确返回了 4xx）：不重试，
                # 也不计入 circuit breaker——否则一串 400/401/404 会误熔断健康的依赖。
                if 400 <= status < 500:
                    raise
                # 5xx 是服务端故障：可重试并计入 circuit breaker
                last_exc = exc
                if circuit_breaker:
                    circuit_breaker.record_failure()
                if attempt < max_retries:
                    delay = min(backoff_max, backoff_base * (2 ** attempt))
                    logger.warning(
                        "http_retry",
                        method=method,
                        url=url,
                        attempt=attempt + 1,
                        max_retries=max_retries,
                        delay=round(delay, 2),
                        status=status,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if circuit_breaker:
                    circuit_breaker.record_failure()
                if attempt < max_retries:
                    delay = min(backoff_max, backoff_base * (2 ** attempt))
                    logger.warning(
                        "http_retry",
                        method=method,
                        url=url,
                        attempt=attempt + 1,
                        max_retries=max_retries,
                        delay=round(delay, 2),
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
        # 重试耗尽
        raise last_exc  # type: ignore[misc]
    finally:
        if own_client:
            await active_client.aclose()


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

class CircuitBreakerOpenError(Exception):
    """Breaker 处于 open 状态，拒绝请求。"""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"circuit breaker '{name}' is open")


class CircuitBreaker:
    """简易 circuit breaker，与 edge-daq Modbus CB 对齐。

    States: closed (正常) → open (连续失败超阈值，拒绝请求) → half-open (冷却后放行单探针)。
    """

    def __init__(
        self,
        name: str = "default",
        fail_threshold: int = 5,
        cooldown_sec: float = 30.0,
    ) -> None:
        self.name = name
        self.fail_threshold = int(os.getenv(f"CB_{name.upper()}_THRESHOLD", str(fail_threshold)))
        self.cooldown_sec = float(os.getenv(f"CB_{name.upper()}_COOLDOWN", str(cooldown_sec)))
        self._consecutive_failures = 0
        self._open_until = 0.0

    def is_open(self) -> bool:
        if self._consecutive_failures < self.fail_threshold:
            return False
        now = time.monotonic()
        if now < self._open_until:
            return True
        # cooldown 过了 → half-open，允许探针
        return False

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._open_until = 0.0

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.fail_threshold:
            self._open_until = time.monotonic() + self.cooldown_sec
            logger.warning(
                "circuit_breaker_opened",
                name=self.name,
                failures=self._consecutive_failures,
                cooldown_sec=self.cooldown_sec,
            )
