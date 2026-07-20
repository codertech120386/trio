# PORTLESS by design — no EXPOSE, no PORT. Fleet must handle a group member that never listens.
FROM python:3.12-alpine

WORKDIR /app
COPY app.py .

ENV SERVICE_NAME=worker \
    TICK_INTERVAL_S=5 \
    WORKER_HOLD_MB=0 \
    PYTHONUNBUFFERED=1

CMD ["python", "app.py"]
