#!/usr/bin/env bash
# Deploys a merged release and rolls back by itself if health checks fail (S1.9.3).
# Usage on the host: ./deploy/deploy.sh <git-tag>
set -euo pipefail
TAG="${1:?release tag}"
HERE="$(cd "$(dirname "$0")" && pwd)"
COMPOSE=(docker compose -f "$HERE/hub/compose.yaml")
STATE="$HERE/hub/.current-release"
PREV="$(cat "$STATE" 2>/dev/null || echo latest)"

notify() { [[ -n "${NOTIFY_URL:-}" ]] && curl -fsS -m 10 -d "$1" "$NOTIFY_URL" >/dev/null || true; echo "$1"; }

healthy() {
  for _ in $(seq 1 20); do
    curl -fsS -m 5 http://127.0.0.1:8700/healthz >/dev/null && return 0
    sleep 3
  done
  return 1
}

git -C "$HERE/.." fetch --tags --quiet
git -C "$HERE/.." checkout --quiet "$TAG"
RELEASE="$TAG" "${COMPOSE[@]}" build agent
RELEASE="$TAG" "${COMPOSE[@]}" up -d agent

if healthy; then
  echo "$TAG" > "$STATE"
  notify "Deployed $TAG"
else
  notify "Deploy of $TAG failed health checks; rolling back to $PREV"
  git -C "$HERE/.." checkout --quiet "$PREV" 2>/dev/null || true
  RELEASE="$PREV" "${COMPOSE[@]}" up -d agent
  healthy && notify "Rolled back to $PREV" || notify "ROLLBACK FAILED: agent service is down"
  exit 1
fi
