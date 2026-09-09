"""Finding and rewriting the places that name an entity_id.

Renaming an ``entity_id`` carries its history along for free -- the recorder
migrates ``states_meta`` and ``statistics_meta`` -- but nothing else follows.
An automation that targeted the old id goes on targeting a name that no longer
exists, and says nothing about it.

So before an id is renamed the caller asks here what points at it. Four
surfaces are covered:

* ``automations.yaml``, ``scripts.yaml``, ``scenes.yaml`` -- machine managed
  by Core's own config API, which does not preserve formatting either, so a
  YAML round-trip loses nothing. Rewritten in place, keeping the previous
  file as a ``.bak`` sibling, then reloaded.
* Lovelace storage dashboards -- rewritten through the dashboard's own save.
  A YAML-mode dashboard is reported and never touched.
* Every other config entry's ``data`` and ``options`` -- helpers, HomeKit
  include filters, ``history_stats``. Rewritten through the config entry.
* ``configuration.yaml``, ``templates.yaml`` and everything under
  ``packages/`` -- the operator's own files, with their comments, anchors and
  deliberate structure. Reported, never rewritten: the caller is expected to
  refuse the rename instead.

Matching is by whole token, never by substring: ``sensor.washer`` does not
match ``sensor.washer_program``, but it does match inside
``{{ states('sensor.washer') }}`` and ``states.sensor.washer.state``. Dict
keys are matched too, because a scene names its members that way. Nothing
here special-cases ``entity_id`` or ``target`` fields: the walk is recursive,
so a service call, a target block, a card and a template are all found the
same way, and a ``device_id`` target is simply never a match.

A file that cannot be parsed is a hit that cannot be rewritten, never a file
without references: the whole-token text scan still decides whether it names
the entity, and if it does the caller refuses the rename.

The walking, matching and rewriting decisions are module-level pure functions
so they can be tested without a Home Assistant runtime. The Home Assistant
and YAML imports live inside the functions that need them, which keeps this
module importable with nothing but the standard library.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Core's config API owns these three, keyed by the domain whose reload service
# picks the rewritten file up.
MANAGED_FILES: dict[str, str] = {
    "automation": "automations.yaml",
    "script": "scripts.yaml",
    "scene": "scenes.yaml",
}
# Never a reference surface: no entity ids live in it, and reading it is not
# something a naming integration should do at all.
SECRETS_FILE = "secrets.yaml"
PACKAGES_DIR = "packages"
YAML_SUFFIXES = (".yaml", ".yml")

LOVELACE_DOMAIN = "lovelace"
LOVELACE_MODE_STORAGE = "storage"

KIND_LOVELACE = "lovelace"
KIND_CONFIG_ENTRY = "config_entry"
KIND_YAML = "yaml"

# Fields that name a thing to an operator, best first.
_LABEL_FIELDS = ("alias", "name", "friendly_name", "id")


@dataclass(frozen=True)
class Reference:
    """One place that names an entity, and whether we can move it ourselves."""

    kind: str
    where: str
    rewritable: bool


# What a surface reports: the ids it names, and the one place that names them.
_Hits = tuple[tuple[frozenset[str], Reference], ...]


# -- matching ---------------------------------------------------------------


@lru_cache(maxsize=256)
def _pattern(ids: tuple[str, ...]) -> re.Pattern[str]:
    """One alternation for ``ids``, guarded so it only matches whole tokens.

    ``\\w`` excludes ``.``, which is what makes ``states.sensor.x.state`` a
    match while ``sensor.x_2`` and ``binary_sensor.x`` are not. Longest first
    so an id that is a prefix of another never wins.
    """
    alternatives = "|".join(re.escape(i) for i in sorted(ids, key=len, reverse=True))
    return re.compile(rf"(?<!\w)({alternatives})(?!\w)")


def token_matches(text: str, entity_id: str) -> bool:
    """Whether ``text`` names ``entity_id`` as a whole token."""
    return _pattern((entity_id,)).search(text) is not None


def matched_tokens(text: str, ids: frozenset[str]) -> frozenset[str]:
    """The ids of ``ids`` that ``text`` names as whole tokens."""
    if not ids:
        return frozenset()
    return frozenset(_pattern(tuple(ids)).findall(text)) & ids


def replace_tokens(text: str, renames: Mapping[str, str]) -> str:
    """Apply ``renames`` to every whole-token id in ``text``.

    One pass, so a rename whose new id is another rename's old id cannot
    cascade, and so a prefix is never mangled the way ``str.replace`` would.
    Returns ``text`` itself when nothing matched.
    """
    if not renames:
        return text
    replaced = _pattern(tuple(renames)).sub(lambda m: renames[m.group(1)], text)
    return text if replaced == text else replaced


def find_in_text(text: str, ids: frozenset[str]) -> dict[str, tuple[int, ...]]:
    """Line numbers, per id, where ``text`` names one of ``ids``.

    Comments count: a rename that would strand a commented-out reference is
    still a rename the operator should look at.
    """
    if not ids:
        return {}
    pattern = _pattern(tuple(ids))
    lines: dict[str, list[int]] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        for entity_id in frozenset(pattern.findall(line)) & ids:
            lines.setdefault(entity_id, []).append(number)
    return {entity_id: tuple(numbers) for entity_id, numbers in lines.items()}


def find_in_structure(obj: Any, ids: frozenset[str]) -> frozenset[str]:
    """The ids of ``ids`` named anywhere in a parsed config structure."""
    if not ids:
        return frozenset()
    found: set[str] = set()
    _collect(obj, ids, found)
    return frozenset(found)


def _collect(obj: Any, ids: frozenset[str], found: set[str]) -> None:
    """Accumulate matches into ``found``, stopping once every id is accounted."""
    if len(found) == len(ids):
        return
    if isinstance(obj, str):
        found |= matched_tokens(obj, ids)
    elif isinstance(obj, Mapping):
        for key, value in obj.items():
            if isinstance(key, str):
                found |= matched_tokens(key, ids)
            _collect(value, ids, found)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _collect(item, ids, found)


def rewrite_structure(obj: Any, renames: Mapping[str, str]) -> Any:
    """``obj`` with every whole-token id replaced, keys included.

    Returns ``obj`` itself -- the same object, not a copy -- when nothing
    matched, so a caller can tell an untouched config from a rewritten one by
    identity and skip writing it.
    """
    if not renames:
        return obj
    if isinstance(obj, str):
        return replace_tokens(obj, renames)
    if isinstance(obj, Mapping):
        rewritten: dict[Any, Any] = {}
        changed = False
        for key, value in obj.items():
            new_key = replace_tokens(key, renames) if isinstance(key, str) else key
            new_value = rewrite_structure(value, renames)
            changed = changed or new_key is not key or new_value is not value
            rewritten[new_key] = new_value
        return rewritten if changed else obj
    if isinstance(obj, (list, tuple)):
        items = [rewrite_structure(item, renames) for item in obj]
        if all(new is old for new, old in zip(items, obj, strict=True)):
            return obj
        return items if isinstance(obj, list) else tuple(items)
    return obj


# -- naming what we found ---------------------------------------------------


def label_for(kind: str, key: str | int | None, item: Any) -> str:
    """How to describe one automation, script or scene to the operator."""
    name: str | None = None
    if isinstance(item, Mapping):
        name = next(
            (
                value
                for field in _LABEL_FIELDS
                if isinstance(value := item.get(field), str) and value
            ),
            None,
        )
    if name is None and isinstance(key, str):
        name = key
    if name is None:
        return f"{kind} #{key + 1}" if isinstance(key, int) else kind
    return f'{kind} "{name}"'


def entries_of(kind: str, data: Any) -> tuple[tuple[str, Any], ...]:
    """The individually nameable entries of a managed file, in file order.

    ``scripts.yaml`` is a mapping of object id to script, the other two are
    lists. Anything else is one unnamed entry, so a hand-mangled file still
    gets reported rather than skipped.
    """
    if isinstance(data, Mapping):
        return tuple((label_for(kind, key, value), value) for key, value in data.items())
    if isinstance(data, list):
        return tuple(
            (label_for(kind, index, item), item) for index, item in enumerate(data)
        )
    return ((label_for(kind, None, data), data),)


def changed_labels(kind: str, before: Any, after: Any) -> tuple[str, ...]:
    """Which entries of a managed file a rewrite actually touched."""
    old = entries_of(kind, before)
    new = entries_of(kind, after)
    return tuple(
        label
        for (label, old_item), (new_label, new_item) in zip(old, new, strict=True)
        if new_item is not old_item or new_label != label
    )


def _where_in_file(name: str, lines: tuple[int, ...]) -> str:
    """An operator-readable spot in a file we will not rewrite."""
    numbers = ", ".join(str(number) for number in lines)
    return f"{name} line {numbers}" if len(lines) == 1 else f"{name} lines {numbers}"


# -- disk, all of it in the executor ----------------------------------------


@dataclass(frozen=True)
class _YamlFile:
    """A config file as it was found: the raw text, and the parse if it worked."""

    text: str
    data: Any
    parsed: bool


def _read_text(path: Path) -> str | None:
    """``path`` as text, or None when it is absent or unreadable."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        _LOGGER.warning("Could not read %s", path)
        return None


