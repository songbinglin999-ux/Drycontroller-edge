# Ops Console (Svelte)

运维控制台，实时查看 `edge-monitor` 上报的系统状态 / 指标 / 自愈事件。

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

## 功能概览

- 仪表卡：系统状态、CPU/内存/磁盘占用。
- 服务列表：展示每个服务的健康状态、自愈统计。
- 自愈事件流：订阅 `/api/ws/system` WebSocket，实时展示 `edge-monitor` 推送的事件。

