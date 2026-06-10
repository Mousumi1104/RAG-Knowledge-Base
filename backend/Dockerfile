# syntax=docker/dockerfile:1
# -----------------------------------------------------------------------------
# RAG Knowledge Base — backend (FastAPI + Alembic + pgvector client)
#
# The local hash embedder (EMBEDDING_PROVIDER=local) needs NO model download,
# so this image stays small and builds without network access to a model hub.
# psycopg[binary] and pgvector ship wheels, so no Postgres client libs are
# required at build time; build-essential is included only as a safety net for
# any sdist that needs compiling.
# -----------------------------------------------------------------------------
FROM python:3.12-slim

# Keep Python lean and unbuffered for clean container logs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Build dependencies (removed after pip install to keep the image small).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching.
COPY requirements.txt ./
RUN pip install -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential

# Application code + migration assets (alembic upgrade head runs on boot).
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# Uploaded files live here; mounted as a named volume in compose.
RUN mkdir -p /app/uploads

EXPOSE 8000

# entrypoint.sh runs `alembic upgrade head` then `uvicorn app.main:app`.
ENTRYPOINT ["./entrypoint.sh"]
