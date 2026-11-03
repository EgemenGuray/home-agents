"""Screen relay (S1.6.1): the only way into an agent's desktop, for the app's live view and take over.

Runs on the workspace host, next to the desktops: the rented Linux VM with the Mac mini only, the
Studio with the Mac mini + Mac Studio. It listens on 127.0.0.1 and is published on the tailnet with
`tailscale serve`, like every other service. Each desktop's VNC server listens on a Unix socket in
its own Docker volume, so desktops can't reach each other's screens over the workspace network.

The app opens a WebSocket here with a ticket from the agent service (`takeover.py`):

* binary frames are VNC (RFB) in both directions, so noVNC in a web view works unchanged;
* a view ticket shows the screen and drops your keyboard, pointer and clipboard input;
* a text frame `{"ticket": "<control ticket>"}` upgrades the connection to control without
  reconnecting, so tapping the screen takes over at once. Control lasts until that ticket expires,
  and the app keeps renewing it while you drive. When you hand back, renewals stop and input is
  dropped again within a minute at most (at once, if the app closes the socket, which it does);
* with `?notices=1` (the native app) the relay answers upgrades and says once when it dropped your
  input, so the app can offer Take over. Without it (noVNC in a browser) it sends only VNC.
"""

import asyncio
import json
import re
import struct
import time
from typing import Awaitable, Callable

from .takeover import CONTROL, TicketError, TicketVerifier

WORKSPACE_NAME = re.compile(r"^ws-(mac-)?[a-z0-9]{6,16}$")

# Client-to-server RFB messages: type -> (header length, function giving the full length from the header).
_FIXED = {0: 20, 3: 10, 4: 8, 5: 6, 150: 10, 250: 4, 255: 12}
INPUT_TYPES = {4, 5, 6, 255}          # key, pointer, clipboard, QEMU extended key
MAX_CLIPBOARD = 1 << 20


class ProtocolError(Exception):
    pass


def _length(buf: bytes | bytearray) -> int | None:
    """Full length of the message at the start of buf, or None if more bytes are needed to tell."""
    kind = buf[0]
    if kind in _FIXED:
        return _FIXED[kind]
    if kind == 2:                     # SetEncodings: pad, u16 count, s32 * count
        return None if len(buf) < 4 else 4 + 4 * struct.unpack(">H", bytes(buf[2:4]))[0]
    if kind == 6:                     # ClientCutText: pad*3, u32 length, text
        if len(buf) < 8:
            return None
        n = struct.unpack(">I", bytes(buf[4:8]))[0]
        if n > MAX_CLIPBOARD:
            raise ProtocolError("clipboard too large")
        return 8 + n
    if kind == 248:                   # ClientFence: pad*3, u32 flags, u8 length, payload
        return None if len(buf) < 9 else 9 + buf[8]
    if kind == 251:                   # SetDesktopSize: pad, w, h, u8 screens, pad, 16 * screens
        return None if len(buf) < 8 else 8 + 16 * buf[6]
    raise ProtocolError(f"unknown client message {kind}")


class RfbInputFilter:
    """Passes the handshake and screen requests through and drops input unless you hold control.

    The handshake from the client is the 12-byte version, the 1-byte security choice (None, because
    the desktop's VNC is reachable only through this relay) and the 1-byte ClientInit.
    """

    def __init__(self, handshake_bytes: int = 14):
        self._handshake = handshake_bytes
        self._buf = bytearray()

    def feed(self, data: bytes, allow_input: bool) -> tuple[bytes, int]:
        """Returns the bytes to forward and how many input messages were dropped."""
        out = bytearray()
        dropped = 0
        if self._handshake:
            take = data[: self._handshake]
            out += take
            self._handshake -= len(take)
            data = data[len(take):]
        self._buf += data
        while self._buf:
            n = _length(self._buf)
            if n is None or len(self._buf) < n:
                break
            msg, self._buf = bytes(self._buf[:n]), self._buf[n:]
            if msg[0] in INPUT_TYPES and not allow_input:
                dropped += 1
                continue
            out += msg
        return bytes(out), dropped


Upstream = Callable[[str], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


DOCKER_SOCKET = "/var/lib/docker/volumes/screen-{workspace}/_data/vnc.sock"


def unix_upstream(template: str = DOCKER_SOCKET) -> Upstream:
    """Linux desktops: x11vnc listens on vnc.sock in the workspace's own `screen-<name>` volume
    (workspaces.py, workspaces/desktop/start.sh). The relay mounts the Docker volumes folder read-only."""
    async def connect(workspace: str):
        return await asyncio.open_unix_connection(template.format(workspace=workspace))
    return connect


def build_relay(verifier: TicketVerifier, upstream: Upstream, clock: Callable[[], float] = time.time):
    from fastapi import FastAPI, WebSocket

    app = FastAPI(title="screen-relay", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.websocket("/screen/{workspace}")
    async def screen(websocket: WebSocket, workspace: str, ticket: str = "", notices: int = 0):
        if not WORKSPACE_NAME.match(workspace):
            await websocket.close(code=4404)
            return
        try:
            mode, expires = verifier.verify(ticket, workspace)
        except TicketError:
            await websocket.close(code=4401)
            return
        offered = websocket.scope.get("subprotocols") or []
        await websocket.accept(subprotocol="binary" if "binary" in offered else None)
        try:
            reader, writer = await upstream(workspace)
        except OSError:
            await websocket.close(code=4502)
            return
        control_until = expires if mode == CONTROL else 0.0
        warned = False
        rfb = RfbInputFilter()

        async def notice(data: dict) -> None:
            # Only clients that asked (?notices=1, the native app) get text frames; noVNC expects binary only.
            if notices:
                await websocket.send_text(json.dumps(data))

        async def down():
            while data := await reader.read(65536):
                await websocket.send_bytes(data)

        async def up():
            nonlocal control_until, warned
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("text") is not None:
                    try:
                        m, exp = verifier.verify(json.loads(msg["text"]).get("ticket", ""), workspace)
                    except (TicketError, ValueError, AttributeError) as exc:
                        await notice({"control": False, "error": str(exc)})
                        continue
                    if m == CONTROL:
                        control_until, warned = exp, False
                    await notice({"control": m == CONTROL, "until": int(exp)})
                    continue
                allow = clock() < control_until
                out, dropped = rfb.feed(msg.get("bytes") or b"", allow)
                if out:
                    writer.write(out)
                    await writer.drain()
                if dropped and not warned:
                    warned = True
                    await notice({"control": False, "input": "dropped",
                                  "hint": "The agent is driving. Tap Take over to use the screen."})

        tasks = [asyncio.ensure_future(down()), asyncio.ensure_future(up())]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                if t.exception() and isinstance(t.exception(), ProtocolError):
                    await websocket.close(code=4400)
        finally:
            writer.close()

    return app


def main() -> None:  # pragma: no cover - entry point on the workspace host
    import os

    import uvicorn

    key = bytes.fromhex(os.environ["HOME_AGENTS_SCREEN_KEY"])
    app = build_relay(TicketVerifier(key), unix_upstream(os.environ.get("SCREEN_SOCKET", DOCKER_SOCKET)))
    # In its container it listens on all interfaces; compose publishes it on the host's 127.0.0.1 only.
    uvicorn.run(app, host=os.environ.get("RELAY_HOST", "127.0.0.1"), port=int(os.environ.get("RELAY_PORT", "8710")))


if __name__ == "__main__":  # pragma: no cover
    main()
