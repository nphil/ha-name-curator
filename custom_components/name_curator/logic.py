"""Decisions for Name Curator. Pure functions, no Home Assistant imports.

The house convention names a device after the room it lives in ("Dining Room
Humidity Sensor") because that is what the operator types when pairing it.
Once the device is assigned to that area the prefix only repeats what every
grouped view already shows, so it is stripped from the *displayed* name:

* a device keeps its integration name and gets a ``name_by_user`` override;
* an entity whose displayed name does not follow its device (an explicit name
  override, or an integration that never opted into ``has_entity_name``) gets a
  registry ``name`` override.

Entity ids, unique ids and statistics are never touched. The frontend's own
prefix stripping (``stripPrefixFromEntityName``) defines what counts as a
prefix: the area name followed by " ", ": " or " - ", compared case-insensitively.
Area aliases count too, so an area "Nitin's Office" aliased "Office" also
shortens "Office Canvas Lights".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# Longest first, so "Room - Fan" strips the dash rather than leaving "- Fan".
SEPARATORS = (" - ", ": ", " ")

OPTION_STRIP_DEVICES = "strip_devices"
OPTION_STRIP_ENTITIES = "strip_entities"
OPTION_RESTORE_ON_MOVE = "restore_on_move"
OPTION_ASSIST_ALIASES = "assist_aliases"
OPTION_EXCLUDED_DEVICE_CLASSES = "excluded_device_classes"
OPTION_EXCLUDED_DOMAINS = "excluded_domains"
OPTION_NOTIFY = "notify"
OPTION_DEBOUNCE_SECONDS = "debounce_seconds"

# Doors read as "<Location> Door" by house rule: never shorten them.
DEFAULT_EXCLUDED_DEVICE_CLASSES = ("door", "garage_door")
# Automations, scripts and scenes are named by the operator as "<Area> - <What>"
# on purpose; they are not things in a room.
DEFAULT_EXCLUDED_DOMAINS = ("automation", "script", "scene")
MIN_DEBOUNCE_SECONDS = 0
MAX_DEBOUNCE_SECONDS = 300


@dataclass(frozen=True)
class Options:
    strip_devices: bool = True
    strip_entities: bool = True
    restore_on_move: bool = True
    assist_aliases: bool = True
    excluded_device_classes: frozenset[str] = frozenset(DEFAULT_EXCLUDED_DEVICE_CLASSES)
    excluded_domains: frozenset[str] = frozenset(DEFAULT_EXCLUDED_DOMAINS)
    notify: bool = True
    debounce_seconds: int = 5


def _string_set(raw, key: str, default: tuple[str, ...]) -> frozenset[str]:
    values = raw.get(key, default)
    if isinstance(values, str):
        values = values.split(",")
    return frozenset(v.strip() for v in values if v and v.strip())


def options_from_mapping(raw) -> Options:
    """Effective options from a config entry's ``options`` mapping."""
    raw = raw or {}
    debounce = int(float(raw.get(OPTION_DEBOUNCE_SECONDS, Options.debounce_seconds)))
    return Options(
        strip_devices=bool(raw.get(OPTION_STRIP_DEVICES, True)),
        strip_entities=bool(raw.get(OPTION_STRIP_ENTITIES, True)),
        restore_on_move=bool(raw.get(OPTION_RESTORE_ON_MOVE, True)),
        assist_aliases=bool(raw.get(OPTION_ASSIST_ALIASES, True)),
        excluded_device_classes=_string_set(
            raw, OPTION_EXCLUDED_DEVICE_CLASSES, DEFAULT_EXCLUDED_DEVICE_CLASSES
        ),
        excluded_domains=_string_set(raw, OPTION_EXCLUDED_DOMAINS, DEFAULT_EXCLUDED_DOMAINS),
        notify=bool(raw.get(OPTION_NOTIFY, True)),
        debounce_seconds=min(MAX_DEBOUNCE_SECONDS, max(MIN_DEBOUNCE_SECONDS, debounce)),
    )


