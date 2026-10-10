# Stock Analyzer container image.
# Pinned to Python 3.11 (the supported runtime; see requirements.txt).
FROM python:3.11-slim

# tzdata so --schedule / cron times honour TZ; build tools for any wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    PYTHONPATH=/app/src

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY . .

# Persisted at runtime via volumes (see docker-compose.yml).
RUN mkdir -p data reports database logs

# Non-root.
RUN useradd -m runner && chown -R runner:runner /app
USER runner

ENTRYPOINT ["bash", "scheduling/entrypoint.sh"]
# Default: long-lived in-process scheduler at 22:30 (container TZ). Override the
# time/markets by changing TZ + the CMD, or run one-shot:  docker compose run --rm app once
CMD ["schedule", "22:30"]
