"""Config flow for RaySharp NVR integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .api_client import (
    RaySharpNVRAuthError,
    RaySharpNVRClient,
    RaySharpNVRConnectionError,
)
from .const import (
    CONF_EVENT_PUSH_AUTO_CONFIGURE,
    CONF_EVENT_TIMEOUT,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    DEFAULT_EVENT_PUSH_AUTO_CONFIGURE,
    DEFAULT_EVENT_TIMEOUT,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_USERNAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class RaySharpNVRConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for RaySharp NVR."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            client = RaySharpNVRClient(
                host=user_input[CONF_HOST],
                port=user_input[CONF_PORT],
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
            )

            try:
                login_data = await client.async_login()
            except RaySharpNVRAuthError:
                errors["base"] = "invalid_auth"
            except RaySharpNVRConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during login")
                errors["base"] = "unknown"
            else:
                # Extract MAC for unique ID
                data = login_data.get("data", login_data) if isinstance(login_data, dict) else {}
                mac = data.get("mac_addr", "")
                if mac:
                    await self.async_set_unique_id(mac)
                    self._abort_if_unique_id_configured()

                title = f"RaySharp NVR ({user_input[CONF_HOST]})"
                return self.async_create_entry(title=title, data=user_input)
            finally:
                await client.async_close()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """Handle reauth when credentials become invalid."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle reauth confirmation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            reauth_entry = self._get_reauth_entry()
            client = RaySharpNVRClient(
                host=reauth_entry.data[CONF_HOST],
                port=reauth_entry.data[CONF_PORT],
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
            )

            try:
                await client.async_login()
            except RaySharpNVRAuthError:
                errors["base"] = "invalid_auth"
            except RaySharpNVRConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during reauth")
                errors["base"] = "unknown"
            else:
                new_data = {**reauth_entry.data, **user_input}
                return self.async_update_reload_and_abort(
                    reauth_entry, data=new_data
                )
            finally:
                await client.async_close()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> RaySharpNVROptionsFlow:
        """Get the options flow."""
        return RaySharpNVROptionsFlow(config_entry)


class RaySharpNVROptionsFlow(OptionsFlow):
    """Handle options for RaySharp NVR."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_interval = self._config_entry.options.get(
            "scan_interval", DEFAULT_SCAN_INTERVAL
        )
        current_auto_configure = self._config_entry.options.get(
            CONF_EVENT_PUSH_AUTO_CONFIGURE, DEFAULT_EVENT_PUSH_AUTO_CONFIGURE
        )
        current_event_timeout = self._config_entry.options.get(
            CONF_EVENT_TIMEOUT, DEFAULT_EVENT_TIMEOUT
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("scan_interval", default=current_interval): vol.All(
                        int, vol.Range(min=10, max=300)
                    ),
                    vol.Required(
                        CONF_EVENT_PUSH_AUTO_CONFIGURE,
                        default=current_auto_configure,
                    ): bool,
                    vol.Required(
                        CONF_EVENT_TIMEOUT,
                        default=current_event_timeout,
                    ): vol.All(int, vol.Range(min=5, max=300)),
                }
            ),
        )
