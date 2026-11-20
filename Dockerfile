# RANKFLOW AI — production image (Prompt 12, Phase 6)
FROM python:3.12-slim

# FFmpeg + fonts (captions/graphics use DejaVu) + sqlite3 CLI for backups.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg sqlite3 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Never ship a real .env in the image; configuration comes from the
# environment at run time (see docker-compose.yml / docs/DEPLOYMENT.md).
RUN rm -f .env && mkdir -p data/storage data/backups

ENV APP_ENV=production
EXPOSE 8000

# Migrations run first (idempotent), then the API. One worker: the
# orchestrator runs jobs in-process, so a second worker would run
# the same job twice.
CMD ["sh", "-c", "python scripts/migrate_all.py && exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
