FROM python:3.11-slim@sha256:ff05d1a05204fb9f7444c435db8e8ec104e587a413280dc9ffc27a4797554182

LABEL org.opencontainers.image.base.name="docker.io/library/python:3.11-slim"
LABEL org.opencontainers.image.base.digest="sha256:ff05d1a05204fb9f7444c435db8e8ec104e587a413280dc9ffc27a4797554182"

WORKDIR /app

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com
ENV PIP_INDEX_URL=${PIP_INDEX_URL}
ENV PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST}
ENV PIP_DEFAULT_TIMEOUT=120
ENV PIP_RETRIES=10

COPY services/license-service/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY services/license-service/license/ ./license/

RUN mkdir -p /logs/license-service

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/ready', timeout=5).read()"

CMD ["uvicorn", "license.main:app", "--host", "0.0.0.0", "--port", "8000"]
