#!/usr/bin/env bash
# Desktop session for one agent. VNC listens only inside the workspace network; the app reaches
# it through the workspace manager's authenticated noVNC proxy.
set -euo pipefail
Xvfb "$DISPLAY" -screen 0 "${WIDTH}x${HEIGHT}x24" -nolisten tcp &
sleep 1
startxfce4 >/dev/null 2>&1 &
x11vnc -display "$DISPLAY" -forever -shared -rfbport 5900 -localhost -nopw -quiet &
exec websockify --web /usr/share/novnc 6080 localhost:5900
