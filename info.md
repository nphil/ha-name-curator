# Name Curator

Stops area-grouped dashboards from saying the room twice.

Name a device *Dining Room Humidity Sensor* when you pair it; the moment it is
assigned to the Dining Room area, its displayed name becomes **Humidity Sensor**.

- Devices get a user name without the area prefix; the integration's own name
  stays underneath
- Entities that do not follow their device (name overrides, legacy naming) get a
  display-name override without the prefix
- Rename or move a device later and **that device's entity ids follow too**:
  `sensor.living_room_clock_time` → `sensor.living_room_wall_clock_time`. Ids
  keep the area prefix on purpose — an id has no room heading above it
- History and long-term statistics follow the id via Core's recorder;
  automations, scripts, scenes, dashboards and config entries are rewritten,
  and a rename blocked by hand-written YAML or an id already in use is refused
  rather than forced
- Ids are **never** changed on a guess, and never house-wide: only the entities
  of the device named in the event, or of the devices you name in a
  `curate_ids` call. A prototype that guessed flagged 947 of 4016 entities
- Exposed entities keep their old name as an Assist alias
- Doors and automations are left alone by default; every change is reported as
  a notification

Configured entirely in the UI. No YAML.
