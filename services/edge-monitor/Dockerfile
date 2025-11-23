FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor/ ./monitor/
RUN mkdir -p /app/logs

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

CMD ["python", "-m", "monitor.main"]

