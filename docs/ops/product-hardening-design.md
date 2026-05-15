# 产品能力补齐设计（非 PLC / 非控制优化）

本文档总入口位于 cloudserver：

```text
Drycontroller-cloudserver/docs/ops/product-hardening-design.md
```

本文件保留在 edge 仓库中，作为边端研发入口。边端需要实现的部分包括：

- `scripts/preflight.sh`。状态：已完成第一版。
- `make preflight`。状态：已完成第一版。
- `edge-api /api/v1/ops/preflight`。状态：已完成第一版（云端远程只读检查）。
- `edge-api /api/v1/ops/license/install`。状态：已完成第一版。
- `edge-api /api/v1/alarms/{alarm_id}/acknowledge`
- `edge-ota-agent dry_run`
- `edge-ota-agent offline/offline-tar`
- `edge-ota-agent /ready` 深度检查
- `edge-monitor` 容器 start/stop/restart 操作闭环
- Influx outbox 本地补写

## 设计原则：现场无人值守

边端实现必须按“云端人工决策，边端无人值守执行”设计。

License 相关动作的边端职责：

- 接收 cloud-ops 下发的新 signed license 和 public key。
- 做基础 signed license 形状检查与 public key base64 检查；signed license 为扁平 JSON，必须包含 `signature`、`license_id`、`machine_id`、`issued_at`、`expires_at`。
- 备份旧 license 与 public key。
- 原子替换正式文件。
- 调用 `license-service /verify` 并回报结果。
- 验签失败时自动恢复旧 license，不停止生产服务。

已落地接口：

```http
GET  /api/v1/ops/license/verify
POST /api/v1/ops/license/install
```

现场人员默认只负责接电、接网络、接 PLC 线；不应要求现场人员登录机器、复制 license 文件或执行 Docker 命令。

## 已落地：本机 Preflight

入口：

```bash
make preflight
```

直接执行：

```bash
bash scripts/preflight.sh
```

可选参数：

- `--strict`：将 degraded 视为失败，用于最终严格验收。
- `--check-ota`：强制检查 `edge-ota-agent /ready`。
- `--skip-ota`：跳过 OTA agent 检查，适合未启用 OTA profile 的出厂工位。

当前检查内容：

- bootstrap sentinel。
- device identity。
- signed license 文件与 license public key。
- `license-service /verify`。
- cosign public key。
- Docker Compose 与核心容器状态。
- `edge-api /ready`、`edge-daq /ready`、`edge-monitor /ready`。
- OTA agent readiness（按参数或运行状态决定）。
- tailnet 与 cloud enroll 本机配置。

边界：

- 只读本机状态，不写入业务数据。
- 输出 JSON，失败时返回非 0。
- edge-api 远程只读接口、cloud-ops 远程 job、cloud-console 快速创建入口已完成第一版。
- 真 Linux edge 上的 Docker / TUN / OTA / PLC 路径仍需现场 e2e 验证。

不包含：

- PLC 点位与 Modbus 地址映射
- 软测量模型
- 控制优化算法
