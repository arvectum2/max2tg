# Build the native lz4/msgpack extensions on Debian; keep GCC out of runtime.
FROM python:3.15.2-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
RUN /opt/venv/bin/python -m pip install --no-cache-dir "pip==26.3"
WORKDIR /build
COPY requirements.lock ./
RUN /opt/venv/bin/python -m pip install --no-cache-dir --progress-bar off -r requirements.lock \
    && /opt/venv/bin/python -m pip check

# Builder and runtime share the same CPython and glibc ABI.
FROM python:3.15.2-slim

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY requirements.lock ./
COPY app/ ./app/
COPY scripts/max2tg-preflight.py ./scripts/max2tg-preflight.py

CMD ["python", "-m", "app.main"]
