#!/usr/bin/env bash
# Build here, ship the image to Unraid, (re)start it behind Traefik. Same approach as videomessaging.
set -euo pipefail

UNRAID_HOST="${UNRAID_HOST:-unraid}"
REMOTE_DIR=/mnt/user/appdata/image-merge
cd "$(dirname "$0")"

echo "==> Building image-merge:latest..."
docker build -q -t image-merge:latest .

echo "==> Transferring image to Unraid ($UNRAID_HOST)..."
docker save image-merge:latest | gzip -1 | ssh "$UNRAID_HOST" "gunzip | docker load"

echo "==> Updating $REMOTE_DIR/compose.yml..."
ssh "$UNRAID_HOST" "mkdir -p $REMOTE_DIR"
scp -q compose.yml "$UNRAID_HOST:$REMOTE_DIR/compose.yml"

# First deploy only: seed ./data (admin account). Never overwrite live templates or accounts.
if ssh "$UNRAID_HOST" "test -e $REMOTE_DIR/data"; then
  echo "==> Keeping existing data on Unraid"
else
  echo "==> Seeding data (first deploy)..."
  tar -cz data | ssh "$UNRAID_HOST" "tar -C $REMOTE_DIR -xz"
fi

echo "==> Starting container..."
ssh "$UNRAID_HOST" "cd $REMOTE_DIR && docker compose up -d"
sleep 4
ssh "$UNRAID_HOST" "docker logs image-merge 2>&1 | tail -n 5"

echo "==> Checking https://image-merge.biel.casa/healthz..."
curl -s -w "\nHTTP %{http_code}\n" https://image-merge.biel.casa/healthz || true
