# Per-service Dockerfile on purpose: Fleet's compose path builds each service separately, so a
# single shared image would not exercise it.
FROM python:3.12-alpine

WORKDIR /app
COPY app.py .

# Fleet discovers the app's address from the container's PORT env (containerAddr in cmd/fleetd).
ENV PORT=8080 \
    SERVICE_NAME=web \
    PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "app.py"]
