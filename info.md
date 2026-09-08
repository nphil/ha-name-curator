# Name Curator

Stops area-grouped dashboards from saying the room twice.

Name a device *Dining Room Humidity Sensor* when you pair it; the moment it is
assigned to the Dining Room area, its displayed name becomes **Humidity Sensor**.

- Devices get a user name without the area prefix; the integration's own name
  stays underneath
- Entities that do not follow their device (name overrides, legacy naming) get a
  display-name override without the prefix
- Entity ids, history and statistics are never changed
- Exposed entities keep their old name as an Assist alias
- Doors and automations are left alone by default; every change is reported as
  a notification

Configured entirely in the UI. No YAML.
