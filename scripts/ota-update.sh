#!/bin/bash
# OTA 升级脚本 - 支持在线和离线升级

set -e

echo "======================================"
echo "  边缘计算系统 OTA 升级"
echo "======================================"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 参数解析
MODE=${1:-"online"}  # online / offline
OTA_PACKAGE=${2:-""}

# 日志函数
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 备份当前版本
backup_current_version() {
    log_info "备份当前版本..."
    BACKUP_DIR="backup/$(date +%Y%m%d_%H%M%S)"
    mkdir -p $BACKUP_DIR
    
    # 备份配置文件
    cp -r configs $BACKUP_DIR/
    cp .env $BACKUP_DIR/
    cp docker-compose.yml $BACKUP_DIR/
    
    # 导出当前运行的镜像列表
    docker-compose config --services > $BACKUP_DIR/services.txt
    
    log_info "备份完成: $BACKUP_DIR"
}

# 在线 OTA 升级
online_update() {
    log_info "执行在线 OTA 升级..."
    
    # 拉取最新镜像
    log_info "拉取最新镜像..."
    docker-compose pull
    
    # 重启服务
    log_info "重启服务..."
    docker-compose up -d
    
    # 清理旧镜像
    log_info "清理旧镜像..."
    docker image prune -f
    
    log_info "在线升级完成"
}

# 离线 OTA 升级
offline_update() {
    if [ -z "$OTA_PACKAGE" ]; then
        log_error "离线升级需要提供 OTA 包路径"
        echo "用法: $0 offline <ota-package-path>"
        exit 1
    fi
    
    if [ ! -f "$OTA_PACKAGE" ]; then
        log_error "OTA 包不存在: $OTA_PACKAGE"
        exit 1
    fi
    
    log_info "执行离线 OTA 升级..."
    log_info "OTA 包: $OTA_PACKAGE"
    
    # 创建临时目录
    TMP_DIR="tmp/ota_$(date +%Y%m%d_%H%M%S)"
    mkdir -p $TMP_DIR
    
    # 解压 OTA 包
    log_info "解压 OTA 包..."
    tar -xzf $OTA_PACKAGE -C $TMP_DIR
    
    # 验证 checksum
    if [ -f "$TMP_DIR/checksum.md5" ]; then
        log_info "验证文件完整性..."
        cd $TMP_DIR
        md5sum -c checksum.md5
        if [ $? -ne 0 ]; then
            log_error "文件完整性验证失败"
            exit 1
        fi
        cd -
        log_info "文件完整性验证通过"
    fi
    
    # 加载镜像
    if [ -f "$TMP_DIR/images.tar" ]; then
        log_info "加载 Docker 镜像..."
        docker load -i $TMP_DIR/images.tar
    fi
    
    # 更新配置文件（如果有）
    if [ -d "$TMP_DIR/configs" ]; then
        log_info "更新配置文件..."
        cp -r $TMP_DIR/configs/* configs/
    fi
    
    # 重启服务
    log_info "重启服务..."
    docker-compose up -d
    
    # 清理临时文件
    log_info "清理临时文件..."
    rm -rf $TMP_DIR
    
    log_info "离线升级完成"
}

# 验证升级结果
verify_update() {
    log_info "验证升级结果..."
    
    # 等待服务启动
    sleep 10
    
    # 检查服务状态
    log_info "检查服务状态..."
    docker-compose ps
    
    # 检查服务健康状态
    UNHEALTHY=$(docker-compose ps | grep -i "unhealthy\|exit" | wc -l)
    
    if [ $UNHEALTHY -gt 0 ]; then
        log_error "发现不健康的服务，升级可能失败"
        log_warn "请检查日志: docker-compose logs"
        return 1
    else
        log_info "所有服务运行正常"
        return 0
    fi
}

# 回滚到备份版本
rollback() {
    log_warn "执行回滚操作..."
    
    if [ ! -d "backup" ]; then
        log_error "没有找到备份目录"
        exit 1
    fi
    
    # 找到最新的备份
    LATEST_BACKUP=$(ls -t backup/ | head -1)
    
    if [ -z "$LATEST_BACKUP" ]; then
        log_error "没有找到备份"
        exit 1
    fi
    
    log_info "回滚到: $LATEST_BACKUP"
    
    # 恢复配置文件
    cp -r backup/$LATEST_BACKUP/configs/* configs/
    cp backup/$LATEST_BACKUP/.env .env
    cp backup/$LATEST_BACKUP/docker-compose.yml docker-compose.yml
    
    # 重启服务
    docker-compose up -d
    
    log_info "回滚完成"
}

# 主流程
main() {
    echo ""
    log_info "升级模式: $MODE"
    echo ""
    
    # 备份当前版本
    backup_current_version
    
    # 根据模式执行升级
    case $MODE in
        online)
            online_update
            ;;
        offline)
            offline_update
            ;;
        rollback)
            rollback
            return
            ;;
        *)
            log_error "未知的升级模式: $MODE"
            echo "支持的模式: online, offline, rollback"
            exit 1
            ;;
    esac
    
    # 验证升级结果
    if verify_update; then
        log_info "✅ OTA 升级成功！"
    else
        log_error "❌ OTA 升级失败"
        log_warn "是否执行回滚? (y/n)"
        read -r response
        if [[ "$response" =~ ^[Yy]$ ]]; then
            rollback
        fi
        exit 1
    fi
}

# 执行主流程
main

echo ""
echo "======================================"
echo "  OTA 升级完成"
echo "======================================"

