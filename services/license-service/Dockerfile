FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY license/ ./license/
RUN mkdir -p /app/logs

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uvicorn", "license.main:app", "--host", "0.0.0.0", "--port", "8000"]

