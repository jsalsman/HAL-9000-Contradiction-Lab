# Standard CPython plus gthread is appropriate for this network/filesystem-bound service. 
FROM python:3.14.7-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

# Install runtime dependencies separately for effective layer caching.
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && addgroup --system app \
    && adduser --system --ingroup app --home /app app

# The runtime entry point imports the packaged implementation and serves the single page.
COPY flask-app.py index.html ./
COPY hal ./hal
COPY static ./static
RUN chown -R app:app /app
USER app

# Parse every shipped HTML document as Jinja syntax, including documents that do
# not use template expressions, then compile all application modules.
RUN python -c "from pathlib import Path; from jinja2 import Environment; environment = Environment(); [environment.parse(path.read_text(encoding='utf-8')) for path in Path('.').rglob('*.html')]" \
    && python -m compileall . -q

# Start the production server during the image build and smoke-test the Cloud Run
# health endpoint, the single-page document, and the pinned model catalog. These
# need no storage and make no OpenRouter calls.
RUN set -eu; \
    gunicorn --bind 127.0.0.1:8080 --worker-class gthread \
      --workers 1 --threads 2 --timeout 30 flask-app:app & \
    server_pid=$!; \
    trap 'kill "$server_pid" 2>/dev/null || true' EXIT; \
    sleep 2; \
    python -c "import json, urllib.request; base='http://127.0.0.1:8080'; health=json.load(urllib.request.urlopen(base + '/api/healthz', timeout=5)); assert health == {'status': 'ok'}; page=urllib.request.urlopen(base + '/', timeout=5).read().decode(); assert 'Jim Salsman' in page and 'HAL 9000 Contradiction Lab' in page; catalog=json.load(urllib.request.urlopen(base + '/api/catalog', timeout=5)); assert len(catalog['models']) == 20"

EXPOSE 8080
# Deploy with Cloud Run --max-instances 1 and keep WORKERS at 1 (hal/storage.py
# explains the single-writer storage contract). gthread overlaps provider and
# storage waits; each streamed run holds one thread, so THREADS bounds concurrent
# runs. exec preserves signal forwarding.
CMD exec gunicorn --bind "0.0.0.0:${PORT:-8080}" --worker-class gthread --workers "${WORKERS:-1}" --threads "${THREADS:-8}" --timeout 270 --graceful-timeout 30 flask-app:app
