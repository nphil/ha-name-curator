"""Name Curator.

Keeps device and entity display names free of the room they are already
grouped under. When a device is created or gains an area, or an area is
renamed, its displayed name is checked against the area name (and aliases) and
the repeated prefix is removed — "Dining Room Humidity Sensor" in Dining Room
becomes "Humidity Sensor". Entity ids never change; only the ``name_by_user``
of devices and the ``name`` override of entities that do not follow their
device are written.

All decisions live in ``logic.py`` (pure, unit-tested); this module gathers
registry facts, applies the plan, and reports it as a persistent notification.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, ServiceCall, callback
from homeassistant.helpers import (
    area_registry as ar,
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.typing import ConfigType

from . import logic
from .logic import Area, Device, Entity, Options, Plan, Snapshot

_LOGGER = logging.getLogger(__name__)

DOMAIN = "name_curator"
SERVICE_CURATE = "curate"
ATTR_DRY_RUN = "dry_run"

NOTIFICATION_ID = "name_curator"
NOTIFICATION_TITLE = "Name curator"
# Assist stores its exposure flag under the conversation domain in entity options.
ASSIST_DOMAIN = "conversation"

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SERVICE_SCHEMA = vol.Schema({vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean})

DEVICE_FIELDS = {"area_id", "name", "name_by_user"}
ENTITY_FIELDS = {"area_id", "name", "device_id", "disabled_by", "entity_category"}


class Curator:
    """Watches the registries and shortens what repeats its area."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.options = logic.options_from_mapping(entry.options)
        self._lock = asyncio.Lock()
        self._pending: CALLBACK_TYPE | None = None
        self._applying = False
        self._unsubs: list[CALLBACK_TYPE] = []

    # -- lifecycle ---------------------------------------------------------

    @callback
    def async_start(self) -> None:
        bus = self.hass.bus
        self._unsubs = [
            bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._device_event),
            bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._entity_event),
            bus.async_listen(ar.EVENT_AREA_REGISTRY_UPDATED, self._area_event),
        ]
        if self.hass.is_running:
            self._schedule()
        else:
            self._unsubs.append(bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._started))

    @callback
    def _started(self, _event: Event) -> None:
        self._schedule()

    @callback
    def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()
        if self._pending:
            self._pending()
            self._pending = None

    # -- events ------------------------------------------------------------

    @callback
    def _device_event(self, event: Event) -> None:
        if self._applying:
            return
        data = event.data
        if data["action"] == "create":
            self._schedule()
        elif data["action"] == "update" and DEVICE_FIELDS & set(data.get("changes", {})):
            if "area_id" in data["changes"]:
                self.hass.async_create_task(
                    self._async_handle_move(data["device_id"], data["changes"]["area_id"])
                )
            self._schedule()

    @callback
    def _entity_event(self, event: Event) -> None:
        if self._applying:
            return
        data = event.data
        if data["action"] == "create" or (
            data["action"] == "update" and ENTITY_FIELDS & set(data.get("changes", {}))
        ):
            self._schedule()

    @callback
    def _area_event(self, event: Event) -> None:
        if not self._applying and event.data["action"] in ("create", "update"):
            self._schedule()

    @callback
    def _schedule(self) -> None:
        if self._pending:
            self._pending()
        self._pending = async_call_later(
            self.hass, self.options.debounce_seconds, self._run_scheduled
        )

    @callback
    def _run_scheduled(self, _now: Any) -> None:
        self._pending = None
        self.hass.async_create_task(self.async_curate())

    # -- facts -------------------------------------------------------------

    def _snapshot(self) -> Snapshot:
        areas = {
            area.id: Area(area.id, area.name, tuple(sorted(area.aliases)))
            for area in ar.async_get(self.hass).async_list_areas()
        }
        devices = {
            device.id: Device(device.id, device.name, device.name_by_user, device.area_id)
            for device in dr.async_get(self.hass).devices  # iterating yields entries (2026.9+)
        }
        entities: dict[str, Entity] = {}
        for entry in er.async_get(self.hass).entities.values():
            state = self.hass.states.get(entry.entity_id)
            entities[entry.entity_id] = Entity(
                entity_id=entry.entity_id,
                device_id=entry.device_id,
                area_id=entry.area_id,
                name=entry.name,
                original_name=entry.original_name,
                has_entity_name=entry.has_entity_name,
                disabled=entry.disabled_by is not None,
                entity_category=entry.entity_category.value if entry.entity_category else None,
                # Some platforms (zwave_js, groups) only publish the class in state.
                device_class=entry.device_class
                or entry.original_device_class
                or (state.attributes.get("device_class") if state else None),
                friendly_name=state.attributes.get("friendly_name") if state else None,
                # Aliases are an ordered list; Core's COMPUTED_NAME sentinel is
                # not a str and is passed through untouched.
                aliases=tuple(a for a in entry.aliases if isinstance(a, str)),
                # Read the stored setting only: async_should_expose() persists a
                # default for every entity it is asked about.
                exposed_to_assist=bool(
                    entry.options.get(ASSIST_DOMAIN, {}).get("should_expose", False)
                ),
            )
        return Snapshot(areas, devices, entities)

    # -- actions -----------------------------------------------------------

    async def _async_handle_move(self, device_id: str, previous_area_id: str | None) -> None:
        """Restore a name we shortened once its device leaves that area."""
        if previous_area_id is None:
            return
        async with self._lock:
            registry = dr.async_get(self.hass)
            device = registry.async_get(device_id)
            if device is None:
                return
            areas = ar.async_get(self.hass)
            previous = areas.async_get_area(previous_area_id)
            current = areas.async_get_area(device.area_id) if device.area_id else None
            change = logic.plan_device_move(
                Device(device.id, device.name, device.name_by_user, device.area_id),
                Area(previous.id, previous.name, tuple(previous.aliases)) if previous else None,
                Area(current.id, current.name, tuple(current.aliases)) if current else None,
                self.options,
            )
            if change is None:
                return
            self._applying = True
            try:
                registry.async_update_device(device.id, name_by_user=None)
            finally:
                self._applying = False
            _LOGGER.info(
                "Restored %s: %r -> %r (left %s)", device.id, change.old, device.name,
                previous.name if previous else previous_area_id,
            )
            if self.options.notify:
                snapshot = self._snapshot()
                self._notify(logic.describe(Plan(devices=[change]), snapshot))

    async def async_curate(self, *, dry_run: bool = False, force_notify: bool = False) -> Plan:
        """One full pass over the registries."""
        async with self._lock:
            snapshot = self._snapshot()
            plan_ = logic.plan(snapshot, self.options)
            if plan_ and not dry_run:
                self._apply(plan_)
            if plan_ or force_notify:
                _LOGGER.info(
                    "%s%d device(s), %d entity name(s), %d alias(es)",
                    "dry run: " if dry_run else "",
                    len(plan_.devices), len(plan_.entities), len(plan_.aliases),
                )
                if self.options.notify or force_notify:
                    self._notify(logic.describe(plan_, snapshot, dry_run=dry_run))
            return plan_

    @callback
    def _apply(self, plan_: Plan) -> None:
        devices = dr.async_get(self.hass)
        entities = er.async_get(self.hass)
        self._applying = True
        try:
            for change in plan_.devices:
                devices.async_update_device(change.device_id, name_by_user=change.new)
                _LOGGER.info("Device %s: %r -> %r", change.device_id, change.old, change.new)
            for change in plan_.entities:
                entities.async_update_entity(change.entity_id, name=change.new)
                _LOGGER.info("Entity %s: %r -> %r", change.entity_id, change.old, change.new)
            for change in plan_.aliases:
                entry = entities.async_get(change.entity_id)
                if entry is None:
                    continue
                if change.alias in entry.aliases:
                    continue
                entities.async_update_entity(
                    change.entity_id, aliases=[*entry.aliases, change.alias]
                )
                _LOGGER.info("Alias %s += %r", change.entity_id, change.alias)
        finally:
            self._applying = False

    def _notify(self, message: str) -> None:
        persistent_notification.async_create(
            self.hass, message, title=NOTIFICATION_TITLE, notification_id=NOTIFICATION_ID
        )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    curator = Curator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = curator
    curator.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    if not hass.services.has_service(DOMAIN, SERVICE_CURATE):

        async def _curate(call: ServiceCall) -> None:
            for active in list(hass.data[DOMAIN].values()):
                await active.async_curate(dry_run=call.data[ATTR_DRY_RUN], force_notify=True)

        hass.services.async_register(DOMAIN, SERVICE_CURATE, _curate, schema=SERVICE_SCHEMA)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    curator: Curator = hass.data[DOMAIN].pop(entry.entry_id)
    curator.async_stop()
    if not hass.data[DOMAIN]:
        hass.services.async_remove(DOMAIN, SERVICE_CURATE)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
