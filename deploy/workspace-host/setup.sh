#!/usr/bin/env bash
# One-time setup of the rented workspace VM (Ubuntu 24.04). Nothing is exposed publicly:
# the provider firewall allows no inbound ports, and SSH is Tailscale SSH only.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh --advertise-tags=tag:workspace
apt-get update && apt-get install -y docker.io docker-compose-v2 ufw
ufw default deny incoming && ufw default allow outgoing && ufw allow in on tailscale0 && ufw --force enable
systemctl disable --now ssh || true
# Workspaces may not reach the VM's own metadata service or the host.
iptables -I DOCKER-USER -d 169.254.169.254 -j DROP
docker build -t home-agents/workspace-cli:latest ../../workspaces/cli
docker build -t home-agents/workspace-desktop:latest ../../workspaces/desktop
docker build -t home-agents/service:latest ../..     # the screen relay runs from the service image
docker compose up -d
# Screen relay (live view and take over): tailnet only, HTTPS with the tailnet certificate.
tailscale serve --bg --https=8710 http://127.0.0.1:8710
