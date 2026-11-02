#!/usr/bin/env bash
# Remove the relay server's containers, networks, volumes and images.
# The repo folder (with .env and ./data) stays; delete it by hand if wanted.
#   ./destroy.sh
set -euo pipefail
cd "$(dirname "$0")"
docker compose down --volumes --rmi all --remove-orphans
