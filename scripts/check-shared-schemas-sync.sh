#!/bin/bash
# 校验 cross-repo schemas 在 Drycontroller-edge 和 Drycontroller-cloudserver 是 byte-identical
# (P1-8 Phase D — 方案 c "各仓库各放一份 + CI hash 校验" 的强制执行点)
#
# 源 of truth: Drycontroller-cloudserver/docs/architecture/shared-schemas.md
# 当前清单：cloud_to_edge.py (mqtt_topics.py 是 edge-only,不在此列)
#
# 用法:
#   GITEA_TOKEN=<pat-with-repository-read> bash scripts/check-shared-schemas-sync.sh
#
# 退出码: 0 = 一致, 1 = 不一致 (输出 diff)
#
# 走 Gitea REST API (raw 文件) 而不是 git clone：避开 fake-ip / 全局代理对
# git 协议协商的干扰，且 single GET 比 sparse-clone 快得多。

set -euo pipefail

GITEA_BASE="${GITEA_BASE:-https://git.example.com}"
CLOUD_OPS_OWNER="${CLOUD_OPS_OWNER:-example-org}"
CLOUD_OPS_REPO="${CLOUD_OPS_REPO:-Drycontroller-cloudserver}"
CLOUD_OPS_SCHEMA_PATH="services/cloud-ops/app/common/schemas"
EDGE_SCHEMA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/common/schemas"

# Cross-repo byte-identical files — extend here when adding new shared schema
CROSS_REPO_FILES=(
  cloud_to_edge.py
)

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

REF="${REF:-main}"
auth_header=""
if [ -n "${GITEA_TOKEN:-}" ]; then
  auth_header="Authorization: token $GITEA_TOKEN"
fi

failed=0
for f in "${CROSS_REPO_FILES[@]}"; do
  edge="$EDGE_SCHEMA_DIR/$f"
  cloud_raw_url="${GITEA_BASE}/api/v1/repos/${CLOUD_OPS_OWNER}/${CLOUD_OPS_REPO}/raw/${CLOUD_OPS_SCHEMA_PATH}/${f}?ref=${REF}"
  cloud_local="$TMP_DIR/$f"

  echo "==> fetch $f from cloud-ops via Gitea API (ref=$REF)"
  if [ -n "$auth_header" ]; then
    http_code=$(curl -sS --max-time 30 -H "$auth_header" -o "$cloud_local" -w '%{http_code}' "$cloud_raw_url" || echo "000")
  else
    http_code=$(curl -sS --max-time 30 -o "$cloud_local" -w '%{http_code}' "$cloud_raw_url" || echo "000")
  fi
  if [ "$http_code" != "200" ]; then
    echo "❌ failed to fetch $f (HTTP $http_code) from $cloud_raw_url"
    head -3 "$cloud_local" 2>/dev/null | sed 's/^/    /'
    failed=1
    continue
  fi

  if [ ! -f "$edge" ]; then
    echo "❌ $f missing on edge side ($edge)"
    failed=1
    continue
  fi

  if diff -q "$edge" "$cloud_local" >/dev/null; then
    echo "✅ $f byte-identical"
  else
    echo "❌ $f drifted between edge and cloud-ops:"
    diff "$edge" "$cloud_local" | head -30
    failed=1
  fi
done

echo
if [ $failed -ne 0 ]; then
  echo "❌ Schema parity check FAILED — fix the drift before merging."
  exit 1
fi
echo "✅ All cross-repo schemas in sync."
