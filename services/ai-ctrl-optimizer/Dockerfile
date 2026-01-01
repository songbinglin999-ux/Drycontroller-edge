FROM python:3.11-slim

WORKDIR /app

COPY services/ai-ctrl-optimizer/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY services/ai-ctrl-optimizer/src/ ./src/

RUN mkdir -p /logs/ai-ctrl-optimizer

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/ready', timeout=5).read()"

CMD ["python", "-m", "src.main"]

