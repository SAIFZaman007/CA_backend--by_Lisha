#!/bin/sh

set -e

echo "Applying database migrations..."
alembic upgrade head

WORKERS="${WEB_CONCURRENCY:-2}"
echo "Starting Coach Auto API with ${WORKERS} worker(s)..."
exec gunicorn app.main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers "${WORKERS}" \
    --bind 0.0.0.0:8000 \
    --timeout 300 \
    --graceful-timeout 30 \
    --keep-alive 5 \
    --access-logfile - \
    --error-logfile -