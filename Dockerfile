# Standard CPython plus gthread suits this network-bound service. The Dockerfile is
# the Cloud Build contract: build, import, and smoke-test the served page here.
FROM python:3.14.7-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EXPERIMENTS_DIR=/experiments
WORKDIR /app

# Runtime dependencies first for layer caching.
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && addgroup --system app \
    && adduser --system --ingroup app --home /app app \
    && mkdir -p /experiments && chown app:app /experiments

COPY flask-app.py index.html ./
COPY hal ./hal
COPY static ./static
RUN chown -R app:app /app
USER app
RUN python -m compileall -q .

# Start the production server during the build and smoke-test health and the page.
RUN set -eu; \
    EXPERIMENTS_DIR=/tmp/smoke gunicorn --bind 127.0.0.1:8080 --worker-class gthread \
      --workers 1 --threads 2 --timeout 30 flask-app:app & \
    server_pid=$!; \
    trap 'kill "$server_pid" 2>/dev/null || true' EXIT; \
    sleep 3; \
    python -c "import json, urllib.request; b='http://127.0.0.1:8080'; assert json.load(urllib.request.urlopen(b+'/api/healthz', timeout=5)) == {'status': 'ok'}; page = urllib.request.urlopen(b+'/', timeout=5).read().decode(); assert 'HAL 9000 Contradiction Lab' in page; cat = json.load(urllib.request.urlopen(b+'/api/catalog', timeout=5)); assert len(cat['models']) == 20"

EXPOSE 8080
# One worker with many threads: each streamed run holds one thread for its duration.
# --timeout 0 disables the gthread worker heartbeat kill during long streams.
CMD exec gunicorn --bind "0.0.0.0:${PORT:-8080}" --worker-class gthread --workers 1 \
    --threads "${THREADS:-16}" --timeout 0 --graceful-timeout 30 flask-app:app
