FROM python:3.11-slim

WORKDIR /app

# 中国大陆环境：apt 走国内镜像加速（生产部署也在国内）
RUN find /etc/apt -type f \( -name "*.sources" -o -name "*.list" \) \
        -exec sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' {} + \
    && apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY services/edge-api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY services/edge-api/app/ ./app/
COPY services/edge-api/alembic.ini ./alembic.ini
COPY services/edge-api/alembic/ ./alembic/

RUN mkdir -p /logs/edge-api

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8000/ready', timeout=5).raise_for_status()"

# 启动前 alembic 接管 schema 升级；alembic 之后启 uvicorn
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]

