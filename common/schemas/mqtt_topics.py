"""
MQTT topic 单一来源 (single source of truth).

P1-8 Phase C: 当前所有 service 都通过 `os.getenv("MQTT_TOPIC_*", "<default>")`
读取 topic，但 default 值散落在各 service 代码里（同一个 topic 在多处各写一份，
改一处忘另一处的风险）。此文件集中所有 topic 的 default value 与命名约定。

**设计约定**：
- 所有 topic 含 `{line_id}` 占位符——按产线隔离（一个 broker 多产线场景）。
  调用方按 `TOPIC.format(line_id=...)` 渲染。例外见 GLOBAL_* 段。
- topic segment 层次：`<domain>/<line_id>/<entity>[/<verb>]`
  - domain ∈ {realtime, control, system, events}
  - entity 由 domain 决定（tags / alarms / status / acks ...）
- env var 名约定：`MQTT_TOPIC_<UPPER_SNAKE>`，default 字面量以本文件常量为准。

**不在此文件**：实际的 publish/subscribe 调用、MQTT client 配置、ACL 规则。

cloud-ops 不消费 MQTT，因此配套云端服务不镜像本文件。
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# Per-line topics (use .format(line_id=...) at call site)
# --------------------------------------------------------------------------

#: edge-daq publishes PLC sampled tags here; edge-api subscribes.
#: env var: MQTT_TOPIC_REALTIME (edge-daq publisher.py)
REALTIME_TAGS_TPL = "realtime/{line_id}/tags"

#: edge-monitor publishes per-line service health snapshot; edge-api+cloud subscribe.
#: env var: MQTT_TOPIC_STATUS (edge-monitor monitor/main.py)
SYSTEM_STATUS_TPL = "system/{line_id}/status"

#: edge-daq publishes alarms (out-of-range sensor / device error) here.
#: env var: MQTT_TOPIC_ALARM (edge-daq publisher.py)
SYSTEM_ALARMS_TPL = "system/{line_id}/alarms"

#: edge-api publishes control commands here; edge-daq subscribes and writes PLC.
#: env var: MQTT_TOPIC_CONTROL_TPL (edge-api control_command.py)
CONTROL_TAGS_TPL = "control/{line_id}/tags"

#: edge-daq publishes control ACKs (succ/fail + reason) here; edge-api subscribes.
#: env vars: CONTROL_ACK_TOPIC (edge-daq) / CONTROL_ACK_TOPIC_TPL (edge-api).
CONTROL_ACKS_TPL = "control/{line_id}/acks"


# --------------------------------------------------------------------------
# Global topics (no line_id segment)
# --------------------------------------------------------------------------

#: edge-monitor publishes self-heal events (restart/disable decisions) here.
#: env var: MQTT_TOPIC_SELF_HEAL (edge-monitor)
GLOBAL_SELF_HEAL = "events/system/self_heal"

#: edge-monitor's bootstrap/discovery topic (line-agnostic since it announces
#: which lines a node serves).
#: env var: DISCOVERY_MQTT_TOPIC (edge-monitor discovery.py)
GLOBAL_BOOTSTRAP = "system/bootstrap"


# --------------------------------------------------------------------------
# Wildcard subscriptions (edge-api aggregates everything)
# --------------------------------------------------------------------------

#: edge-api subscribes to all real-time data across lines.
WILDCARD_REALTIME = "realtime/#"

#: edge-api subscribes to all system events (status / alarms / etc.).
WILDCARD_SYSTEM = "system/#"
