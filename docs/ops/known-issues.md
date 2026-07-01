# Known Issues — Drycontroller-edge

本文件记录 edge 仓的细粒度技术债与已知问题（非上线阻塞项）。
上线阻塞 / 架构级风险记入根 `architecture-audit-2026-05-06.md`，不在此处。

---

## CONTROL-STATUS-LATENCY — `/api/v1/control/status` 在联调高状态切换下响应偏慢

**发现**：2026-07-01

**现象**：edge-api + edge-daq + `fas66mt_fake_plc` + HMI runtime 本地联调时，AUTO 授权、AUTO dispatch、LOCAL_SCREEN/EDGE 权属切换后，`GET /api/v1/control/status?line=L1` 多次耗时约 4-11s；`POST /api/v1/control/auto/dispatch` 曾耗时约 12.6s。edge-daq 同期保持 ready，PLC ACK 最终成功。

**影响**：HMI edge HTTP 超时过短时会出现危险假阴性：HMI 先报 `timed out` / `edge_control_status_unavailable`，但 edge/PLC 随后 ACK 成功。HMI runtime 已将默认 `HMI_EDGE_TIMEOUT_SEC` 调整为 15s 并保留 fail-closed；edge-api 状态接口延迟本身仍需后续优化。

**修法（未排期）**：排查 `control/status` 的实时点查询路径，优先考虑复用内存最新快照或为控制状态查询设置更窄的 tag/timeout，避免 HMI 操作路径被 Influx/实时查询尾延迟拖慢。

**状态**：backlog，未排期。

---

## TEST-IMPORT-COLLISION — ai-softsensor / ai-ctrl-optimizer 同名 `src.main` pytest import 串包

**发现**：2026-06-17

**现象**：手工把多个服务的测试放进**同一个 pytest session** 混跑（自定义顺序一把跑全部服务）时，`ai-softsensor` 与 `ai-ctrl-optimizer` 各自的顶级 `src` 包（同名 `src.main`）会发生 import 串包。根因：pytest 默认 `importmode=prepend` 把每个测试文件的 rootdir 插到 `sys.path[0]` 后按模块名 import，一旦 `sys.modules['src.main']` 被第一个服务占住，第二个服务的 `src.main` 不会被重新加载——于是第二个服务的测试实际 import 了另一个服务的代码。

**影响**：**非上线阻塞**。CI 入口 `scripts/test-all.sh`（`--cov-fail-under=50`）对各服务以**独立 pytest 进程**分别执行，进程级 `sys.modules` 互不污染，不触发该问题。仅在"一条 pytest 命令跨服务混跑"的手工跑法下出现。已由 CI 实跑（Gitea Actions）验证通过。

**修法（择一，未排期）**：
- 跑测试时加 `--import-mode=importlib`（pytest 现代导入模式，不操纵 `sys.path`、不按模块名去重）——最省；
- 或将两个服务的 `src` 改为唯一包名（如 `ai_softsensor` / `ai_ctrl_optimizer`）——更彻底但侵入大。

**状态**：backlog，未排期。
