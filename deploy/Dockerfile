# One image for all three tiers: API + Temporal server + worker.
# Render: native Docker deploy. Cloud Run: `gcloud run deploy --source`.
# Oracle: same image under Coolify/Dokploy.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# psycopg[binary] needs libpq at runtime; curl for container healthchecks.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY engine/requirements.txt ./engine/requirements.txt
RUN pip install -r engine/requirements.txt

COPY engine/ ./engine/
COPY scan-configs.yaml ./
COPY INTEGRITY_CHARTER.md ./

WORKDIR /app/engine

# The Temporal server runs alongside the API inside the tier. In production
# this is the full Temporal service pointed at the shared Supabase Postgres
# (via DB env); the worker and uvicorn share the box.
EXPOSE 8000 7233 8233

# TIER_NAME, DATABASE_URL, WATCHDOG_SECRET, TEMPORAL_ADDRESS come from env.
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]
