# Single Debian-based runtime avoids copying glibc wheels into musl Alpine.
# Candidate image only: current macOS launchd runtime remains untouched.
FROM python:3.15.2-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2

WORKDIR /app

COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --progress-bar off -r requirements.lock \
    && python -m pip check

COPY app/ ./app/
COPY scripts/max2tg-preflight.py ./scripts/max2tg-preflight.py

CMD ["python", "-m", "app.main"]
