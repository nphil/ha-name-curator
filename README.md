<img src="custom_components/name_curator/brand/icon.png" width="96" align="right" alt="">

# Name Curator

Stops your dashboards from saying the room twice.

You name a device after where it lives when you pair it — *Dining Room Humidity
Sensor* — because that is the only place the name will be seen for the next five
minutes. Then you assign it to the Dining Room area, and every area-grouped view
reads **Dining Room › Dining Room Humidity Sensor**, and the card truncates it.

Name Curator watches for exactly that moment and shortens the *displayed* name to
**Humidity Sensor**. Rename a device later and, optionally, the entity ids of
*that device* follow too — with history, statistics and every reference it can
find, or not at all.

[![hacs][hacs-badge]][hacs] [![release][release-badge]][releases] [![validate][validate-badge]][validate]

## What it does

Whenever a device is created, renamed, moved to an area, or an area is renamed:

- **Devices** whose displayed name starts with their area's name (or one of the
  area's aliases) get a user name without that prefix. The integration's own
  name is kept underneath — Zigbee2MQTT, Z-Wave JS or the vendor app still see
  what you typed.
- **Entities** whose displayed name does *not* follow their device — an explicit
  name override, or an integration that never adopted device-based naming
  (Lutron Caséta, for one) — get a name override without the prefix. Entities
  that derive their name from the device follow it automatically.
- **Assist aliases**: for entities exposed to Assist, the old friendly name is
  kept as an alias so "turn on the living room vent fan" keeps working.
- **Restore on move**: if a device Name Curator shortened is later moved out of
  that area, its integration name is put back rather than leaving a bare
  *Vent Fan* with no room attached. Names you chose yourself are never restored.
- **Follows the source**: rename the device in Zigbee2MQTT / ESPHome / the vendor
  app and the shortened display name is re-derived from the new name instead of
  masking it.
