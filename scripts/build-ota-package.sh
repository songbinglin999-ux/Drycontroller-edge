#!/bin/bash
# 生成离线 OTA 包目录（images/*.tar + metadata.yaml + SHA256SUMS）
#
# 用法：
#   bash scripts/build-ota-package.sh <version> <output_dir>
#
# 示例：
#   bash scripts/build-ota-package.sh 1.2.0 /tmp/ota/1.2.0
#
# 说明：
# - 默认打包镜像名：edge/<service>:<version>（与 scripts/build-images.sh 一致）
# - 你可以先执行：bash scripts/build-images.sh <version>
# - 为了回滚可靠，现场应保留“上一版本 OTA 包目录”

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VERSION="${1:-}"
OUT_DIR="${2:-}"

if [ "${VERSION:-}" = "--help" ] || [ "${VERSION:-}" = "-h" ]; then
  echo "用法: $0 <version> <output_dir>"
  echo "示例: $0 1.2.0 /tmp/ota/1.2.0"
  exit 0
fi

if [ -z "$VERSION" ] || [ -z "$OUT_DIR" ]; then
  echo "用法: $0 <version> <output_dir>" >&2
  echo "示例: $0 1.2.0 /tmp/ota/1.2.0" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "缺少 docker 命令" >&2
  exit 1
fi

mkdir -p "$OUT_DIR/images"

SERVICES=(
  "edge-api"
  "edge-daq"
  "ai-softsensor"
  "ai-ctrl-optimizer"
  "license-service"
  "edge-monitor"
)

echo "[INFO] 打包版本: $VERSION"
echo "[INFO] 输出目录: $OUT_DIR"

for svc in "${SERVICES[@]}"; do
  img="edge/$svc:$VERSION"
  tar_path="$OUT_DIR/images/$svc.tar"
  echo "[INFO] docker save $img -> $tar_path"
  docker image inspect "$img" >/dev/null 2>&1 || {
    echo "[ERROR] 镜像不存在：$img（请先 build 或 pull）" >&2
    exit 1
  }
  docker save -o "$tar_path" "$img"
done

# 可选附带 compose/configs（按需拷贝）
if [ -f docker-compose.yml ]; then
  cp docker-compose.yml "$OUT_DIR/docker-compose.yml"
fi
if [ -d configs ]; then
  mkdir -p "$OUT_DIR/configs"
  cp -R configs/* "$OUT_DIR/configs/" || true
fi

COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
DATE="$(date +%Y-%m-%d)"

cat > "$OUT_DIR/metadata.yaml" <<EOF
version: "$VERSION"
release_date: "$DATE"
git_commit: "$COMMIT"
images:
EOF
for svc in "${SERVICES[@]}"; do
  echo "  - name: $svc" >> "$OUT_DIR/metadata.yaml"
  echo "    image: edge/$svc:$VERSION" >> "$OUT_DIR/metadata.yaml"
done

# 生成 SHA256SUMS（校验包内所有文件；跳过自身文件）
echo "[INFO] 生成 SHA256SUMS"
(
  cd "$OUT_DIR"
  rm -f SHA256SUMS
  if command -v sha256sum >/dev/null 2>&1; then
    find . -type f ! -name "SHA256SUMS" -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
  else
    # macOS
    find . -type f ! -name "SHA256SUMS" -print0 | sort -z | xargs -0 shasum -a 256 > SHA256SUMS
  fi
)

echo "[OK] OTA 包目录生成完成：$OUT_DIR"

