"""Name Curator.

Keeps device and entity display names free of the room they are already
grouped under. When a device is created or gains an area, or an area is
renamed, its displayed name is checked against the area name (and aliases) and
the repeated prefix is removed — "Dining Room Humidity Sensor" in Dining Room
becomes "Humidity Sensor". Only the ``name_by_user`` of devices and the
``name`` override of entities that do not follow their device are written.

Entity *ids* are a separate, far more dangerous job, and are handled on their
own terms: an id keeps its area prefix (an id is a global identifier, not a
label under a room heading) and is only ever rewritten for the entities of the
single device named in a rename/move event, or in an explicit
``name_curator.curate_ids`` call. There is no house-wide id sweep and there
never will be: a prototype "retroactive" rule flagged 947 of 4016 entities
here, because most integrations mint ids from the name they were given at
pairing time and a later rename never propagated. Rewriting all of those at
once is not a rename, it is an outage.

All decisions live in ``logic.py`` (pure, unit-tested); reference discovery and
rewriting live in ``references.py``; this module gathers registry facts,
applies the plan, and reports it as a persistent notification.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    area_registry as ar,
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import slugify

from . import logic, references
from .logic import Area, Device, Entity, IdChange, Plan, Snapshot

_LOGGER = logging.getLogger(__name__)

DOMAIN = "name_curator"
SERVICE_CURATE = "curate"
SERVICE_CURATE_IDS = "curate_ids"
ATTR_DRY_RUN = "dry_run"
ATTR_DEVICE_ID = "device_id"
ATTR_PREVIOUS_NAME = "previous_name"

NOTIFICATION_ID = "name_curator"
NOTIFICATION_TITLE = "Name curator"
# Assist stores its exposure flag under the conversation domain in entity options.
ASSIST_DOMAIN = "conversation"

# What the prototype house-wide rule would have touched. Quoted back at the
# operator when curate_ids is called without a target, because the number is
# the whole argument for requiring one.
SWEEP_FLAGGED = 947
SWEEP_TOTAL = 4016

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SERVICE_SCHEMA = vol.Schema({vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean})
CURATE_IDS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_DEVICE_ID, default=list): vol.All(cv.ensure_list, [cv.string]),
        # The registry keeps no name history, so a retroactive fix of an id
        # that spells a *former* display name needs the operator to state
        # that name. It is then a stem exactly as an event would supply it.
        vol.Optional(ATTR_PREVIOUS_NAME): cv.string,
        # Defaults to reporting: an id rename cannot be undone from a notification.
        vol.Optional(ATTR_DRY_RUN, default=True): cv.boolean,
    }
)

DEVICE_FIELDS = {"area_id", "name", "name_by_user"}
ENTITY_FIELDS = {"area_id", "name", "device_id", "disabled_by", "entity_category"}
# A device event carrying one of these may have left an id out of date, and
# only the event knows the previous value it was minted from.
SOURCE_FIELDS = {"area_id", "name", "name_by_user"}
# How long the frontend's rename dialog gets to finish its own entity-id
# renames before ids are planned against the registry.
ID_SETTLE_SECONDS = 3

ID_REASON_LABELS = {
    logic.ID_REASON_INTEGRATION_NAME: "id was minted from the integration's own name",
    logic.ID_REASON_STALE_THING: "id still carried a name the device has dropped",
}


@dataclass(frozen=True)
class IdTarget:
    """One device whose entity ids should be re-checked.

    ``previous_names`` and ``previous_area_id`` are what the triggering event
    carried; without them a rename cannot be recognised as one.
    """

    device_id: str
    previous_names: tuple[str | None, ...] = ()
    previous_area_id: str | None = None


@dataclass(frozen=True)
class IdRefusal:
    """An id rename that was planned and then deliberately not applied."""

    entity_id: str
    new_entity_id: str
    why: str


@dataclass
class IdPlan:
    """The id half of a run: what changed, what was refused, what was rewritten."""

    renames: dict[str, str] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    refusals: list[IdRefusal] = field(default_factory=list)
    rewritten: tuple[references.Reference, ...] = ()
    # Rows the rewriter handed back as *not* rewritten: a hand-authored YAML
    # file it re-found, or a write that failed. Reported separately so the
    # notification never claims a file was rewritten when it was not.
    by_hand: tuple[references.Reference, ...] = ()
    # Dry run only: the rewritable references a real run would touch, so the
    # preview shows the blast radius rather than leaving it to be discovered
    # by performing the rename.
    would_rewrite: tuple[references.Reference, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.renames or self.refusals)


def describe_ids(ids: IdPlan, *, dry_run: bool = False) -> str:
    """Markdown for the id half of the notification."""
    verb = "would be" if dry_run else "were"
    lines: list[str] = []
    if ids.renames:
        lines.append(
            f"**Entity ids** ({len(ids.renames)} {verb} renamed; "
            "history and statistics follow)"
        )
        for old, new in ids.renames.items():
            reason = ids.reasons.get(old, "")
            lines.append(f"- `{old}` → **`{new}`** — {ID_REASON_LABELS.get(reason, reason)}")
    if ids.refusals:
        if lines:
            lines.append("")
        lines.append(f"**Entity ids left alone** ({len(ids.refusals)} refused)")
        for refusal in ids.refusals:
            lines.append(f"- `{refusal.entity_id}` ↛ `{refusal.new_entity_id}`: {refusal.why}")
    if ids.rewritten:
        lines.append("")
        counts = Counter(reference.kind for reference in ids.rewritten)
        lines.append(f"**References** ({len(ids.rewritten)} {verb} rewritten)")
        for kind, count in sorted(counts.items()):
            lines.append(f"- {kind}: {count}")
    if ids.would_rewrite:
        lines.append("")
        lines.append(f"**References that would be rewritten** ({len(ids.would_rewrite)})")
        for reference in ids.would_rewrite:
            lines.append(f"- {reference.kind}: {reference.where}")
    if ids.by_hand:
        lines.append("")
        lines.append(f"**References to fix by hand** ({len(ids.by_hand)})")
        for reference in ids.by_hand:
            lines.append(f"- {reference.kind}: {reference.where}")
    return "\n".join(lines)


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
        # Source-change handlers sleep through the settle window; an unload
        # must not leave one to wake on a dead curator and rename anyway.
        self._inflight: set[asyncio.Task[None]] = set()

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
        for task in list(self._inflight):
            task.cancel()
        self._inflight.clear()

    # -- events ------------------------------------------------------------

    @callback
    def _device_event(self, event: Event) -> None:
        if self._applying:
            return
        data = event.data
        if data["action"] == "create":
            self._schedule()
        elif data["action"] == "update" and DEVICE_FIELDS & set(data.get("changes", {})):
            if SOURCE_FIELDS & set(data["changes"]):
                task = self.hass.async_create_task(
                    self._async_handle_source_change(data["device_id"], data["changes"])
                )
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
            self._schedule()

    @callback
    def _entity_event(self, event: Event) -> None:
        if self._applying:
            return
        data = event.data
        if data["action"] == "create":
            # A device rename is not the only way an id ends up room-less.
            # Core mints a new entity's id from the device's *display* name,
            # which this integration has deliberately stripped of the room -
            # so an entity added to a curated device later (a firmware update
            # exposing a new sensor, an integration upgrade) is born as
            # sensor.bluetooth_proxy_… and, across seven identical proxies,
            # collides into …_2 …_6. The device carried no event, so only
            # the entity's own creation can trigger the fix.
            if self.options.rename_entity_ids:
                task = self.hass.async_create_task(self._async_handle_new_entity(data["entity_id"]))
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
            self._schedule()
        elif data["action"] == "update" and ENTITY_FIELDS & set(data.get("changes", {})):
            self._schedule()

    async def _async_handle_new_entity(self, entity_id: str) -> None:
        """Bring a freshly minted entity's id to convention."""
        # Platforms add their entities in a burst; let the whole device land.
        await asyncio.sleep(ID_SETTLE_SECONDS)
        async with self._lock:
            entry = er.async_get(self.hass).async_get(entity_id)
            if entry is None or entry.device_id is None:
                return
            device = dr.async_get(self.hass).async_get(entry.device_id)
            if device is None or not (entry.area_id or device.area_id):
                return
            ids = await self._async_curate_ids([IdTarget(device.id)], dry_run=False)
        if ids and self.options.notify:
            self._report(Plan(), ids, self._snapshot(), dry_run=False)

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
                # Ordered list; Core's COMPUTED_NAME sentinel is not a str.
                aliases=tuple(a for a in entry.aliases if isinstance(a, str)),
                # Stored Assist flag when present; otherwise leave it as a
                # candidate and let async_curate's bounded check decide.
                exposed_to_assist=bool(
                    entry.options.get(ASSIST_DOMAIN, {}).get("should_expose", True)
                ),
            )
        return Snapshot(areas, devices, entities)

    # -- actions -----------------------------------------------------------

    def _area(self, area_id: str | None) -> Area | None:
        entry = ar.async_get(self.hass).async_get_area(area_id) if area_id else None
        return Area(entry.id, entry.name, tuple(entry.aliases)) if entry else None

    async def _async_handle_source_change(self, device_id: str, changes: dict[str, Any]) -> None:
        """React to a device leaving its area, or being renamed.

        Display-name restore/re-derive and entity-id propagation all need the
        *previous* value, which only the event carries, so none of them can
        wait for the debounced sweep.
        """
        change = None
        async with self._lock:
            registry = dr.async_get(self.hass)
            device = registry.async_get(device_id)
            if device is None:
                return

            view = Device(device.id, device.name, device.name_by_user, device.area_id)
            current = self._area(device.area_id)
            if "area_id" in changes:
                change = logic.plan_device_move(
                    view, self._area(changes["area_id"]), current, self.options
                )
            if change is None and "name" in changes:
                change = logic.plan_device_rename(view, changes["name"], current, self.options)
            if change is not None:
                self._applying = True
                try:
                    registry.async_update_device(device.id, name_by_user=change.new)
                finally:
                    self._applying = False
                _LOGGER.info(
                    "Device %s: %r -> %r (%s)",
                    device.id, change.old, change.new or device.name, change.kind,
                )

        ids = IdPlan()
        if self.options.rename_entity_ids:
            # The frontend's rename dialog renames entity ids itself, with
            # separate registry calls right after the device update. Racing
            # it would leave one side erroring on an id the other had already
            # moved, so let it finish, then plan against what is actually in
            # the registry and only fill the gaps it left (ids it could not
            # match, entities minted after the last rename).
            await asyncio.sleep(ID_SETTLE_SECONDS)
            previous_names = (changes.get("name"), changes.get("name_by_user"))
            if change is not None:
                previous_names = (*previous_names, change.old)
            async with self._lock:
                if dr.async_get(self.hass).async_get(device_id) is None:
                    return
                # Ids follow the name the device ends up with, so this runs
                # after the display change above, and for this device only.
                ids = await self._async_curate_ids(
                    [
                        IdTarget(
                            device_id,
                            previous_names,
                            changes.get("area_id", device.area_id),
                        )
                    ],
                    dry_run=False,
                )

        if (change is not None or ids) and self.options.notify:
            plan_ = Plan(devices=[change]) if change is not None else Plan()
            self._report(plan_, ids, self._snapshot(), dry_run=False)

    # -- entity ids --------------------------------------------------------

    @callback
    def _id_stems(self, device: dr.DeviceEntry, target: IdTarget) -> tuple[str, ...]:
        """Slugs this device's object ids could legitimately encode today.

        Every candidate is a name the device demonstrably *has* carried: the
        integration's own name (which is what most platforms mint the id
        from), whatever it was displayed as before this event, what it is
        displayed as now, and — for both the old and the new area — the
        prefix-stripped thing and the conventional "<area> <thing>" form.

        Nothing is inferred. ``plan_entity_id`` only rewrites an id that
        actually starts with one of these, which is the single property that
        keeps the other ~950 legacy ids in this house untouched.
        """
        # Index 0 is the integration-name slug by contract — it is what makes
        # plan_entity_id report ID_REASON_INTEGRATION_NAME — and stays even
        # when the device has no integration name (an empty stem never
        # matches, so it is a placeholder, not a wildcard).
        integration = slugify(device.name) if device.name else ""
        rest: list[str] = []
        areas = (self._area(device.area_id), self._area(target.previous_area_id))
        for name in (*target.previous_names, device.name, device.name_by_user):
            if not name:
                continue
            rest.append(slugify(name))
            for area in areas:
                if area is None:
                    continue
                area_slug = slugify(area.name)
                thing = slugify(logic.strip_prefix(name, area.prefixes) or name)
                rest.append(thing)
                if area_slug or thing:
                    rest.append(logic.compose_object_id(area_slug, thing, ""))
        # dict.fromkeys: dedup, first occurrence wins, order preserved.
        return (integration, *dict.fromkeys(s for s in rest if s and s != integration))

    @callback
    def _plan_ids(self, targets: Sequence[IdTarget]) -> list[IdChange]:
        """Candidate id renames for the given devices, and nothing else."""
        devices = dr.async_get(self.hass)
        entities = er.async_get(self.hass)
        # An id is taken if anything answers to it - registry entries, and the
        # state-machine-only entities (YAML templates and groups without a
        # unique_id) the registry knows nothing about. Landing on either
        # makes the recorder drop the history instead of migrating it.
        registered = frozenset(entities.entities) | frozenset(self.hass.states.async_entity_ids())
        planned: list[IdChange] = []
        claimed: set[str] = set()
        for target in targets:
            device = devices.async_get(target.device_id)
            if device is None:
                _LOGGER.warning("Unknown device %s, skipped", target.device_id)
                continue
            stems = self._id_stems(device, target)
            # The room prefix an id should carry is the *current* one, so the
            # thing is the display name minus a room prefix — either area this
            # event named, and only those two. A device moved from Garage to
            # Basement keeps the display name "Garage Freezer Plug" (that is
            # what restore_on_move is for), but its id should read
            # basement_freezer_plug_power, not basement_garage_freezer_plug_power.
            was = self._area(target.previous_area_id)
            for entry in er.async_entries_for_device(
                entities, target.device_id, include_disabled_entities=True
            ):
                if entry.entity_id.partition(".")[0] in self.options.excluded_domains:
                    continue
                area = self._area(entry.area_id or device.area_id)
                thing = device.name_by_user or device.name or ""
                prefixes = (area.prefixes if area else ()) + (was.prefixes if was else ())
                if prefixes and thing:
                    thing = logic.strip_prefix(thing, prefixes) or thing
                change = logic.plan_entity_id(
                    entry.entity_id,
                    area_slug=slugify(area.name) if area else "",
                    thing_slug=slugify(thing) if thing else "",
                    suffix_slug=slugify(entry.original_name) if entry.original_name else "",
                    stem_slugs=stems,
                    # Anything already registered, plus what an earlier target
                    # in this same run has claimed.
                    taken=registered | claimed,
                )
                if change is None:
                    continue
                planned.append(change)
                claimed.add(change.new_entity_id)
        return planned

    async def _async_curate_ids(
        self, targets: Sequence[IdTarget], *, dry_run: bool
    ) -> IdPlan:
        """Plan, refuse, rewrite references, rename. Caller holds ``_lock``."""
        ids = IdPlan()
        planned = self._plan_ids(targets)
        if not planned:
            return ids

        found = await references.async_find_references(
            self.hass, frozenset(change.entity_id for change in planned)
        )
        for change in planned:
            # Both refusals live here: an id already in use (the recorder
            # would log "Cannot migrate history ... already in use" and drop
            # the history), and a reference nothing can rewrite for us.
            if change.conflict:
                ids.refusals.append(
                    IdRefusal(
                        change.entity_id,
                        change.new_entity_id,
                        "target id is already in use",
                    )
                )
                continue
            blocking = [
                reference
                for reference in found.get(change.entity_id, ())
                if not reference.rewritable
            ]
            if blocking:
                where = ", ".join(reference.where for reference in blocking)
                ids.refusals.append(
                    IdRefusal(
                        change.entity_id,
                        change.new_entity_id,
                        f"referenced where it cannot be rewritten: {where}",
                    )
                )
                continue
            ids.renames[change.entity_id] = change.new_entity_id
            ids.reasons[change.entity_id] = change.reason

        for refusal in ids.refusals:
            _LOGGER.warning(
                "Entity id %s -> %s refused: %s",
                refusal.entity_id, refusal.new_entity_id, refusal.why,
            )
        if dry_run:
            ids.would_rewrite = tuple(
                dict.fromkeys(
                    reference
                    for old in ids.renames
                    for reference in found.get(old, ())
                    if reference.rewritable
                )
            )
            return ids
        if not ids.renames:
            return ids

        # References first: a rewritten reference pointing at an id that does
        # not exist yet is momentarily wrong, whereas a renamed entity with
        # stale references is wrong until someone notices. The veto lives in
        # the pre-check above, not here — by this point files may already be
        # written, so refusing the rename would leave them dangling.
        touched = await references.async_rewrite_references(self.hass, ids.renames)
        ids.rewritten = tuple(r for r in touched if r.rewritable)
        ids.by_hand = tuple(r for r in touched if not r.rewritable)
        for reference in ids.by_hand:
            _LOGGER.warning(
                "Reference not rewritten, fix by hand: %s (%s)",
                reference.where, reference.kind,
            )
        entities = er.async_get(self.hass)
        failed: dict[str, str] = {}
        self._applying = True
        try:
            for old, new in list(ids.renames.items()):
                try:
                    # The recorder listens for this same registry event and
                    # migrates statistics_meta and states_meta itself, so
                    # history follows without us touching the database.
                    entities.async_update_entity(old, new_entity_id=new)
                except KeyError:
                    # Gone from under us - the frontend or another integration
                    # moved it during the settle window; nothing left to do.
                    del ids.renames[old]
                    ids.reasons.pop(old, None)
                    _LOGGER.info("Entity id %s was already renamed by someone else", old)
                except ValueError as err:
                    del ids.renames[old]
                    ids.reasons.pop(old, None)
                    ids.refusals.append(IdRefusal(old, new, f"registry refused it: {err}"))
                    failed[new] = old
                    _LOGGER.error("Entity id %s -> %s failed: %s", old, new, err)
                else:
                    _LOGGER.info("Entity id %s -> %s (%s)", old, new, ids.reasons.get(old))
        finally:
            self._applying = False
        if failed:
            # The references were already pointed at ids that will now never
            # exist. Put them back, so a refused rename leaves every file and
            # entry exactly as it found it.
            restored = await references.async_rewrite_references(self.hass, failed)
            ids.rewritten = tuple(r for r in ids.rewritten if r not in restored)
            _LOGGER.warning(
                "Rolled back %d reference(s) for %d refused rename(s)", len(restored), len(failed)
            )
        return ids

    async def async_curate_ids(
        self, device_ids: Sequence[str], *, dry_run: bool, previous_name: str | None = None
    ) -> IdPlan:
        """The ``curate_ids`` service: retroactive fix for named devices only."""
        async with self._lock:
            ids = await self._async_curate_ids(
                [IdTarget(device_id, (previous_name,)) for device_id in device_ids],
                dry_run=dry_run,
            )
            _LOGGER.info(
                "%s%d entity id(s), %d refused, %d reference(s) rewritten",
                "dry run: " if dry_run else "",
                len(ids.renames), len(ids.refusals), len(ids.rewritten),
            )
            self._report(Plan(), ids, self._snapshot(), dry_run=dry_run)
            return ids

    async def async_curate(self, *, dry_run: bool = False, force_notify: bool = False) -> Plan:
        """One full pass over the registries — display names only.

        Deliberately does not plan entity ids. A sweep has no event to learn
        the previous names from, so it would have to guess which of the 4016
        ids here are stale; the prototype that guessed flagged 947. Ids move
        on a rename event, or through ``curate_ids`` with an explicit device.
        """
        async with self._lock:
            snapshot = self._snapshot()
            plan_ = logic.plan(snapshot, self.options)
            if plan_.aliases:
                # Default-aware check, bounded to entities already in the plan:
                # async_should_expose persists a default for whatever it is asked.
                plan_.aliases = [
                    a for a in plan_.aliases
                    if async_should_expose(self.hass, ASSIST_DOMAIN, a.entity_id)
                ]
            if plan_ and not dry_run:
                self._apply(plan_)
            if plan_ or force_notify:
                _LOGGER.info(
                    "%s%d device(s), %d entity name(s), %d alias(es)",
                    "dry run: " if dry_run else "",
                    len(plan_.devices), len(plan_.entities), len(plan_.aliases),
                )
                if self.options.notify or force_notify:
                    self._report(plan_, IdPlan(), snapshot, dry_run=dry_run)
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

    def _report(
        self, plan_: Plan, ids: IdPlan, snapshot: Snapshot, *, dry_run: bool
    ) -> None:
        """One notification for both halves of a run, under one id."""
        sections = [
            logic.describe(plan_, snapshot, dry_run=dry_run) if plan_ else "",
            describe_ids(ids, dry_run=dry_run),
        ]
        self._notify("\n\n".join(s for s in sections if s) or "Nothing to change.")

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

    if not hass.services.has_service(DOMAIN, SERVICE_CURATE_IDS):

        async def _curate_ids(call: ServiceCall) -> None:
            device_ids: list[str] = call.data[ATTR_DEVICE_ID]
            if not device_ids:
                # The one hard guarantee this integration makes. Without a
                # target there is no event to learn previous names from, so a
                # house-wide run would be the guessing prototype again.
                raise ServiceValidationError(
                    f"{SERVICE_CURATE_IDS} needs at least one device_id. Entity id "
                    "curation is never house-wide: without a rename event to say "
                    "what a device used to be called, the rule has to guess, and "
                    f"the prototype that guessed flagged {SWEEP_FLAGGED} of "
                    f"{SWEEP_TOTAL} entities. Pick the devices you renamed.",
                    translation_domain=DOMAIN,
                    translation_key="curate_ids_needs_target",
                    translation_placeholders={
                        "flagged": str(SWEEP_FLAGGED),
                        "total": str(SWEEP_TOTAL),
                    },
                )
            for active in list(hass.data[DOMAIN].values()):
                await active.async_curate_ids(
                    device_ids,
                    dry_run=call.data[ATTR_DRY_RUN],
                    previous_name=call.data.get(ATTR_PREVIOUS_NAME),
                )

        hass.services.async_register(
            DOMAIN, SERVICE_CURATE_IDS, _curate_ids, schema=CURATE_IDS_SCHEMA
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    curator: Curator = hass.data[DOMAIN].pop(entry.entry_id)
    curator.async_stop()
    if not hass.data[DOMAIN]:
        hass.services.async_remove(DOMAIN, SERVICE_CURATE)
        hass.services.async_remove(DOMAIN, SERVICE_CURATE_IDS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