def _read_yaml(path: Path) -> _YamlFile | None:
    """Read and parse ``path``; None when it does not exist.

    A parse failure is not an absence: the text comes back with
    ``parsed=False`` so the caller can still refuse a rename it cannot apply.
    """
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.util.yaml import parse_yaml

    text = _read_text(path)
    if text is None:
        return None
    try:
        return _YamlFile(text, parse_yaml(text), True)
    except HomeAssistantError:
        _LOGGER.warning("Could not parse %s; references in it will refuse a rename", path)
        return _YamlFile(text, None, False)


def _write_yaml(path: Path, data: Any) -> bool:
    """Replace ``path`` with ``data``, keeping what was there as ``.bak``.

    Serialises before touching anything, so a config that will not round-trip
    leaves the file alone. False when nothing was written.
    """
    import yaml
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.util.file import write_utf8_file_atomic
    from homeassistant.util.yaml import dump

    try:
        contents = dump(data)
    except yaml.YAMLError:
        _LOGGER.exception("Could not serialise %s", path)
        return False
    try:
        path.with_suffix(f"{path.suffix}.bak").write_bytes(path.read_bytes())
        write_utf8_file_atomic(str(path), contents)
    except (HomeAssistantError, OSError):
        _LOGGER.exception("Could not write %s", path)
        return False
    return True


