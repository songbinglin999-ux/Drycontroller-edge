# 边缘机远程运维（宿主机 Tailscale / Headscale）

适用场景：现场边缘机只有家用路由/NAT，**无法做公网入站**，但可以出站访问互联网；需要从云端对边缘机进行运维（探活/重启/拉日志等）。

核心思路：在云端部署 **Headscale**（自建控制面），边缘机跑 **Tailscale** 客户端加入同一 tailnet，云端通过 tailnet IP（例如 `100.x.x.x`）访问边缘机 `edge-api`。

---

## 1) 云端准备（Headscale）

请先按云端文档完成：
- `Drycontroller-cloudserver/docs/ops/cloud-ops.md`

历史 Huawei Cloud Headscale 部署说明已归档到
`Drycontroller-cloudserver/docs/archive/headscale-huawei-cloud.md`。

在云端生成预授权 key（preauth key）：
- `docker exec -it headscale headscale preauthkeys create --user drycontroller --reusable --expiration 720h`

> 你也可以使用 cloud-ops 的可选接口生成（若启用）：`POST /api/v1/headscale/preauthkeys`。

---

## 2) 边缘机启动 Tailscale（宿主机保命层）

Tailscale 运行在宿主机 systemd 上，不再由 `docker compose` 管理。这样即使业务 compose 被 `down` 或 compose 文件损坏，远程通道仍可用于排障。

一次性装机会由 OS Installer USB 复制 `activation-bundle.json`，host tailnet /
activation service 会从中生成或同步 `/etc/drycontroller/tailnet.env` 并启动：

- `drycontroller-tailnet.service`
- `drycontroller-enroll.service`

手工调试时，也准备一个调试用 `/etc/drycontroller/activation-bundle.json`
（`0600`，不要提交到仓库）。host tailnet / activation service 会从 bundle
派生 `/etc/drycontroller/tailnet.env`：

- `tailnet.ts_login_server`：你的 headscale 地址（HTTPS），例如 `https://headscale.example.com`
- `tailnet.ts_authkey`：上一步生成的 preauth key（敏感）
- `tailnet.ts_hostname`：节点名，例如 `edge-001`（建议与云端节点名一致）
- `tailnet.cloud_ops_base_url`：云端 cloud-ops 的地址，例如 `https://cloud-ops.example.com` 或 `http://<server-ip>:8088`
- `factory_license`：签名的 `drycontroller.factory-license.v1` 工厂授权对象

正常 activation 成功后会把 node enroll token 写回派生的 `tailnet.env`。若你绕过
activation、只想手工调试云端 **自注册/心跳 enroll**，再补这些派生 env 字段：
- `CLOUD_OPS_BASE_URL`：云端 cloud-ops 的地址，例如 `https://cloud-ops.example.com` 或 `http://<server-ip>:8088`
- `CLOUD_OPS_ENROLL_TOKEN`：对应云端 cloud-ops 的 `ENROLL_TOKEN`（敏感）
- `EDGE_API_PORT`：边缘 `edge-api` 对外端口（默认 8000）
- `ENROLL_INTERVAL_SEC`：心跳周期（默认 60 秒）

启动：
- `sudo systemctl enable --now drycontroller-tailnet.service`
- `sudo systemctl enable --now drycontroller-enroll.service`

查看状态：
- `systemctl status drycontroller-tailnet.service`
- `journalctl -u drycontroller-tailnet.service -f`
- `systemctl status drycontroller-enroll.service`
- `journalctl -u drycontroller-enroll.service -f`

> 该方案依赖 Linux + systemd + 宿主机 Tailscale。`install-edge-host.sh` 会安装宿主机脚本和 systemd unit；Tailscale CLI 必须在 factory activation 前可用。

---

## 3) 云端验证可达性

当边缘机加入 tailnet 后，会获得一个 tailnet IP（常见为 `100.x.x.x`）。

你可以在云端（同一 tailnet 内的机器）验证：
- `curl http://<tailnet-ip>:8000/ready`

若能返回 200/503（取决于依赖就绪），说明云端已可访问边缘 `edge-api`。

---

## 4) 云端通过 cloud-ops 批量运维

cloud-ops 通过 headscale 虚拟内网访问边缘机 `edge-api`（需要边缘端配置 `ADMIN_API_KEYS`/`CONTROL_API_KEYS`）。

典型流程：
- 边缘机上线自注册（自动）：`drycontroller-enroll.service` 会周期性调用云端 `POST /api/v1/enroll`
- 云端发起批量重启：`POST /api/v1/jobs/restart-service`
- 云端拉取容器日志：`POST /api/v1/jobs/fetch-container-logs`

详见：
- `Drycontroller-cloudserver/docs/ops/cloud-ops.md`
