# Known Issues — Drycontroller-edge

本文件记录 edge 仓的细粒度技术债与已知问题（非上线阻塞项）。
上线阻塞 / 架构级风险请提交 GitHub Issue，不在此处。

---

## CONTROL-STATUS-LATENCY — `/api/v1/control/status` 在联调高状态切换下响应偏慢

**发现**：2026-07-01

**现象**：edge-api + edge-daq + `fas66mt_fake_plc` + HMI runtime 本地联调时，AUTO 授权、AUTO dispatch、LOCAL_SCREEN/EDGE 权属切换后，`GET /api/v1/control/status?line=L1` 多次耗时约 4-11s；`POST /api/v1/control/auto/dispatch` 曾耗时约 12.6s。edge-daq 同期保持 ready，PLC ACK 最终成功。

**影响**：HMI edge HTTP 超时过短时会出现危险假阴性：HMI 先报 `timed out` / `edge_control_status_unavailable`，但 edge/PLC 随后 ACK 成功。HMI runtime 已将默认 `HMI_EDGE_TIMEOUT_SEC` 调整为 15s 并保留 fail-closed；edge-api 状态接口延迟本身仍需后续优化。

**修法**：2026-07-01 已处理。`GET /api/v1/control/status` 改为内存快路径，只同步返回产品态、transport、最近 authority snapshot 和最近 command 结果；AUTO 状态下只触发后台轻量 authority gate refresh，不再同步构建完整 process snapshot。`AUTO` 授权改为只查 `CONTROL_STATE_TAGS`，不拉 22 个电流历史窗口；`AUTO dispatch` 仍保留完整诊断快照，但历史窗口查询改为并发执行，避免串行放大尾延迟。

**验证**：`python3 -m py_compile services/edge-api/app/services/control_state.py services/edge-api/app/services/control_core/snapshot.py`；`../scripts/agent-test-env.sh pytest services/edge-api/tests/test_control_orchestrator.py -q`；`../scripts/agent-test-env.sh pytest services/edge-api/tests/test_api_endpoints.py -q`；`../scripts/agent-test-env.sh pytest services/edge-api/tests/test_services.py -q`；`../scripts/agent-test-env.sh pytest services/edge-api/tests/test_influx_validation.py -q`。

**状态**：done（edge-api local verification passed）。