def _hand_authored_paths(root: Path) -> tuple[Path, ...]:
    """The operator's own YAML: every file in the config root that Core's
    config API does not manage, plus everything under packages/.

    Derived from the directory rather than listed, so a file that appears
    later as a new ``!include`` blocks a rename the day it is created instead
    of the day someone remembers to add it here.
    """
    skip = {*MANAGED_FILES.values(), SECRETS_FILE}
    paths = sorted(
        path
        for path in root.iterdir()
        if path.is_file() and path.suffix in YAML_SUFFIXES and path.name not in skip
    ) if root.is_dir() else []
    packages = root / PACKAGES_DIR
    if packages.is_dir():
        paths.extend(
            sorted(
                path
                for path in packages.rglob("*")
                if path.suffix in YAML_SUFFIXES and path.is_file()
            )
        )
    return tuple(paths)


def _find_managed(config_dir: str, ids: frozenset[str]) -> _Hits:
    """Scan the machine-managed files. Runs in the executor."""
    hits: list[tuple[frozenset[str], Reference]] = []
    for kind, filename in MANAGED_FILES.items():
        path = Path(config_dir) / filename
        file = _read_yaml(path)
        if file is None:
            continue
        if not file.parsed:
            for entity_id, lines in find_in_text(file.text, ids).items():
                where = f"{_where_in_file(filename, lines)} (unparseable)"
                hits.append((frozenset({entity_id}), Reference(kind, where, False)))
            continue
        for label, item in entries_of(kind, file.data):
            if found := find_in_structure(item, ids):
                hits.append((found, Reference(kind, label, True)))
    return tuple(hits)


