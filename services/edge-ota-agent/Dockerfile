FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    curl \
    ca-certificates \
    docker.io \
    docker-compose \
    && rm -rf /var/lib/apt/lists/*

# cosign（二进制）用于验签（工业级信任链）
# 现场网络可能无法稳定访问 GitHub Release，因此这里做“尽力下载”：
# - 下载失败不阻塞构建（enforce=true 时，运行期会明确报错）
# - 生产推荐：在 CI 侧构建并推送 edge-ota-agent 镜像，避免现场构建与外网依赖
ARG COSIGN_VERSION=2.4.1
RUN set -e; \
    url="https://github.com/sigstore/cosign/releases/download/v${COSIGN_VERSION}/cosign-linux-amd64"; \
    echo "Installing cosign from ${url} (best-effort)"; \
    (curl -fsSL --connect-timeout 10 --max-time 60 -o /usr/local/bin/cosign "${url}" && chmod +x /usr/local/bin/cosign) \
      || (echo "WARN: cosign download failed; cosign_enforce=true will fail at runtime" && true)

COPY services/edge-ota-agent/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY services/edge-ota-agent/ota_agent/ ./ota_agent/

RUN mkdir -p /logs/edge-ota-agent /data/ota

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8090/ready', timeout=5).read()"

CMD ["python", "-m", "ota_agent.main"]


