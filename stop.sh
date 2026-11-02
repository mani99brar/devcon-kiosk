#!/usr/bin/env bash
# Stop the relay server. Keeps .env, ./data and the image; ./start.sh starts it again.
#   ./stop.sh
set -euo pipefail
cd "$(dirname "$0")"
docker compose down