def _find_hand_authored(config_dir: str, ids: frozenset[str]) -> _Hits:
    """Whole-token scan of the operator's own YAML. Runs in the executor."""
    root = Path(config_dir)
    hits: list[tuple[frozenset[str], Reference]] = []
    for path in _hand_authored_paths(root):
        text = _read_text(path)
        if text is None:
            continue
        name = path.relative_to(root).as_posix()
        for entity_id, lines in find_in_text(text, ids).items():
            reference = Reference(KIND_YAML, _where_in_file(name, lines), False)
            hits.append((frozenset({entity_id}), reference))
    return tuple(hits)


def _rewrite_managed(config_dir: str, renames: dict[str, str]) -> tuple[Reference, ...]:
    """Rewrite the machine-managed files. Runs in the executor."""
    ids = frozenset(renames)
    done: list[Reference] = []
    for kind, filename in MANAGED_FILES.items():
        path = Path(config_dir) / filename
        file = _read_yaml(path)
        if file is None:
            continue
        if not file.parsed:
            for lines in find_in_text(file.text, ids).values():
                where = f"{_where_in_file(filename, lines)} (unparseable)"
                done.append(Reference(kind, where, False))
            continue
        rewritten = rewrite_structure(file.data, renames)
        if rewritten is file.data:
            continue
        labels = changed_labels(kind, file.data, rewritten) or (filename,)
        applied = _write_yaml(path, rewritten)
        done.extend(Reference(kind, label, applied) for label in labels)
    return tuple(done)


# -- Lovelace ---------------------------------------------------------------


def _dashboards(hass: HomeAssistant) -> tuple[tuple[str, Any], ...]:
    """Every storage or YAML dashboard, labelled, from the lovelace integration."""
    data = hass.data.get(LOVELACE_DOMAIN)
    dashboards = getattr(data, "dashboards", None)
    if not dashboards:
        return ()
    labelled = []
    for url_path, dashboard in dashboards.items():
        item = getattr(dashboard, "config", None)
        title = item.get("title") if isinstance(item, Mapping) else None
        name = title or url_path or LOVELACE_DOMAIN
        labelled.append((f'lovelace dashboard "{name}"', dashboard))
    return tuple(labelled)


async def _async_find_lovelace(hass: HomeAssistant, ids: frozenset[str]) -> _Hits:
    """Scan every dashboard's config for ``ids``."""
    from homeassistant.exceptions import HomeAssistantError

    hits: list[tuple[frozenset[str], Reference]] = []
    for label, dashboard in _dashboards(hass):
        try:
            config = await dashboard.async_load(force=False)
        except HomeAssistantError:
            # An auto-generated dashboard has no stored config to reference us.
            continue
        if found := find_in_structure(config, ids):
            storage = getattr(dashboard, "mode", None) == LOVELACE_MODE_STORAGE
            where = label if storage else f"{label} (YAML mode)"
            hits.append((found, Reference(KIND_LOVELACE, where, storage)))
    return tuple(hits)


async def _async_rewrite_lovelace(
    hass: HomeAssistant, renames: dict[str, str]
) -> tuple[Reference, ...]:
    """Save every dashboard whose config named a renamed entity."""
    from homeassistant.exceptions import HomeAssistantError

    done: list[Reference] = []
    for label, dashboard in _dashboards(hass):
        try:
            config = await dashboard.async_load(force=False)
        except HomeAssistantError:
            continue
        rewritten = rewrite_structure(config, renames)
        if rewritten is config:
            continue
        if getattr(dashboard, "mode", None) != LOVELACE_MODE_STORAGE:
            done.append(Reference(KIND_LOVELACE, f"{label} (YAML mode)", False))
            continue
        try:
            await dashboard.async_save(rewritten)
        except HomeAssistantError:
            _LOGGER.exception("Could not save %s", label)
            done.append(Reference(KIND_LOVELACE, label, False))
            continue
        done.append(Reference(KIND_LOVELACE, label, True))
    return tuple(done)


