import pytest

from monitor.self_heal import (
    SelfHealExecutionResult,
    SelfHealExecutor,
    SelfHealManager,
    SelfHealPolicy,
)


def test_policy_threshold_not_met():
    policy = SelfHealPolicy()
    state = {
        "self_heal_enabled": True,
        "consecutive_failures": 1,
        "failure_threshold": 3,
        "max_restart_per_hour": 5,
    }

    decision = policy.evaluate("edge-api", state, now=100)

    assert decision.action == "noop"
    assert decision.reason == "threshold_not_met"
    assert decision.details == {"failures": 1, "threshold": 3}


def test_policy_restart_limit_reached():
    policy = SelfHealPolicy()
    state = {
        "self_heal_enabled": True,
        "consecutive_failures": 5,
        "failure_threshold": 2,
        "max_restart_per_hour": 1,
        "restart_count_hour": 1,
    }

    decision = policy.evaluate("edge-api", state, now=200)

    assert decision.action == "escalate"
    assert decision.reason == "restart_limit_reached"
    assert decision.details["restart_count_hour"] == 1
    assert decision.details["max_restart_per_hour"] == 1


def test_policy_restart_action():
    policy = SelfHealPolicy()
    state = {
        "self_heal_enabled": True,
        "consecutive_failures": 3,
        "failure_threshold": 2,
        "max_restart_per_hour": 5,
        "restart_count_hour": 1,
    }

    decision = policy.evaluate("edge-api", state, now=300)

    assert decision.action == "restart"
    assert decision.reason == "failure_threshold_reached"
    assert decision.details == {"failures": 3, "threshold": 2}


class DummyExecutor(SelfHealExecutor):
    def __init__(self, *, succeed: bool = True):
        super().__init__()
        self.succeed = succeed
        self.calls = []

    async def restart_container(self, container_name, timeout=15):
        self.calls.append((container_name, timeout))
        if self.succeed:
            return SelfHealExecutionResult(success=True)
        return SelfHealExecutionResult(success=False, error="boom")


@pytest.mark.asyncio
async def test_manager_restart_success(monkeypatch):
    executor = DummyExecutor(succeed=True)
    manager = SelfHealManager(executor=executor)

    now = 1000
    monkeypatch.setattr("monitor.self_heal.time.time", lambda: now)

    state = {
        "self_heal_enabled": True,
        "consecutive_failures": 3,
        "failure_threshold": 2,
        "max_restart_per_hour": 5,
        "restart_count_hour": 0,
        "recovery_observe": 3,
        "recovery_duration_sec": 90,
    }
    target = {"container": "edge-api"}

    outcome = await manager.evaluate_and_execute(
        name="edge-api",
        target=target,
        state=state,
        observe_interval_sec=10,
    )

    assert executor.calls == [("edge-api", 15)]
    assert outcome.executed is True
    assert outcome.success is True
    assert outcome.state_updates["restart_count_hour"] == 1
    assert outcome.state_updates["cooldown_until"] == now + 90


@pytest.mark.asyncio
async def test_manager_skips_during_cooldown(monkeypatch):
    executor = DummyExecutor(succeed=True)
    manager = SelfHealManager(executor=executor)

    now = 2000
    monkeypatch.setattr("monitor.self_heal.time.time", lambda: now)

    state = {
        "self_heal_enabled": True,
        "consecutive_failures": 5,
        "failure_threshold": 2,
        "max_restart_per_hour": 5,
        "restart_count_hour": 1,
        "cooldown_until": now + 100,
    }
    target = {"container": "edge-api"}

    outcome = await manager.evaluate_and_execute(
        name="edge-api",
        target=target,
        state=state,
        observe_interval_sec=10,
    )

    assert executor.calls == []
    assert outcome.decision.action == "noop"
    assert outcome.decision.reason == "cooldown_active"


@pytest.mark.asyncio
async def test_default_executor_cannot_mutate_docker():
    result = await SelfHealExecutor().restart_container("edge-api")

    assert result.success is False
    assert result.error == "host_container_mutation_disabled"