@dataclass(frozen=True)
class Area:
    id: str
    name: str
    aliases: tuple[str, ...] = ()

    @property
    def prefixes(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


@dataclass(frozen=True)
class Device:
    id: str
    name: str | None  # what the integration calls it
    name_by_user: str | None
    area_id: str | None

    @property
    def display_name(self) -> str:
        return self.name_by_user or self.name or ""


@dataclass(frozen=True)
class Entity:
    entity_id: str
    device_id: str | None
    area_id: str | None  # the entity's own override, if any
    name: str | None  # registry override
    original_name: str | None
    has_entity_name: bool
    disabled: bool = False
    entity_category: str | None = None
    device_class: str | None = None
    friendly_name: str | None = None  # current state attribute, for the alias
    aliases: tuple[str, ...] = ()
    exposed_to_assist: bool = False

    def follows_device(self) -> bool:
        """Does the friendly name derive from the device name?"""
        return self.has_entity_name and self.name is None

    @property
    def own_display_name(self) -> str | None:
        """The name shown when it does not follow the device."""
        if self.name is not None:
            return self.name
        if not self.has_entity_name:
            return self.original_name
        return None


@dataclass(frozen=True)
class Snapshot:
    areas: dict[str, Area]
    devices: dict[str, Device]
    entities: dict[str, Entity]

    def area_of_entity(self, entity: Entity) -> Area | None:
        area_id = entity.area_id
        if area_id is None and entity.device_id is not None:
            device = self.devices.get(entity.device_id)
            area_id = device.area_id if device else None
        return self.areas.get(area_id) if area_id else None


@dataclass(frozen=True)
class DeviceChange:
    device_id: str
    old: str
    new: str | None  # None restores the integration name
    kind: Literal["strip", "restore"] = "strip"


@dataclass(frozen=True)
class EntityChange:
    entity_id: str
    old: str
    new: str


@dataclass(frozen=True)
class AliasChange:
    entity_id: str
    alias: str


@dataclass
class Plan:
    devices: list[DeviceChange] = field(default_factory=list)
    entities: list[EntityChange] = field(default_factory=list)
    aliases: list[AliasChange] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.devices or self.entities or self.aliases)

    def __len__(self) -> int:
        return len(self.devices) + len(self.entities) + len(self.aliases)


def strip_prefix(name: str, prefixes: tuple[str, ...] | str) -> str | None:
    """Return ``name`` without a leading area prefix, or None if there is none.

    Mirrors the frontend: case-insensitive, one of three separators, and the
    remainder's first word is capitalised unless it already carries a capital.
    """
    if isinstance(prefixes, str):
        prefixes = (prefixes,)
    lowered = name.lower()
    for prefix in prefixes:
        lowered_prefix = prefix.lower()
        if not lowered_prefix:
            continue
        for sep in SEPARATORS:
            head = f"{lowered_prefix}{sep}"
            if not lowered.startswith(head):
                continue
            rest = name[len(head):].strip()
            if not rest:
                continue
            first_word = rest.split(" ", 1)[0]
            if first_word.lower() == first_word:
                rest = rest[0].upper() + rest[1:]
            return rest
    return None


def plan_device(device: Device, area: Area | None, options: Options) -> DeviceChange | None:
    """Shorten a device's displayed name if it repeats its area."""
    if not options.strip_devices or area is None:
        return None
    current = device.display_name
    if not current:
        return None
    stripped = strip_prefix(current, area.prefixes)
    if stripped is None or stripped == current:
        return None
    return DeviceChange(device.id, current, stripped)


def plan_device_move(
    device: Device,
    previous: Area | None,
    current: Area | None,
    options: Options,
) -> DeviceChange | None:
    """Undo a shortening once the device leaves the area that justified it.

    Only names this integration produced are touched: ``name_by_user`` must be
    exactly the integration name minus the previous area's prefix. Moving to an
    area whose prefix the integration name also carries is handled by the
    ordinary strip; anything else restores the integration name so the device
    is not left as a bare "Vent Fan" with no room attached.
    """
    if not options.restore_on_move or previous is None or device.name_by_user is None:
        return None
    if not device.name or device.name_by_user != strip_prefix(device.name, previous.prefixes):
        return None
    if current is not None and strip_prefix(device.name, current.prefixes) is not None:
        return None
    return DeviceChange(device.id, device.name_by_user, None, kind="restore")


