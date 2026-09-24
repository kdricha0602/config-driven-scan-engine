# One image for all three tiers: API + Temporal server + worker.
# Render: native Docker deploy. Cloud Run: `gcloud run deploy --source`.
# Oracle: same image under Coolify/Dokploy.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TEMPORAL_CLI_VERSION=1.9.1

WORKDIR /app

# psycopg[binary] needs libpq at runtime; curl/tar for the Temporal CLI
# download and for container healthchecks.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl tar \
    && rm -rf /var/lib/apt/lists/*

# Temporal CLI (bundles the dev server). Pinned; bump TEMPORAL_CLI_VERSION
# to upgrade. The dev server persists to ephemeral SQLite — fine, because
# all durable scan state (watermarks, results, dedup) lives in Supabase;
# a container restart simply re-triggers from the last watermark.
RUN curl -sSL -o /tmp/tcli.tar.gz \
      https://github.com/temporalio/cli/releases/download/v${TEMPORAL_CLI_VERSION}/temporal_cli_${TEMPORAL_CLI_VERSION}_linux_amd64.tar.gz \
    && tar xzf /tmp/tcli.tar.gz -C /usr/local/bin temporal \
    && rm /tmp/tcli.tar.gz \
    && temporal --version

COPY engine/requirements.txt ./engine/requirements.txt
RUN pip install -r engine/requirements.txt

COPY engine/ ./engine/
COPY scan-configs.yaml ./

WORKDIR /app/engine

# The Temporal server runs alongside the API inside the tier. The dev server
# keeps its event history in ephemeral SQLite (see above); the worker and
# uvicorn share the box on localhost:7233.
EXPOSE 8000 7233 8233

COPY deploy/start.sh /app/start.sh
RUN chmod +x /app/start.sh

# TIER_NAME, DATABASE_URL, WATCHDOG_SECRET, TEMPORAL_ADDRESS come from env.
CMD ["/app/start.sh"]
