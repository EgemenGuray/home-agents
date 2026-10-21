from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_TOKEN, CONF_URL, DOMAIN


class HomeAgentConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            try:
                async with session.get(f"{user_input[CONF_URL].rstrip('/')}/healthz", timeout=10) as r:
                    if r.status != 200:
                        errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "cannot_connect"
            if not errors:
                return self.async_create_entry(title="Home Agent", data=user_input)
        schema = vol.Schema({vol.Required(CONF_URL): str, vol.Required(CONF_TOKEN): str})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
