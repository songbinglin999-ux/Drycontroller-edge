## 后续待办（TODO）

- **配置与安全**
  - 将当前开发默认密码与 Token 在运维文档中标明“仅限开发环境”，并提供生产环境覆写策略。
  - 评估是否需要为 Mosquitto 提供更细粒度的访问控制（按 topic 分组授权）。

- **监控与健康检查**
  - 为 `edge-daq`、`ai-softsensor`、`ai-ctrl-optimizer`、`edge-monitor` 设计轻量级的 healthcheck（如基于进程心跳或自检接口）。
  - 将 Docker 层 healthcheck 与应用内健康状态打通，纳入统一监控。

- **自动化验证**
  - 增加简单的集成测试脚本（如 `scripts/smoke-test.sh`）自动验证：
    - `/health` 接口。
    - MQTT 发布/订阅闭环。
    - InfluxDB / PostgreSQL 连通性。

- **文档与开发体验**
  - 在 `README.md` / `QUICKSTART.md` 中增加指向 `docs/edge-stack-bootstrap` 的链接，方便新开发者理解启动流程与验收标准。
  - 为各服务补充更细致的配置说明（如何调整扫描周期、AI 模型路径等）。