- **Entity ids follow a rename** — for the renamed device only, and only when
  the id demonstrably spells out a name that device used to have. See
  [Entity ids](#entity-ids) below; this is the part with teeth, so read it once.
- Entity display-name overrides are **one-way**: the previous override is not
  kept, so run `name_curator.curate` with `dry_run: true` first on an existing
  install and keep its report if you may want to revert.
- **Every change is reported** in a persistent notification and logged at INFO.

Prefix matching follows the frontend's own rule (`stripPrefixFromEntityName`):
case-insensitive, separated by a space, `: ` or ` - `, and the remainder's first
word is capitalised unless it already carries a capital (so *IKEA Repeater*
stays *IKEA Repeater*).

### What it never touches

- **Doors** (`device_class: door`, `garage_door`) — a door reads as
  "*Location* Door" on purpose. Configurable.
- **Automations, scripts and scenes** — their names are deliberate, ids
  included. Configurable.
- Disabled entities and config/diagnostic entities (display names only —
  a disabled entity's id is still kept in step, since nothing depends on it
  being loaded).
- Names without the area prefix. It removes repetition; it does not invent names.
- Unique ids, and the recorder database. See [Entity ids](#entity-ids).

## Entity ids

Display names and entity ids want *opposite* things, which is why they are
handled by different rules.

A display name is shown underneath an area heading, so repeating the room is
noise: **Dining Room › Humidity Sensor**. An entity id is a global identifier
typed into automations and templates with no heading above it, so the room is
the useful part: `sensor.dining_room_humidity`. **Name Curator therefore
shortens display names and keeps the area prefix in ids** — deliberately, not
by omission.

The id it aims for is the area slug, then the device's name with the room
stripped off, then the entity's own suffix, with a repeated leading token
collapsed:

| Area | Device | Entity | Id |
|---|---|---|---|
| Nitin's Office | Nitin's Standing Desk | Height | `sensor.nitin_s_office_standing_desk_height` |
| Living Room | Wall Clock | Time | `sensor.living_room_wall_clock_time` |

(`nitin_s_office` + `nitin_s_standing_desk` collapses to
`nitin_s_office_standing_desk`, not `nitin_s_office_nitin_s_standing_desk`.)

### Only the device you renamed

Id propagation runs when a **device registry update** carries a new `name`,
`name_by_user` or `area_id` — that is, when you rename or move a device in the
UI, or an integration renames it upstream — and it considers **only the
entities of that one device**. `name_curator.curate` (the full pass) never
touches an id at all.

The UI's own rename dialog also offers to rename entity ids, with its own
registry calls right after the device update. The curator waits three seconds
for that to finish, then plans against what is actually in the registry — so
the two never fight, and it only fills the gaps the dialog left: ids the
dialog could not match, and entities minted after the last rename.

### It never guesses

An id is only rewritten when its object id **starts with a slug of a name the
device demonstrably had**: the integration's own name, the name it was
displayed as before this event, its current display name, or the
"*&lt;area&gt; &lt;thing&gt;*" form of any of those, for the area it is in and
the area it just left. Whatever follows that stem must match the entity's own
suffix exactly. No match, or a tail that does not match — no rename.

That rule is the whole safety story, and it is not theoretical. A prototype
that instead asked "does this id look out of date?" flagged **947 of 4016
entities** on the author's install: every Z-Wave node, both robot vacuums, the
washer and the dryer, all because their ids were minted from an integration
name at pairing time and a later rename never propagated. Rewriting 947 ids in
one pass is not a rename, it is an outage. There is no house-wide id sweep, and
`curate_ids` refuses to run without an explicit device for the same reason.

### History and statistics follow; references are checked first

Long-term statistics and recorded history **move with the id on their own**:
Core's recorder listens for the same registry event and migrates
`statistics_meta` and `states_meta` itself. Name Curator does not touch the
database.

References do *not* follow, so every reference to the old id is found *before*
anything is written, and a rename that cannot be made whole is not made at all:

| Surface | Rewritten? |
|---|---|
| Automations, scripts, scenes (`automations.yaml` & co.) | yes — parsed, rewritten with a `.bak` sibling kept, then reloaded. Not if the file will not parse |
| Lovelace dashboards | yes, in storage mode. Not for a YAML-mode dashboard |
| Other integrations' config entries | yes — only entries whose data actually pointed at the old id |
| Hand-written YAML — every other `.yaml` in the config directory (`configuration.yaml`, `templates.yaml`, any `!include` you add later) and `packages/**` | **never.** Your formatting and comments are not ours to rewrite; a rename that would strand one of these is refused and the file and line are reported |

A reference that cannot be rewritten **blocks its rename outright**: the id
stays exactly as it was, and the notification names the file and line so you
can decide. Nothing is renamed half-way.

A rename is also refused when the target id is **already in use**. Letting it
through would make the recorder log *"Cannot migrate history … already in
use"* and silently drop that entity's history, which is exactly the outcome
this integration exists to avoid.

## Installation

### HACS

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/nphil/ha-name-curator`, category **Integration**.
2. Install **Name Curator**, restart Home Assistant.
3. Settings → Devices & services → **Add integration** → *Name Curator*.

### Manual

Copy `custom_components/name_curator/` into your `config/custom_components/`,
restart, then add the integration from the UI.

## Configuration

Everything is in the UI. Settings → Devices & services → Name Curator → **Configure**:

| Option | Default | Meaning |
|---|---|---|
| Shorten device names | on | Strip the area prefix from device display names |
| Shorten entity display names | on | Also handle entities that do not follow their device |
| Restore the full name when a device leaves its area | on | Undo a shortening once the device moves |
| Keep the old name as an Assist alias | on | Preserve voice commands for exposed entities |
| Follow a rename into entity ids | on | Rewrite the renamed device's entity ids too — see [Entity ids](#entity-ids) |
| Never shorten these device classes | `door`, `garage_door` | Entities with these classes are left alone |
| Never shorten these domains | `automation`, `script`, `scene` | Entities in these domains are left alone, ids included |
| Send a notification for every change | on | Persistent notification per run |
| Wait before acting | 5 s | Debounce after a registry change |

Saving reloads the integration and runs one pass with the new policy.

## Services

`name_curator.curate` runs one full pass over every device and entity with an
area and posts the result as a notification. **Display names only** — a full
pass never renames an id. With `dry_run: true` it only reports what *would*
change — run that first on an existing install.

`name_curator.curate_ids` is the retroactive fix, for devices you renamed
before installing this (or with the option off), whose ids still spell out the
old name:

```yaml
action: name_curator.curate_ids
data:
  device_id:
    - 4f9c1e0a2b7d48c3a1e5f60d9b8c7a21
  dry_run: true
```

`dry_run` defaults to **true**, because an id rename cannot be undone from a
notification. `device_id` is required: called without one it raises an error
rather than sweeping the house — see [It never guesses](#it-never-guesses).
Everything else is identical to the event path, refusals included.

## Development

Decisions live in `custom_components/name_curator/logic.py` with no Home
Assistant imports (name shortening, and `plan_entity_id`, which is where the
no-guess rule is actually enforced); reference discovery and rewriting live in
`references.py`. `python -m pytest tests` runs the pure parts.

[hacs]: https://github.com/hacs/integration
[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[releases]: https://github.com/nphil/ha-name-curator/releases
[release-badge]: https://img.shields.io/github/v/release/nphil/ha-name-curator
[validate]: https://github.com/nphil/ha-name-curator/actions/workflows/validate.yml
[validate-badge]: https://github.com/nphil/ha-name-curator/actions/workflows/validate.yml/badge.svg
