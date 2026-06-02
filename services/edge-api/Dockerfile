FROM python:3.11-slim

WORKDIR /app

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com
ENV PIP_INDEX_URL=${PIP_INDEX_URL}
ENV PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST}
ENV PIP_DEFAULT_TIMEOUT=120
ENV PIP_RETRIES=10

# 中国大陆环境：apt 走国内镜像加速（生产部署也在国内）
RUN find /etc/apt -type f \( -name "*.sources" -o -name "*.list" \) \
        -exec sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' {} + \
    && apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY services/edge-api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -U "wheel>=0.46.2" "jaraco.context>=6.1.0"

COPY common/ ./common/
COPY services/edge-api/app/ ./app/
COPY services/edge-api/alembic.ini ./alembic.ini
COPY services/edge-api/alembic/ ./alembic/
COPY services/edge-api/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

RUN mkdir -p /logs/edge-api

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8000/ready', timeout=5).raise_for_status()"

# P2-N8: entrypoint 先 pg_dump 再 alembic upgrade，失败保留备份供恢复
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
