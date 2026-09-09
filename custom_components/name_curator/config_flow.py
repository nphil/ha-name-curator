"""Config and options flows for Name Curator."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import DOMAIN, logic

ENTRY_TITLE = "Name Curator"

# Offered in the dropdowns; custom values are accepted too.
DEVICE_CLASS_CHOICES = [
    "door", "garage_door", "window", "opening", "lock", "gate", "motion",
    "occupancy", "presence", "moisture", "smoke", "carbon_monoxide", "gas",
    "vibration", "tamper", "safety", "problem", "power", "plug", "outlet",
    "battery", "temperature", "humidity",
]
DOMAIN_CHOICES = [
    "automation", "script", "scene", "input_boolean", "input_number", "input_select",
    "input_datetime", "input_text", "input_button", "counter", "timer", "group",
    "sensor", "binary_sensor", "switch", "light", "fan", "cover", "climate",
    "media_player", "camera", "lock", "siren", "number", "select", "button",
]


def _multi_select(choices: list[str]) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=choices,
            multiple=True,
            custom_value=True,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _options_schema(options: logic.Options) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(logic.OPTION_STRIP_DEVICES, default=options.strip_devices):
                selector.BooleanSelector(),
            vol.Required(logic.OPTION_STRIP_ENTITIES, default=options.strip_entities):
                selector.BooleanSelector(),
            vol.Required(logic.OPTION_RESTORE_ON_MOVE, default=options.restore_on_move):
                selector.BooleanSelector(),
            vol.Required(logic.OPTION_ASSIST_ALIASES, default=options.assist_aliases):
                selector.BooleanSelector(),
            vol.Required(logic.OPTION_RENAME_ENTITY_IDS, default=options.rename_entity_ids):
                selector.BooleanSelector(),
            vol.Optional(
                logic.OPTION_EXCLUDED_DEVICE_CLASSES,
                default=sorted(options.excluded_device_classes),
            ): _multi_select(DEVICE_CLASS_CHOICES),
            vol.Optional(
                logic.OPTION_EXCLUDED_DOMAINS,
                default=sorted(options.excluded_domains),
            ): _multi_select(DOMAIN_CHOICES),
            vol.Required(logic.OPTION_NOTIFY, default=options.notify):
                selector.BooleanSelector(),
            vol.Required(logic.OPTION_DEBOUNCE_SECONDS, default=options.debounce_seconds):
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=logic.MIN_DEBOUNCE_SECONDS,
                        max=logic.MAX_DEBOUNCE_SECONDS,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
        }
    )


class NameCuratorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Single entry; nothing to ask at setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title=ENTRY_TITLE, data={})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> config_entries.OptionsFlow:
        return NameCuratorOptionsFlow()


class NameCuratorOptionsFlow(config_entries.OptionsFlow):
    """Edit the policy; saving reloads the entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            saved = dict(user_input)
            # NumberSelector returns a float; an emptied multi-select is omitted.
            saved[logic.OPTION_DEBOUNCE_SECONDS] = int(float(saved[logic.OPTION_DEBOUNCE_SECONDS]))
            saved.setdefault(logic.OPTION_EXCLUDED_DEVICE_CLASSES, [])
            saved.setdefault(logic.OPTION_EXCLUDED_DOMAINS, [])
            return self.async_create_entry(title="", data=saved)
        stored = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(logic.options_from_mapping(stored)),
        )
