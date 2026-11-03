#!/usr/bin/env bash
# Desktop session for one agent. VNC listens only on a Unix socket in this workspace's own volume
# (/run/screen, a per-workspace Docker volume) plus the container's loopback. Nothing on the workspace
# network can reach it; the app sees and drives it only through the screen relay on the host, with a
# signed ticket (home_agents/screen_relay.py).
set -euo pipefail
Xvfb "$DISPLAY" -screen 0 "${WIDTH}x${HEIGHT}x24" -nolisten tcp &
sleep 1
startxfce4 >/dev/null 2>&1 &
rm -f /run/screen/vnc.sock
exec x11vnc -display "$DISPLAY" -forever -shared -localhost -rfbport 5900 -unixsock /run/screen/vnc.sock -nopw -quiet
