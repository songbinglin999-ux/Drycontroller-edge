FROM python:3.11-slim

WORKDIR /app

# 中国大陆环境：apt 走国内镜像加速（生产部署也在国内）
RUN find /etc/apt -type f \( -name "*.sources" -o -name "*.list" \) \
        -exec sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' {} + \
    && apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY services/ai-softsensor/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY services/ai-softsensor/src/ ./src/

RUN mkdir -p /logs/ai-softsensor /app/models

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/ready', timeout=5).read()"

CMD ["python", "-m", "src.main"]

