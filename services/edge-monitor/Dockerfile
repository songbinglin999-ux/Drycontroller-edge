FROM python:3.11-slim

WORKDIR /app

COPY services/edge-monitor/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY services/edge-monitor/monitor/ ./monitor/

RUN mkdir -p /logs/edge-monitor

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

CMD ["python", "-m", "monitor.main"]

