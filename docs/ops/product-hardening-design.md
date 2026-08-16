# 产品能力补齐设计（非 PLC / 非控制优化）

本文档总入口位于 cloudserver：

```text
Drycontroller-cloudserver/docs/ops/product-hardening-design.md
```

本文件保留在 edge 仓库中，作为边端研发入口。边端需要实现的部分包括：

- `scripts/preflight.sh`。状态：已完成第一版。
- `make preflight`。状态：已完成第一版。
- `edge-api /api/v1/ops/preflight`。状态：已完成第一版（云端远程只读检查）。
- License 激活。状态：仅保留宿主 root 成对事务，已移除 edge-api 单文件写入。
- `edge-api /api/v1/alarms/{alarm_id}/acknowledge`
- root-owned `drycontroller-ota-helper.service` + `0660` UDS（RB-3 已落协调分支）。
- operation-ID-only、串行幂等的固定 OTA 状态机（RB-3 已落协调分支）。
- 完整 SoftwareRelease manifest、强制 cosign、active/candidate/previous 与确定性回滚（RB-4 协调分支已实现，独立 DANGER review 与隔离 E2E 完成前不得部署）。
- `edge-monitor` 保持只读；容器 start/stop/restart 已 fail-closed。
- Influx outbox 本地补写

RB-4 发布边界：

- 云端运维台必须显式选择一个 `completed` SoftwareRelease；job 仅绑定 `release_id + manifest_digest`，Edge 仍只接收 operation ID。
- canonical manifest 固定包含 12 个 Compose 服务及同一 Registry 下的精确 digest，拒绝 tag、局部 override 和自选服务列表。
- CI 可以只构建直接变更的服务，但未变服务只能继承已完成前序版本的精确 digest，并再次扫描和验签；共享/未知变更、前序不可验证或换签名密钥时全量构建。
- Edge 在停止 active 前重新验签 active manifest 并证明全部本地镜像；候选与回滚均使用 `--pull never`，回滚阶段不访问 Registry。
- journal 在副作用前后原子写入并 fsync；中断恢复保守收敛到已证明的 previous。缺少 active/previous、digest、签名、公钥或本地镜像时拒绝升级。

## 设计原则：现场无人值守

边端实现必须按“云端人工决策，边端无人值守执行”设计。

License 和 trust policy 是同一发布单元，只能通过
`manage-license-transaction.py` 的 root-only activate/rollback 流程成对变更。
edge-api 只保留只读校验接口：

```http
GET  /api/v1/ops/license/verify
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
- `--check-ota`：强制检查 root-owned OTA helper UDS。
- `--skip-ota`：跳过 helper 检查，仅适合 RB-4 完成前的非 OTA 工位。

当前检查内容：

- bootstrap sentinel。
- device identity。
- signed license 文件与 license public key。
- `license-service /verify`。
- cosign public key。
- Docker Compose 与核心容器状态。
- `edge-api /ready`、`edge-daq /ready`、`edge-monitor /ready`。
- root-owned OTA helper readiness（按参数或 systemd 运行状态决定）。
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
