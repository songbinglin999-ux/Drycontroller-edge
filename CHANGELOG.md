# 更新日志

本文档记录项目的所有重要变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
并且本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 安全
- `docker-compose.yml`：`postgres`(5432) / `influxdb`(8086) / `prometheus`(9090) / `alertmanager`(9093) 端口加 `127.0.0.1:` 前缀，不再对现场局域网（含 PLC 网段）暴露。原 `prometheus` / `alertmanager` 的注释声明"仅绑定本机回环"但配置不一致，本次修齐。详见顶层架构审计报告 P0-2 / P0-3（2026-05-06）。
  - **部署提醒**：之前若有从其他机器直连 5432/8086 的工作流（如远程 DB 排障），需改用 SSH 隧道或 `docker exec` 进容器；如确需对外暴露，请在生产 override 文件单独打开。
- `docker-compose.yml`：添加顶层 `x-logging` 锚点（`driver: json-file`，`max-size: 100m`，`max-file: 5`），15 个长期运行 service 引用 `logging: *default-logging`，防止 docker 默认无上限日志撑爆 IPC 磁盘。详见 P0-1。

### 变更
- `docker-compose.yml`：给 7 个应用层 service（`edge-api` / `edge-daq` / `ai-softsensor` / `ai-ctrl-optimizer` / `license-service` / `edge-monitor` / `edge-ota-agent`）加 docker `healthcheck`，用 `python -c "import urllib.request..."` 探活 `/ready`（零依赖，不需镜像安装 curl）。AI 服务 `start_period=60s` 留出模型加载时间，其余服务 `40s`。让 docker engine 能识别"应用 hang"而不只是"进程退出"。详见顶层归档 `docs/workspace/archive/p1-roadmap-2026-05-07.md` P1-7（2026-05-07）。
  - **生产实测建议**：`docker compose up -d` 后等 90s，跑 `docker ps --format "table {{.Names}}\t{{.Status}}"` 查看每个容器是否 `(healthy)`；如 ai-softsensor / ai-ctrl-optimizer 因模型大于 60s 启动，调大 `start_period`。

### 新增
- 初始项目架构
- Edge API 服务
- 数据采集服务 (edge-daq)
- AI 软测量服务 (ai-softsensor)
- AI 控制优化服务 (ai-ctrl-optimizer)
- 授权服务 (license-service)
- 系统监控服务 (edge-monitor)
- Docker Compose 部署配置
- 完整的配置管理系统
- 数据库初始化脚本
- OTA 升级脚本

### 修复
- edge-ota-agent 在只读根目录环境下导入即创建目录导致启动失败的问题（回退到 /tmp）

### 文档
- 完整的 README 文档
- 系统架构文档
- 部署指南
- API 文档

## [1.0.0] - 2025-01-XX

### 新增
- 首次正式发布
- 核心功能完整实现
- 生产环境可用

---

## 版本说明

### 版本号格式

版本号格式：`主版本号.次版本号.修订号`

- **主版本号**：不兼容的 API 修改
- **次版本号**：向下兼容的功能性新增
- **修订号**：向下兼容的问题修正

### 变更类型

- **新增** (Added)：新功能
- **变更** (Changed)：现有功能的变更
- **弃用** (Deprecated)：即将移除的功能
- **移除** (Removed)：已移除的功能
- **修复** (Fixed)：Bug 修复
- **安全** (Security)：安全性相关的修复
