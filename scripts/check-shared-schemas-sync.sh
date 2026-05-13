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

set -euo pipefail

CLOUD_OPS_REPO_URL="${CLOUD_OPS_REPO_URL:-https://git.example.com/example-org/Drycontroller-cloudserver.git}"
CLOUD_OPS_SCHEMA_PATH="services/cloud-ops/app/common/schemas"
EDGE_SCHEMA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/common/schemas"

# Cross-repo byte-identical files — extend here when adding new shared schema
CROSS_REPO_FILES=(
  cloud_to_edge.py
)

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

url="$CLOUD_OPS_REPO_URL"
if [ -n "${GITEA_TOKEN:-}" ]; then
  # Gitea PAT 走 https://oauth2:<token>@host/... 形式
  url="${CLOUD_OPS_REPO_URL/https:\/\//https:\/\/oauth2:${GITEA_TOKEN}@}"
fi

echo "==> sparse-clone cloud-ops schemas only ..."
git clone --depth 1 --filter=blob:none --no-checkout "$url" "$TMP_DIR/cloud-ops" >/dev/null
(
  cd "$TMP_DIR/cloud-ops"
  git sparse-checkout init --cone >/dev/null
  git sparse-checkout set "$CLOUD_OPS_SCHEMA_PATH" >/dev/null
  git checkout >/dev/null
)

failed=0
for f in "${CROSS_REPO_FILES[@]}"; do
  edge="$EDGE_SCHEMA_DIR/$f"
  cloud="$TMP_DIR/cloud-ops/$CLOUD_OPS_SCHEMA_PATH/$f"
  if [ ! -f "$edge" ]; then
    echo "❌ $f missing on edge side ($edge)"
    failed=1
    continue
  fi
  if [ ! -f "$cloud" ]; then
    echo "❌ $f missing on cloud-ops side ($cloud)"
    failed=1
    continue
  fi
  if diff -q "$edge" "$cloud" >/dev/null; then
    echo "✅ $f byte-identical"
  else
    echo "❌ $f drifted between edge and cloud-ops:"
    diff "$edge" "$cloud" | head -30
    failed=1
  fi
done

echo
if [ $failed -ne 0 ]; then
  echo "❌ Schema parity check FAILED — fix the drift before merging."
  exit 1
fi
echo "✅ All cross-repo schemas in sync."
