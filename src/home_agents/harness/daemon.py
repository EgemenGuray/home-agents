"""Background loops for the harness (E1.12): heartbeat, chat channels and schedules.

Started once with the service. Each loop catches its own errors so one failing channel or
model never stops the others. Messages to the phone go through Home Assistant's notify
service, which the app already receives.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from typing import Callable

from . import Harness
from .channels import Gateway, Pairing, TelegramChannel
from .heartbeat import Heartbeat

log = logging.getLogger(__name__)


def state_summary(h: Harness) -> str:
    waiting = h.store.tasks("waiting_approval")
    failed = h.store.tasks("failed", limit=5)
    spend = h.router.spend
    lines = [f"Tasks waiting for approval: {len(waiting)}" + "".join(f"\n- {t['title']} since {t['updated_at']}" for t in waiting),
             f"Recently failed tasks: {len(failed)}" + "".join(f"\n- {t['title']} at {t['updated_at']}" for t in failed)]
    if spend is not None:
        lines.append(f"Model spend this month: CA${spend.month():.2f} of CA${spend.monthly_cap_cad:.0f}")
    return "\n".join(lines)


def phone_notifier(ha, service: str | None) -> Callable[[str], None]:
    """`service` is e.g. "notify.mobile_app_<phone>" from the config; without HA, messages are only logged."""
    def send(text: str) -> None:
        if ha is None or not service:
            log.info("notify: %s", text)
            return
        domain, _, name = service.partition(".")
        ha.call_service({"domain": domain, "service": name, "data": {"message": text, "title": "Home agents"}})
    return send


def _every(seconds: float, fn: Callable[[], None], stop: threading.Event, name: str) -> threading.Thread:
    def loop():
        while not stop.is_set():
            try:
                fn()
            except Exception:  # keep the loop alive; the error is in the log
                log.exception("%s loop failed", name)
            stop.wait(seconds)
    t = threading.Thread(target=loop, daemon=True, name=name)
    t.start()
    return t


def start(h: Harness, config: dict, pairing: Pairing, notify: Callable[[str], None]) -> threading.Event:
    stop = threading.Event()

    hb = config.get("heartbeat")
    if hb:
        beat = Heartbeat(h.router, hb.get("checklist", "config/HEARTBEAT.md"), lambda: state_summary(h),
                         lambda b: notify(b.message) if b.message else None,
                         model=h.profiles["heartbeat"].model if "heartbeat" in h.profiles else "fast")
        _every(60 * float(hb.get("every_minutes", 30)), lambda: beat.beat(datetime.now()), stop, "heartbeat")

    channels = []
    tg = (config.get("channels") or {}).get("telegram")
    if tg and os.environ.get(tg.get("token_env", "")):
        channels.append(TelegramChannel(os.environ[tg["token_env"]]))
    if channels:
        def chat(key: str, text: str, channel: str) -> str:
            sid = _channel_sessions.get(key)
            session, r = h.chat(sid, text, channel=channel)
            _channel_sessions[key] = session["id"]
            return r.text + ("\n(Waiting for your approval on the phone.)" if r.pending_approvals else "")
        gw = Gateway(channels, chat, pairing, h.agent.audit)
        _every(1, gw.poll_once, stop, "channels")  # each poll blocks up to 25 s on the long poll

    # Finished tasks reach the phone like Dispatch's "done" notification.
    h.tasks.notify = lambda t: notify(f"{t['title']}: {t['state']}. {(t.get('result') or '')[:200]}") if t else None
    return stop


_channel_sessions: dict[str, str] = {}
