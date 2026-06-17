"""
自愈策略与执行器框架。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from docker import errors as docker_errors
import structlog
import os


@dataclass
class SelfHealDecision:
    action: str
    reason: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SelfHealExecutionResult:
    success: bool
    error: Optional[str] = None
    detail: Optional[str] = None


@dataclass
class SelfHealOutcome:
    timestamp: int
    decision: SelfHealDecision
    executed: bool = False
    success: bool = False
    execution: Optional[SelfHealExecutionResult] = None
    state_updates: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "timestamp": self.timestamp,
            "decision": {
                "action": self.decision.action,
                "reason": self.decision.reason,
                "details": self.decision.details,
            },
            "executed": self.executed,
            "success": self.success,
        }
        if self.execution:
            payload["execution"] = {
                "success": self.execution.success,
                "error": self.execution.error,
                "detail": self.execution.detail,
            }
        if self.state_updates:
            payload["state_updates"] = self.state_updates
        return payload


class SelfHealPolicy:
    """
    依据当前服务状态和阈值决定是否需要执行自愈动作。
    """

    def __init__(self):
        self.logger = structlog.get_logger(__name__)

    def evaluate(self, name: str, state: Dict[str, Any], *, now: Optional[int] = None) -> SelfHealDecision:
        now = now or int(time.time())

        if not state.get("self_heal_enabled", True):
            return SelfHealDecision(
                action="noop",
                reason="self_heal_disabled",
                details={"enabled": False},
            )

        cooldown_until = state.get("cooldown_until")
        if cooldown_until and cooldown_until > now:
            return SelfHealDecision(
                action="noop",
                reason="cooldown_active",
                details={"cooldown_until": cooldown_until},
            )

        fail_count = int(state.get("consecutive_failures", 0))
        threshold = max(int(state.get("failure_threshold", 0)), 0)
        if threshold == 0:
            return SelfHealDecision(
                action="noop",
                reason="threshold_not_configured",
                details={"threshold": threshold},
            )
        if fail_count < threshold:
            return SelfHealDecision(
                action="noop",
                reason="threshold_not_met",
                details={"failures": fail_count, "threshold": threshold},
            )

        restart_limit = max(int(state.get("max_restart_per_hour", 0)), 0)
        restart_count = int(state.get("restart_count_hour", 0))
        if restart_limit > 0 and restart_count >= restart_limit:
            return SelfHealDecision(
                action="escalate",
                reason="restart_limit_reached",
                details={
                    "restart_count_hour": restart_count,
                    "max_restart_per_hour": restart_limit,
                },
            )

        return SelfHealDecision(
            action="restart",
            reason="failure_threshold_reached",
            details={"failures": fail_count, "threshold": threshold},
        )


class SelfHealExecutor:
    """
    执行具体的自愈动作（例如 Docker 容器重启）。
    """

    def __init__(self, docker_client_getter: Optional[Callable[[], Any]] = None):
        self._docker_client_getter = docker_client_getter or (lambda: None)
        self.logger = structlog.get_logger(__name__)

    def set_docker_client_getter(self, getter: Optional[Callable[[], Any]]):
        self._docker_client_getter = getter or (lambda: None)

    async def restart_container(self, container_name: Optional[str], timeout: int = 15) -> SelfHealExecutionResult:
        if not container_name:
            return SelfHealExecutionResult(success=False, error="missing_container")

        docker_client = self._docker_client_getter()
        if not docker_client:
            return SelfHealExecutionResult(success=False, error="docker_unavailable")

        loop = asyncio.get_running_loop()

        def _restart():
            container = docker_client.containers.get(container_name)
            container.restart(timeout=timeout)

        try:
            await loop.run_in_executor(None, _restart)
            self.logger.info("self_heal_restart_success", container=container_name)
            return SelfHealExecutionResult(success=True)
        except (docker_errors.DockerException, OSError, RuntimeError) as exc:  # pragma: no cover - 依赖 docker 环境
            self.logger.error("self_heal_restart_failed", container=container_name, error=str(exc))
            return SelfHealExecutionResult(success=False, error=str(exc))


class SelfHealManager:
    """
    集成策略与执行器，负责完成单个服务的自愈流程。
    """

    def __init__(
        self,
        *,
        policy: Optional[SelfHealPolicy] = None,
        executor: Optional[SelfHealExecutor] = None,
    ):
        self.policy = policy or SelfHealPolicy()
        self.executor = executor or SelfHealExecutor()
        self.logger = structlog.get_logger(__name__)

    def set_docker_client_getter(self, getter: Optional[Callable[[], Any]]):
        self.executor.set_docker_client_getter(getter)

    async def evaluate_and_execute(
        self,
        name: str,
        target: Dict[str, Any],
        state: Dict[str, Any],
        *,
        observe_interval_sec: int,
    ) -> SelfHealOutcome:
        now = int(time.time())
        decision = self.policy.evaluate(name, state, now=now)
        outcome = SelfHealOutcome(timestamp=now, decision=decision)

        # 默认仅在 restart / escalate 时记录状态更新
        state_updates: Dict[str, Any] = {}
        if decision.action in ("restart", "escalate"):
            state_updates.update(
                {
                    "last_action": decision.action,
                    "last_action_reason": decision.reason,
                    "last_action_details": decision.details,
                    "last_action_at": now,
                }
            )

        if decision.action != "restart":
            # escalate：防止无限重启风暴，进入更长冷却并清零失败计数，等待下一窗口再尝试
            if decision.action == "escalate":
                try:
                    cooldown_sec = int(os.getenv("SELF_HEAL_ESCALATE_COOLDOWN_SEC", "1800") or "1800")
                except (TypeError, ValueError):
                    cooldown_sec = 1800
                state_updates.update(
                    {
                        "cooldown_until": now + max(0, cooldown_sec),
                        "consecutive_failures": 0,
                    }
                )
            outcome.state_updates = {k: v for k, v in state_updates.items() if v is not None}
            return outcome

        execution = await self.executor.restart_container(target.get("container"))
        outcome.executed = True
        outcome.execution = execution
        outcome.success = execution.success

        if execution.success:
            cooldown_duration = state.get("recovery_duration_sec")
            if not cooldown_duration:
                recovery_observe = int(state.get("recovery_observe", 0))
                cooldown_duration = recovery_observe * observe_interval_sec

            restart_count = int(state.get("restart_count_hour", 0)) + 1
            cooldown_until = now + int(cooldown_duration or 0)
            state_updates.update(
                {
                    "restart_count_hour": restart_count,
                    "cooldown_until": cooldown_until if cooldown_duration else None,
                }
            )
        else:
            state_updates["last_action_error"] = execution.error

        outcome.state_updates = {k: v for k, v in state_updates.items() if v is not None}
        return outcome
