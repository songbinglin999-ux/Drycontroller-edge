# P2-10: compose 改动规约

## 背景

2026-05-30 边缘机离线事故（见 `architecture-audit-2026-05-06.md` P2-N10）：
手动 `sed` 给 `docker-compose.yml` 注入含 YAML anchor 的 ulimits 块时，`&` 转义陷阱
导致 YAML 语法错误，`down` 后 `up` 失败，远程通道断联 >21h。

## 规约（HARD RULES）

1. **compose 改动一律走仓库 commit → OTA `cp` 部署**。禁止在 edge 机器上手动 `sed` / `vi` 改 `docker-compose.yml`。

2. **需要脚本化注入 YAML 时，使用 `yq` 而非 `sed`**。`yq` 正确处理 anchor/alias/引号/多行字符串。

3. **紧急修复（无法 OTA 时）**：先 `cp docker-compose.yml docker-compose.yml.bak`，用 `yq` 修改，验证 `docker compose config` 通过后再 `down/up`。

## 安装 yq（edge 宿主机）

```bash
# amd64
sudo curl -L https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -o /usr/local/bin/yq
sudo chmod +x /usr/local/bin/yq
```

## 用 yq 注入 ulimits 示例

```bash
# 给所有 Python 服务加 nofile ulimit
yq -i '.services[].ulimits.nofile.soft = 65536 | .services[].ulimits.nofile.hard = 65536' docker-compose.yml

# 或给特定服务加
yq -i '.services["edge-api"].ulimits.nofile.soft = 65536' docker-compose.yml
```

## 验证

```bash
# 改动后必须验证 YAML 语法
docker compose config --quiet

# 验证服务能正常启动
docker compose up -d --dry-run  # (compose v2.20+)
