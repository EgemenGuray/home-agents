"""Conversation agent entity that forwards Assist requests to the agent service.

Local first: Home Assistant's own intents and sentence triggers (lights, scenes, timers, alarms,
"good night") answer in well under a second and keep working when the agent service or the cloud
is down. Only what they don't understand goes to the agent.
"""

from __future__ import annotations

import asyncio
from typing import Literal

import aiohttp
from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_LOCAL_FIRST, CONF_TOKEN, CONF_URL
from .logic import TOO_SLOW, UNREACHABLE, keep_listening, local_handled, timeout_for


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([HomeAgentEntity(entry)])


class HomeAgentEntity(conversation.ConversationEntity):
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry
        self._attr_unique_id = entry.entry_id

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        return "*"

    def _room(self, device_id: str | None) -> str | None:
        if not device_id:
            return None
        device = dr.async_get(self.hass).async_get(device_id)
        if device is None or device.area_id is None:
            return None
        area = ar.async_get(self.hass).async_get_area(device.area_id)
        return area.id if area else None

    async def _local(self, user_input: conversation.ConversationInput) -> conversation.ConversationResult | None:
        if not self.entry.data.get(CONF_LOCAL_FIRST, True):
            return None
        try:
            result = await conversation.async_converse(
                self.hass, user_input.text, user_input.conversation_id, user_input.context,
                language=user_input.language, agent_id=conversation.HOME_ASSISTANT_AGENT, device_id=user_input.device_id)
        except Exception:  # noqa: BLE001 - the agent is the fallback
            return None
        r = result.response
        code = r.error_code.value if getattr(r, "error_code", None) else None
        return result if local_handled(r.response_type.value, code) else None

    async def async_process(self, user_input: conversation.ConversationInput) -> conversation.ConversationResult:
        local = await self._local(user_input)
        if local is not None:
            return local

        session = async_get_clientsession(self.hass)
        channel = "voice" if user_input.device_id else "app"
        payload = {
            "text": user_input.text,
            "conversation_id": user_input.conversation_id,
            "channel": channel,
            "room": self._room(user_input.device_id),
        }
        response = intent.IntentResponse(language=user_input.language)
        conversation_id = user_input.conversation_id
        speech = ""
        try:
            async with session.post(
                f"{self.entry.data[CONF_URL].rstrip('/')}/v1/converse",
                json=payload,
                headers={"Authorization": f"Bearer {self.entry.data[CONF_TOKEN]}"},
                timeout=aiohttp.ClientTimeout(total=timeout_for(channel)),
            ) as r:
                r.raise_for_status()
                data = await r.json()
            speech = data.get("text") or "Done."
            response.async_set_speech(speech)
            conversation_id = data.get("conversation_id") or conversation_id
        except (asyncio.TimeoutError, TimeoutError):
            response.async_set_error(intent.IntentResponseErrorCode.UNKNOWN, TOO_SLOW)
        except Exception:  # noqa: BLE001
            response.async_set_error(intent.IntentResponseErrorCode.UNKNOWN, UNREACHABLE)
        try:  # HA 2025.4+: listen again after a question, no second wake word
            return conversation.ConversationResult(response=response, conversation_id=conversation_id,
                                                   continue_conversation=keep_listening(speech))
        except TypeError:
            return conversation.ConversationResult(response=response, conversation_id=conversation_id)
