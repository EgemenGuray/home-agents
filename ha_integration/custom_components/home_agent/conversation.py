"""Conversation agent entity that forwards Assist requests to the agent service."""

from __future__ import annotations

from typing import Literal

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_TOKEN, CONF_URL


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

    async def async_process(self, user_input: conversation.ConversationInput) -> conversation.ConversationResult:
        session = async_get_clientsession(self.hass)
        payload = {
            "text": user_input.text,
            "conversation_id": user_input.conversation_id,
            "channel": "voice" if user_input.device_id else "app",
            "room": self._room(user_input.device_id),
        }
        response = intent.IntentResponse(language=user_input.language)
        conversation_id = user_input.conversation_id
        try:
            async with session.post(
                f"{self.entry.data[CONF_URL].rstrip('/')}/v1/converse",
                json=payload,
                headers={"Authorization": f"Bearer {self.entry.data[CONF_TOKEN]}"},
                timeout=60,
            ) as r:
                r.raise_for_status()
                data = await r.json()
            response.async_set_speech(data.get("text") or "Done.")
            conversation_id = data.get("conversation_id") or conversation_id
        except Exception:  # noqa: BLE001
            response.async_set_error(intent.IntentResponseErrorCode.UNKNOWN,
                                     "The assistant is not reachable right now; simple commands still work.")
        return conversation.ConversationResult(response=response, conversation_id=conversation_id)