def plan_device_rename(
    device: Device, previous_name: str | None, area: Area | None, options: Options
) -> DeviceChange | None:
    """Follow the integration when it renames a device we had shortened.

    ``name_by_user`` masks the integration's ``name``, so a rename done in
    Zigbee2MQTT / ESPHome / the vendor app would otherwise never show. If the
    current user name is exactly what we derived from the *previous* integration
    name, re-derive it from the new one: the new name minus the area prefix, or
    the new name itself when it carries no prefix (drop the override).
    """
    if not options.strip_devices or area is None or device.name_by_user is None:
        return None
    if not previous_name or not device.name or device.name == previous_name:
        return None
    if device.name_by_user != strip_prefix(previous_name, area.prefixes):
        return None
    new = strip_prefix(device.name, area.prefixes)
    if new == device.name_by_user:
        return None
    return DeviceChange(device.id, device.name_by_user, new, kind="strip" if new else "restore")


def plan_entity(entity: Entity, area: Area | None, options: Options) -> EntityChange | None:
    """Shorten an entity's displayed name when it does not follow its device."""
    if not options.strip_entities or area is None:
        return None
    if entity.disabled or entity.entity_category is not None:
        return None
    if entity.device_class in options.excluded_device_classes:
        return None
    if entity.entity_id.partition(".")[0] in options.excluded_domains:
        return None
    current = entity.own_display_name
    if not current:
        return None
    stripped = strip_prefix(current, area.prefixes)
    if stripped is None or stripped == current:
        return None
    return EntityChange(entity.entity_id, current, stripped)


def plan(snapshot: Snapshot, options: Options) -> Plan:
    """Everything that should change in the snapshot, in one pass."""
    result = Plan()
    renamed_devices: set[str] = set()
    for device in snapshot.devices.values():
        change = plan_device(device, snapshot.areas.get(device.area_id or ""), options)
        if change:
            result.devices.append(change)
            renamed_devices.add(device.id)

    for entity in snapshot.entities.values():
        area = snapshot.area_of_entity(entity)
        change = plan_entity(entity, area, options)
        friendly_name_changes = change is not None or (
            entity.follows_device()
            and entity.device_id in renamed_devices
            and not entity.disabled
        )
        if change:
            result.entities.append(change)
        if (
            friendly_name_changes
            and options.assist_aliases
            and entity.exposed_to_assist
            and entity.friendly_name
            and entity.friendly_name not in entity.aliases
        ):
            result.aliases.append(AliasChange(entity.entity_id, entity.friendly_name))
    return result


def describe(plan_: Plan, snapshot: Snapshot, *, dry_run: bool = False) -> str:
    """Markdown for the notification: exactly what changed (or would)."""
    verb = "would be" if dry_run else "were"
    lines: list[str] = []
    if plan_.devices:
        lines.append(f"**Devices** ({len(plan_.devices)} {verb} renamed)")
        for change in plan_.devices:
            device = snapshot.devices.get(change.device_id)
            area = snapshot.areas.get(device.area_id or "") if device else None
            where = f" · {area.name}" if area else ""
            if change.kind == "restore":
                lines.append(f"- {change.old} → *{device.name if device else 'integration name'}* (restored){where}")
            else:
                lines.append(f"- {change.old} → **{change.new}**{where}")
    if plan_.entities:
        lines.append("")
        lines.append(f"**Entities** ({len(plan_.entities)} display names {verb} shortened; ids unchanged)")
        for change in plan_.entities:
            lines.append(f"- `{change.entity_id}`: {change.old} → **{change.new}**")
    if plan_.aliases:
        lines.append("")
        lines.append(f"**Assist aliases** ({len(plan_.aliases)} old names kept for voice)")
        for change in plan_.aliases:
            lines.append(f"- `{change.entity_id}` ← “{change.alias}”")
    if not lines:
        return "Nothing to change."
    return "\n".join(lines)