# -- config entries ---------------------------------------------------------


def _entry_label(entry: Any) -> str:
    """Name a config entry the way its card is titled, plus its integration."""
    return f'config entry "{entry.title}" ({entry.domain})'


def _find_config_entries(hass: HomeAssistant, ids: frozenset[str]) -> _Hits:
    """Scan every config entry's data and options."""
    hits: list[tuple[frozenset[str], Reference]] = []
    for entry in hass.config_entries.async_entries():
        found = find_in_structure(entry.data, ids) | find_in_structure(entry.options, ids)
        if found:
            hits.append((found, Reference(KIND_CONFIG_ENTRY, _entry_label(entry), True)))
    return tuple(hits)


def _rewrite_config_entries(
    hass: HomeAssistant, renames: dict[str, str]
) -> tuple[Reference, ...]:
    """Update every config entry whose data or options named a renamed entity."""
    done: list[Reference] = []
    for entry in hass.config_entries.async_entries():
        data = rewrite_structure(entry.data, renames)
        options = rewrite_structure(entry.options, renames)
        if data is entry.data and options is entry.options:
            continue
        hass.config_entries.async_update_entry(entry, data=data, options=options)
        done.append(Reference(KIND_CONFIG_ENTRY, _entry_label(entry), True))
    return tuple(done)


# -- the two things the caller wants ----------------------------------------


async def async_find_references(
    hass: HomeAssistant, entity_ids: frozenset[str]
) -> dict[str, tuple[Reference, ...]]:
    """Every place that names one of ``entity_ids``, keyed by id.

    Ids nothing points at are absent from the result. A reference with
    ``rewritable=False`` is one this integration will not move for the
    operator, and so one an id rename must not proceed past.
    """
    if not entity_ids:
        return {}
    config_dir = hass.config.config_dir
    hits: list[tuple[frozenset[str], Reference]] = []
    hits.extend(await hass.async_add_executor_job(_find_managed, config_dir, entity_ids))
    hits.extend(await _async_find_lovelace(hass, entity_ids))
    hits.extend(_find_config_entries(hass, entity_ids))
    hits.extend(
        await hass.async_add_executor_job(_find_hand_authored, config_dir, entity_ids)
    )

    found: dict[str, dict[Reference, None]] = {}
    for ids, reference in hits:
        for entity_id in sorted(ids):
            found.setdefault(entity_id, {})[reference] = None
    return {entity_id: tuple(references) for entity_id, references in found.items()}


async def async_rewrite_references(
    hass: HomeAssistant, renames: dict[str, str]
) -> tuple[Reference, ...]:
    """Point every rewritable reference at the new id.

    ``renames`` maps old entity_id to new. Returns what was touched: a
    reference with ``rewritable=True`` was moved, one with ``rewritable=False``
    was found and refused, which is what the operator has to fix by hand.
    """
    if not renames:
        return ()
    config_dir = hass.config.config_dir
    done: list[Reference] = []
    done.extend(await hass.async_add_executor_job(_rewrite_managed, config_dir, renames))
    done.extend(await _async_rewrite_lovelace(hass, renames))
    done.extend(_rewrite_config_entries(hass, renames))
    done.extend(
        reference
        for _ids, reference in await hass.async_add_executor_job(
            _find_hand_authored, config_dir, frozenset(renames)
        )
    )

    for domain in {r.kind for r in done if r.rewritable and r.kind in MANAGED_FILES}:
        if hass.services.has_service(domain, "reload"):
            await hass.services.async_call(domain, "reload", blocking=True)
    return tuple(done)
