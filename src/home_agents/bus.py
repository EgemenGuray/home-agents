"""Message bus between agents (S1.5.2). One subject per job; the policy decides who may talk.

InMemoryBus is used in tests and on a single host; NatsBus runs against NATS on the hub Mac
(Phase 1) or the Studio (Phase 2). Both enforce the same policy before delivery.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Callable

from .policy import MessagePolicy


@dataclass
class Message:
    job_id: str
    sender: str
    recipient: str
    body: str
    kind: str = "text"       # text, request_peer, result, human
    meta: dict = field(default_factory=dict)

    def subject(self) -> str:
        return f"jobs.{self.job_id}.{self.recipient}"


class Blocked(Exception):
    pass


class InMemoryBus:
    def __init__(self, policy: MessagePolicy, audit=None):
        self.policy = policy
        self.audit = audit
        self._subs: dict[str, list[Callable[[Message], None]]] = defaultdict(list)
        self.delivered: list[Message] = []

    def subscribe(self, job_id: str, agent: str, handler: Callable[[Message], None]) -> None:
        self._subs[f"jobs.{job_id}.{agent}"].append(handler)

    def publish(self, msg: Message) -> None:
        if not self.policy.check(msg.sender, msg.recipient):
            if self.audit:
                self.audit.record("message_blocked", msg.sender, {"to": msg.recipient, "kind": msg.kind}, msg.job_id)
            raise Blocked(f"{msg.sender} may not message {msg.recipient}")
        if self.audit:
            self.audit.record("message", msg.sender, {"to": msg.recipient, "kind": msg.kind, "body": msg.body[:500]}, msg.job_id)
        self.delivered.append(msg)
        for handler in self._subs.get(msg.subject(), []):
            handler(msg)


class NatsBus:  # pragma: no cover - needs a NATS server
    """Same contract on NATS. Each agent's NATS user may only publish to the broker's ingress
    subject, so the policy check here cannot be skipped by talking to NATS directly."""

    def __init__(self, nc, policy: MessagePolicy, audit=None):
        self.nc = nc
        self.policy = policy
        self.audit = audit

    async def publish(self, msg: Message) -> None:
        if not self.policy.check(msg.sender, msg.recipient):
            if self.audit:
                self.audit.record("message_blocked", msg.sender, {"to": msg.recipient}, msg.job_id)
            raise Blocked(f"{msg.sender} may not message {msg.recipient}")
        await self.nc.publish(msg.subject(), json.dumps(asdict(msg)).encode())

    async def subscribe(self, job_id: str, agent: str, handler) -> None:
        async def _cb(raw):
            await handler(Message(**json.loads(raw.data)))
        await self.nc.subscribe(f"jobs.{job_id}.{agent}", cb=_cb)
