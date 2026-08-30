#!/bin/bash
# Docker 镜像构建脚本

set -euo pipefail

echo "======================================"
echo "  构建所有 Docker 镜像"
echo "======================================"

# 设置变量
VERSION=${1:-"latest"}
REGISTRY=${REGISTRY:-""}
receipt_dir="$(mktemp -d "${TMPDIR:-/tmp}/drycontroller-python-base.XXXXXX")"
trap 'rm -rf -- "${receipt_dir}"' EXIT

python3 scripts/ci/python_base_image_contract.py verify-dockerfiles >/dev/null

echo "🏷️  镜像版本: $VERSION"

# 构建镜像列表
SERVICES=(
    "edge-api"
    "edge-daq"
    "edge-monitor"
)

# 构建每个服务的镜像
for service in "${SERVICES[@]}"; do
    echo ""
    echo "🔨 构建 $service..."
    
    docker build \
        --platform linux/amd64 \
        -t edge/$service:$VERSION \
        -t edge/$service:latest \
        -f services/$service/Dockerfile \
        .

    python3 scripts/ci/python_base_image_contract.py verify-image \
        --service "$service" \
        --reference "edge/$service:$VERSION" \
        --output "${receipt_dir}/${service}.json"
    
    if [ $? -eq 0 ]; then
        echo "✅ $service 构建成功"
    else
        echo "❌ $service 构建失败"
        exit 1
    fi
done

echo ""
echo "======================================"
echo "  镜像构建完成"
echo "======================================"

# 显示镜像列表
docker images | grep "edge/"

echo ""
echo "✅ 所有镜像构建完成！"
