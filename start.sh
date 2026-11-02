#!/usr/bin/env bash
# Build and start the relay server. Rerun after git pull to update it.
#   ./start.sh
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  echo ".env is missing: copy .env.example to .env and fill it in (see server/README.md)" >&2
  exit 1
fi
mkdir -p data
docker compose up -d --build
echo "started; check with: docker compose ps   (logs: docker compose logs -f server)"
