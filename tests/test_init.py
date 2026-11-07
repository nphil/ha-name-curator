"""Name Curator against a real Core: registries, name composition, child devices.

These run on whichever Home Assistant the harness brings. The expected names
are the same before and after 2026.11; what differs is how Core gets there,
which is exactly what the curator has to get right.
"""

from __future__ import annotations

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.name_curator import DOMAIN, SERVICE_CURATE, names_are_composed

SOURCE = "test_source"


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    """A source integration's entry to own devices, and a running curator."""
    source = MockConfigEntry(domain=SOURCE)
    source.add_to_hass(hass)
    curator = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options={"debounce_seconds": 0})
    curator.add_to_hass(hass)
    assert await hass.config_entries.async_setup(curator.entry_id)
    await hass.async_block_till_done()
    return source


async def _curate(hass: HomeAssistant) -> None:
    await hass.services.async_call(DOMAIN, SERVICE_CURATE, {}, blocking=True)
    await hass.async_block_till_done()


async def test_legacy_named_entity_reads_device_then_part_and_keeps_its_old_name(
    hass: HomeAssistant,
) -> None:
    """A Lutron-style button on a room-prefixed device.

    Whatever Core's naming rules, the result must read "Coffee Table Pico On"
    (never "Coffee Table Pico Living Room Coffee Table Pico On"), and the name
    voice users said before must stay an alias.
    """
    source = await _setup(hass)
    area = ar.async_get(hass).async_create("Living Room")
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=source.entry_id,
        identifiers={(SOURCE, "pico")},
        name="Living Room Coffee Table Pico",
    )
    devices.async_update_device(device.id, area_id=area.id)
    entities = er.async_get(hass)
    button = entities.async_get_or_create(
        "button",
        SOURCE,
        "pico_on",
        config_entry=source,
        device_id=device.id,
        original_name="Living Room Coffee Table Pico On",
        has_entity_name=False,
    )
    async_expose_entity(hass, "conversation", button.entity_id, True)
    before = er.async_get_full_entity_name(hass, entities.async_get(button.entity_id))
    assert before == "Living Room Coffee Table Pico On"

    await _curate(hass)

    assert devices.async_get(device.id).name_by_user == "Coffee Table Pico"
    entry = entities.async_get(button.entity_id)
    assert er.async_get_full_entity_name(hass, entry) == "Coffee Table Pico On"
    assert entry.name == ("On" if names_are_composed() else "Coffee Table Pico On")
    assert before in entry.aliases

    # A second pass finds nothing left to do and adds nothing twice.
    await _curate(hass)
    again = entities.async_get(button.entity_id)
    assert again.name == entry.name
    assert list(again.aliases).count(before) == 1


async def test_unexposed_entity_gets_no_alias(hass: HomeAssistant) -> None:
    source = await _setup(hass)
    area = ar.async_get(hass).async_create("Kitchen")
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=source.entry_id, identifiers={(SOURCE, "plug")}, name="Kitchen Plug"
    )
    devices.async_update_device(device.id, area_id=area.id)
    entities = er.async_get(hass)
    power = entities.async_get_or_create(
        "sensor", SOURCE, "plug_power", config_entry=source, device_id=device.id,
        original_name="Power", has_entity_name=True,
    )
    async_expose_entity(hass, "conversation", power.entity_id, False)

    await _curate(hass)

    entry = entities.async_get(power.entity_id)
    assert er.async_get_full_entity_name(hass, entry) == "Plug Power"
    assert "Kitchen Plug Power" not in entry.aliases


async def test_child_device_is_shortened_under_its_parents_area(hass: HomeAssistant) -> None:
    """Child devices inherit their parent's area and need their own update call."""
    source = await _setup(hass)
    area = ar.async_get(hass).async_create("Dining Room")
    devices = dr.async_get(hass)
    strip = devices.async_get_or_create(
        config_entry_id=source.entry_id, identifiers={(SOURCE, "strip")}, name="Power Strip"
    )
    devices.async_update_device(strip.id, area_id=area.id)
    outlet = devices.async_get_or_create_child(
        config_entry_id=source.entry_id,
        identifiers={(SOURCE, "strip_1")},
        name="Dining Room Lamp",
        parent_device_id=strip.id,
    )
    er.async_get(hass).async_get_or_create(
        "switch", SOURCE, "strip_1", config_entry=source, device_id=outlet.id,
        original_name=None, has_entity_name=True,
    )

    await _curate(hass)

    child = devices.async_get(outlet.id)
    assert isinstance(child, dr.ChildDeviceEntry)
    assert child.name_by_user == "Lamp"
    assert child.area_id is None  # still inherited, not pinned


async def test_new_entity_ids_are_left_to_core(hass: HomeAssistant) -> None:
    """Core mints a new entity's id with its room; the curator does not rename it."""
    source = await _setup(hass)
    area = ar.async_get(hass).async_create("Plant Room")
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=source.entry_id, identifiers={(SOURCE, "proxy")}, name="Plant Room Bluetooth Proxy"
    )
    devices.async_update_device(device.id, area_id=area.id)
    await _curate(hass)
    assert devices.async_get(device.id).name_by_user == "Bluetooth Proxy"

    entities = er.async_get(hass)
    heals = entities.async_get_or_create(
        "sensor", SOURCE, "proxy_heals", config_entry=source, device_id=device.id,
        original_name="Ghost Link Heals", object_id_base="Ghost Link Heals", has_entity_name=True,
    )
    await hass.async_block_till_done()
    assert heals.entity_id == "sensor.plant_room_bluetooth_proxy_ghost_link_heals"
    assert entities.async_get(heals.entity_id) is not None
