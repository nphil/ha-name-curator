<img src="custom_components/name_curator/brand/icon.png" width="96" align="right" alt="">

# Name Curator

Stops your dashboards from saying the room twice.

You name a device after where it lives when you pair it — *Dining Room Humidity
Sensor* — because that is the only place the name will be seen for the next five
minutes. Then you assign it to the Dining Room area, and every area-grouped view
reads **Dining Room › Dining Room Humidity Sensor**, and the card truncates it.

Name Curator watches for exactly that moment and shortens the *displayed* name to
**Humidity Sensor**. Entity ids, unique ids, history and statistics never change.

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
- Entity display-name overrides are **one-way**: the previous override is not
  kept, so run `name_curator.curate` with `dry_run: true` first on an existing
  install and keep its report if you may want to revert.
- **Every change is reported** in a persistent notification and logged at INFO.

Prefix matching follows the frontend's own rule (`stripPrefixFromEntityName`):
case-insensitive, separated by a space, `: ` or ` - `, and the remainder's first
word is capitalised unless it already carries a capital (so *IKEA Repeater*
stays *IKEA Repeater*).

### What it never touches

- **Entity ids** — no rename, so no automation, dashboard or statistics breakage.
- **Doors** (`device_class: door`, `garage_door`) — a door reads as
  "*Location* Door" on purpose. Configurable.
- **Automations, scripts and scenes** — their names are deliberate. Configurable.
- Disabled entities and config/diagnostic entities.
- Names without the area prefix. It removes repetition; it does not invent names.

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
| Never shorten these device classes | `door`, `garage_door` | Entities with these classes are left alone |
| Never shorten these domains | `automation`, `script`, `scene` | Entities in these domains are left alone |
| Send a notification for every change | on | Persistent notification per run |
| Wait before acting | 5 s | Debounce after a registry change |

Saving reloads the integration and runs one pass with the new policy.

## Service

`name_curator.curate` runs one full pass over every device and entity with an
area and posts the result as a notification. With `dry_run: true` it only reports
what *would* change — run that first on an existing install.

## Development

Decisions live in `custom_components/name_curator/logic.py` with no Home
Assistant imports; `python -m pytest tests` runs them.

[hacs]: https://github.com/hacs/integration
[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[releases]: https://github.com/nphil/ha-name-curator/releases
[release-badge]: https://img.shields.io/github/v/release/nphil/ha-name-curator
[validate]: https://github.com/nphil/ha-name-curator/actions/workflows/validate.yml
[validate-badge]: https://github.com/nphil/ha-name-curator/actions/workflows/validate.yml/badge.svg
