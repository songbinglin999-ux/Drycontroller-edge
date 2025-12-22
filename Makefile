.PHONY: help dev build start stop restart logs clean test validate

help:
	@echo "工业烘干智能控制系统 - 常用命令"
	@echo ""
	@echo "make dev       - 启动开发环境"
	@echo "make build     - 构建 Docker 镜像"
	@echo "make start     - 启动所有服务"
	@echo "make stop      - 停止所有服务"
	@echo "make restart   - 重启所有服务"
	@echo "make logs      - 查看日志"
	@echo "make clean     - 清理容器和数据"
	@echo "make test      - 运行测试"
	@echo "make validate  - 校验 edge-daq 配置（tags.yaml）"
	@echo ""

dev:
	@echo "启动开发环境..."
	bash scripts/dev-start.sh

build:
	@echo "构建 Docker 镜像..."
	bash scripts/build-images.sh

start:
	@echo "启动所有服务..."
	docker-compose up -d

stop:
	@echo "停止所有服务..."
	docker-compose down

restart:
	@echo "重启所有服务..."
	docker-compose restart

logs:
	@echo "查看日志..."
	docker-compose logs -f

clean:
	@echo "清理容器和数据..."
	docker-compose down -v
	rm -rf logs/*/*.log

test:
	@echo "运行测试..."
	pytest

validate:
	@echo "校验 edge-daq 配置..."
	python scripts/validate-daq-config.py --file configs/tags.yaml

