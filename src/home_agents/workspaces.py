"""Workspace manager (S1.4.3, S2.5.2): disposable shells, Linux desktops and macOS VMs.

Linux workspaces are containers on an internal Docker network with no route to the
internet; their only way out is the egress gateway's port for their assigned route, so a
dropped route fails closed (S1.3.4). No API keys or vault data are ever put inside.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import dataclass

IMAGES = {"cli": "home-agents/workspace-cli:latest", "desktop": "home-agents/workspace-desktop:latest"}
WS_NETWORK = "ws-internal"
GATEWAY = "egress-gateway"


@dataclass
class RoutePort:
    route: str
    port: int


class DockerWorkspaces:
    def __init__(self, route_ports: dict[str, int], docker_client=None, timeout_minutes: int = 60,
                 mem_limit: str = "3g", cpus: float = 2.0, audit=None):
        if docker_client is None:  # pragma: no cover
            import docker
            docker_client = docker.from_env()
        self.docker = docker_client
        self.route_ports = route_ports
        self.timeout = timeout_minutes * 60
        self.mem_limit = mem_limit
        self.cpus = cpus
        self.audit = audit

    def create(self, *, job_id: str, kind: str, route: str) -> str:
        if kind not in IMAGES:
            raise ValueError(f"no Linux image for workspace kind {kind!r}")
        if route not in self.route_ports:
            raise ValueError(f"route {route!r} has no gateway port")
        proxy = f"http://{GATEWAY}:{self.route_ports[route]}"
        name = f"ws-{uuid.uuid4().hex[:10]}"
        self.docker.containers.run(
            IMAGES[kind],
            name=name,
            detach=True,
            network=WS_NETWORK,
            environment={"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy,
                         "NO_PROXY": "localhost,127.0.0.1", "WORKSPACE_ROUTE": route, "JOB_ID": job_id},
            labels={"home-agents.job": job_id, "home-agents.route": route,
                    "home-agents.expires": str(int(time.time()) + self.timeout)},
            mem_limit=self.mem_limit,
            nano_cpus=int(self.cpus * 1e9),
            pids_limit=512,
            cap_drop=["ALL"],
            cap_add=["CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE"],
            security_opt=["no-new-privileges"],
            shm_size="1g" if kind == "desktop" else "64m",
        )
        if self.audit:
            self.audit.record("workspace_created", "orchestrator", {"workspace": name, "kind": kind, "route": route}, job_id)
        return name

    def snapshot(self, name: str, tag: str) -> str:
        c = self.docker.containers.get(name)
        img = c.commit(repository="home-agents/snapshots", tag=tag)
        return img.id

    def destroy(self, name: str) -> None:
        try:
            c = self.docker.containers.get(name)
        except Exception:
            return
        c.remove(force=True)
        if self.audit:
            self.audit.record("workspace_destroyed", "orchestrator", {"workspace": name}, None)

    def reap_expired(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        gone = []
        for c in self.docker.containers.list(filters={"label": "home-agents.job"}):
            if int(c.labels.get("home-agents.expires", "0")) < now:
                self.destroy(c.name)
                gone.append(c.name)
        return gone


class TartWorkspaces:  # pragma: no cover - needs a Mac with Tart (Phase 2, S2.5.2)
    """macOS VM workspaces on the Mac Studio. At most two run at once (Apple's licence and RAM)."""

    MAX_RUNNING = 2

    def __init__(self, base_image: str = "home-agents-macos-base", tart: str = "tart"):
        self.base = base_image
        self.tart = tart

    def _running(self) -> list[str]:
        out = subprocess.run([self.tart, "list", "--quiet", "--source", "local"], capture_output=True, text=True, check=True)
        names = [n for n in out.stdout.split() if n.startswith("ws-mac-")]
        running = []
        for n in names:
            ip = subprocess.run([self.tart, "ip", n], capture_output=True, text=True)
            if ip.returncode == 0:
                running.append(n)
        return running

    def create(self, *, job_id: str, kind: str, route: str) -> str:
        if kind != "macos":
            raise ValueError("TartWorkspaces only runs macOS")
        if len(self._running()) >= self.MAX_RUNNING:
            raise RuntimeError("two macOS workspaces are already running")
        name = f"ws-mac-{uuid.uuid4().hex[:8]}"
        subprocess.run([self.tart, "clone", self.base, name], check=True)
        # Network: the VM's only route out is the mini's proxy for `route` (set in the base image's
        # pf rules from the WORKSPACE_PROXY written to its shared folder).
        subprocess.Popen([self.tart, "run", "--no-graphics", "--vnc-experimental", name])
        return name

    def destroy(self, name: str) -> None:
        subprocess.run([self.tart, "stop", name], check=False)
        subprocess.run([self.tart, "delete", name], check=False)


class WorkspaceRouter:
    """Sends each workspace kind to the host that runs it."""

    def __init__(self, linux, macos=None):
        self.linux = linux
        self.macos = macos
        self._owner: dict[str, object] = {}

    def create(self, *, job_id: str, kind: str, route: str) -> str:
        host = self.macos if kind == "macos" else self.linux
        if host is None:
            raise RuntimeError(f"no host for {kind} workspaces yet")
        name = host.create(job_id=job_id, kind=kind, route=route)
        self._owner[name] = host
        return name

    def destroy(self, name: str) -> None:
        host = self._owner.pop(name, None) or self.linux
        host.destroy(name)
