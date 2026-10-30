from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_LOCAL_FIRST, CONF_TOKEN, CONF_URL, DOMAIN


class HomeAgentConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            base = user_input[CONF_URL].rstrip("/")
            try:
                # An authenticated call, so a wrong token fails here and not on the first voice request.
                async with session.get(f"{base}/v1/approvals", timeout=10,
                                       headers={"Authorization": f"Bearer {user_input[CONF_TOKEN]}"}) as r:
                    if r.status == 401:
                        errors["base"] = "invalid_auth"
                    elif r.status != 200:
                        errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "cannot_connect"
            if not errors:
                await self.async_set_unique_id(base)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Home Agent", data=user_input)
        schema = vol.Schema({vol.Required(CONF_URL): str, vol.Required(CONF_TOKEN): str,
                             vol.Optional(CONF_LOCAL_FIRST, default=True): bool})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
