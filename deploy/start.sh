#!/bin/sh
# All-in-one tier startup: Temporal dev server -> worker -> API.
# Fail fast on any piece so Render restarts the container instead of
# serving a half-alive tier.
set -e

# 1. Temporal dev server (ephemeral SQLite; durable state lives in Supabase).
temporal server start-dev \
  --db-filename /tmp/temporal.db \
  --port 7233 --ui-port 8233 \
  > /tmp/temporal-server.log 2>&1 &
SERVER_PID=$!

# 2. Wait for the gRPC frontend before anything dials it. Uses the CLI
# itself as the probe (portable across sh/dash — no /dev/tcp).
echo "waiting for temporal server on 7233..."
for i in $(seq 1 60); do
  if temporal operator cluster describe --address 127.0.0.1:7233 \
      >/dev/null 2>&1; then
    echo "temporal server up"
    break
  fi
  if ! kill -0 $SERVER_PID 2>/dev/null; then
    echo "temporal server died during startup:" >&2
    tail -20 /tmp/temporal-server.log >&2
    exit 1
  fi
  sleep 1
done
temporal operator cluster describe --address 127.0.0.1:7233 \
  >/dev/null 2>&1 || {
  echo "temporal server never came up" >&2
  tail -20 /tmp/temporal-server.log >&2
  exit 1
}

# 3. Worker (task queue: equity-scans). Dies -> container dies -> Render restarts.
cd /app/engine
python worker.py > /tmp/worker.log 2>&1 &
WORKER_PID=$!
sleep 5
if ! kill -0 $WORKER_PID 2>/dev/null; then
  echo "worker died during startup:" >&2
  tail -20 /tmp/worker.log >&2
  exit 1
fi
echo "worker up"

# 4. API in the foreground (Render routes $PORT here; /ready is the health check).
exec uvicorn api:app --host 0.0.0.0 --port "${PORT:-8000}"
