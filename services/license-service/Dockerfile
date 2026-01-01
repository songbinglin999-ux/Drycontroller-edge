FROM python:3.11-slim

WORKDIR /app

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

