# Known Issues — Drycontroller-edge

本文件记录 edge 仓的细粒度技术债与已知问题（非上线阻塞项）。
上线阻塞 / 架构级风险记入根 `architecture-audit-2026-05-06.md`，不在此处。

---

## CONTROL-STATUS-LATENCY — `/api/v1/control/status` 在联调高状态切换下响应偏慢

**发现**：2026-07-01

**现象**：edge-api + edge-daq + `fas66mt_fake_plc` + HMI runtime 本地联调时，AUTO 授权、AUTO dispatch、LOCAL_SCREEN/EDGE 权属切换后，`GET /api/v1/control/status?line=L1` 多次耗时约 4-11s；`POST /api/v1/control/auto/dispatch` 曾耗时约 12.6s。edge-daq 同期保持 ready，PLC ACK 最终成功。

**影响**：HMI edge HTTP 超时过短时会出现危险假阴性：HMI 先报 `timed out` / `edge_control_status_unavailable`，但 edge/PLC 随后 ACK 成功。HMI runtime 已将默认 `HMI_EDGE_TIMEOUT_SEC` 调整为 15s 并保留 fail-closed；edge-api 状态接口延迟本身仍需后续优化。

**修法**：2026-07-01 已处理。`GET /api/v1/control/status` 改为内存快路径，只同步返回产品态、transport、最近 authority snapshot 和最近 command 结果；AUTO 状态下只触发后台轻量 authority gate refresh，不再同步构建完整 process snapshot。`AUTO` 授权改为只查 `CONTROL_STATE_TAGS`，不拉 22 个电流历史窗口；`AUTO dispatch` 仍保留完整诊断快照，但历史窗口查询改为并发执行，避免串行放大尾延迟。

**验证**：`python3 -m py_compile services/edge-api/app/services/control_state.py services/edge-api/app/services/control_core/snapshot.py services/ai-ctrl-optimizer/tests/test_health.py`；`../scripts/agent-test-env.sh pytest services/edge-api/tests/test_control_orchestrator.py -q`；`../scripts/agent-test-env.sh pytest services/edge-api/tests/test_api_endpoints.py -q`；`../scripts/agent-test-env.sh pytest services/edge-api/tests/test_services.py -q`；`../scripts/agent-test-env.sh pytest services/edge-api/tests/test_influx_validation.py -q`。

**状态**：done（edge-api local verification passed）。

---

## TEST-IMPORT-COLLISION — ai-softsensor / ai-ctrl-optimizer 同名 `src.main` pytest import 串包

**发现**：2026-06-17

**现象**：手工把多个服务的测试放进**同一个 pytest session** 混跑（自定义顺序一把跑全部服务）时，`ai-softsensor` 与 `ai-ctrl-optimizer` 各自的顶级 `src` 包（同名 `src.main`）会发生 import 串包。根因：pytest 默认 `importmode=prepend` 把每个测试文件的 rootdir 插到 `sys.path[0]` 后按模块名 import，一旦 `sys.modules['src.main']` 被第一个服务占住，第二个服务的 `src.main` 不会被重新加载——于是第二个服务的测试实际 import 了另一个服务的代码。

**影响**：**非上线阻塞**。CI 入口 `scripts/test-all.sh`（`--cov-fail-under=50`）对各服务以**独立 pytest 进程**分别执行，进程级 `sys.modules` 互不污染，不触发该问题。仅在"一条 pytest 命令跨服务混跑"的手工跑法下出现。已由 CI 实跑（Gitea Actions）验证通过。

**修法**：2026-07-01 已处理。保留两个服务运行时的 `src` 包名和 Docker 入口不变，只把 `ai-ctrl-optimizer` 测试改为通过唯一模块名 `ai_ctrl_optimizer_main` 加载 `src/main.py`，避免测试进程占用共享的 `src.main`。

**验证**：`../scripts/agent-test-env.sh pytest services/ai-softsensor/tests services/ai-ctrl-optimizer/tests -q`。

**状态**：done（local mixed pytest session passed）。
