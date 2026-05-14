# Edge Maintenance Console (Svelte)

本地维护控制台，仅面向维护人员/工程师。

现场人员使用的大屏只显示只读状态摘要；本控制台保留重启服务、查看日志、调整自愈参数等维护能力，必须通过 edge-api 的管理员 API Key 访问高危接口。

## 开发

```bash
cd ops-console
npm install
npm run dev
```

- 默认 Vite Dev Server 运行在 <http://localhost:4173>。
- `.env` 中可配置：
  - `VITE_API_BASE`：指向 edge-api，例如 `http://localhost:8000`。
  - `VITE_WS_BASE`：自定义 WebSocket 根地址，未配置时自动从 `VITE_API_BASE` 或浏览器地址推导。

## 构建

```bash
npm run build
# 产物位于 ops-console/dist，可由任意静态服务器托管
```

## 边界

- 不做多节点、租户、license 生命周期、OTA 策略、云端 job 管理；这些属于 cloud-console。
- 不作为现场人员主界面；现场只读状态并入大屏。
- 维护动作必须携带 `X-API-Key`。

## 功能概览

- 服务列表：展示每个本机服务的健康状态、自愈统计。
- 维护动作：重启服务、暂停/恢复自愈、调整自愈参数。
- 日志查看：读取本机容器日志。
- 告警查看：读取本机 Alertmanager 聚合告警。
- 自愈事件流：订阅 `/api/ws/system` WebSocket，实时展示 `edge-monitor` 推送的事件。
