"""Behavior of reference hunting: what counts as naming an entity, and what we dare rewrite."""

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

_SPEC = spec_from_file_location(
    "name_curator_references",
    Path(__file__).resolve().parents[1] / "custom_components/name_curator/references.py",
)
references = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = references
_SPEC.loader.exec_module(references)

WASHER = "sensor.washer"
PROGRAM = "sensor.washer_program"
BOTH = frozenset({WASHER, PROGRAM})
LAUNDRY = "sensor.laundry_washer"
RENAMES = {WASHER: LAUNDRY}


class TestTokenMatching:
    """An id is named only as a whole token, whatever punctuation surrounds it."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("sensor.washer", True),
            ("  sensor.washer  ", True),
            ("{{ states('sensor.washer') }}", True),
            ('{{ state_attr("sensor.washer", "x") }}', True),
            ("states.sensor.washer.state", True),
            ("[sensor.washer]", True),
            ("sensor.washer,sensor.dryer", True),
            # The whole point: a longer id that starts with this one.
            ("sensor.washer_program", False),
            ("sensor.washer2", False),
            ("states.sensor.washer_program.state", False),
            # And a longer id that ends with it.
            ("binary_sensor.washer", False),
            ("mysensor.washer", False),
            ("sensor.dryer", False),
            ("", False),
        ],
    )
    def test_whole_token_only(self, text, expected):
        assert references.token_matches(text, WASHER) is expected

    def test_near_miss_ids_do_not_shadow_each_other(self):
        assert references.matched_tokens("sensor.washer_program", BOTH) == frozenset({PROGRAM})
        assert references.matched_tokens("sensor.washer", BOTH) == frozenset({WASHER})
        assert references.matched_tokens("both: sensor.washer sensor.washer_program", BOTH) == BOTH

    def test_nothing_asked_nothing_found(self):
        assert references.matched_tokens("sensor.washer", frozenset()) == frozenset()


class TestFindInStructure:
    """Parsed configs name entities in targets, in templates and in keys."""

    def test_finds_target_entity_id_list(self):
        action = {"action": "homeassistant.turn_on", "target": {"entity_id": [PROGRAM, "light.x"]}}
        assert references.find_in_structure(action, BOTH) == frozenset({PROGRAM})

    def test_finds_bare_entity_id_and_template(self):
        automation = {
            "alias": "Washer done",
            "triggers": [{"trigger": "state", "entity_id": WASHER}],
            "conditions": [{"condition": "template", "value_template": "{{ states('sensor.dryer') }}"}],
            "actions": [{"action": "notify.persistent_notification",
                         "data": {"message": "{{ states.sensor.washer.state }}"}}],
        }
        assert references.find_in_structure(automation, BOTH) == frozenset({WASHER})

    def test_finds_scene_member_keys(self):
        scene = {"name": "Laundry", "entities": {WASHER: "on", "light.x": {"state": "on"}}}
        assert references.find_in_structure(scene, BOTH) == frozenset({WASHER})

    def test_device_targets_are_never_a_match(self):
        action = {"action": "light.turn_on", "target": {"device_id": "abc123"}}
        assert references.find_in_structure(action, BOTH) == frozenset()

    def test_unrelated_config_finds_nothing(self):
        assert references.find_in_structure({"entity_id": ["sensor.dryer"]}, BOTH) == frozenset()


class TestRewriteStructure:
    """Rewriting moves whole tokens and leaves everything else identical."""

    def test_rewrites_target_entity_id_list(self):
        action = {"target": {"entity_id": [WASHER, "light.x"]}}
        assert references.rewrite_structure(action, RENAMES) == {
            "target": {"entity_id": [LAUNDRY, "light.x"]}
        }

    @pytest.mark.parametrize(
        ("template", "expected"),
        [
            ("{{ states('sensor.washer') }}", "{{ states('sensor.laundry_washer') }}"),
            ("states.sensor.washer.state", "states.sensor.laundry_washer.state"),
            ('{{ state_attr("sensor.washer", "x") }}', '{{ state_attr("sensor.laundry_washer", "x") }}'),
            ("{{ is_state('sensor.washer','on') }}", "{{ is_state('sensor.laundry_washer','on') }}"),
            # Adjacent punctuation moves, an adjacent word does not.
            ("sensor.washer_program", "sensor.washer_program"),
            ("binary_sensor.washer", "binary_sensor.washer"),
        ],
    )
    def test_rewrites_templates_by_token(self, template, expected):
        assert references.replace_tokens(template, RENAMES) == expected

    def test_rewrites_scene_member_keys(self):
        scene = {"entities": {WASHER: "on", PROGRAM: "idle"}}
        assert references.rewrite_structure(scene, RENAMES) == {
            "entities": {LAUNDRY: "on", PROGRAM: "idle"}
        }

    def test_untouched_structure_comes_back_unchanged(self):
        config = {"target": {"entity_id": [PROGRAM]}, "data": {"message": "{{ states('sensor.dryer') }}"}}
        assert references.rewrite_structure(config, RENAMES) is config

    def test_no_renames_changes_nothing(self):
        config = {"target": {"entity_id": [WASHER]}}
        assert references.rewrite_structure(config, {}) is config

    def test_only_the_branch_that_matched_is_rebuilt(self):
        untouched = {"entity_id": [PROGRAM]}
        config = {"a": untouched, "b": {"entity_id": [WASHER]}}
        rewritten = references.rewrite_structure(config, RENAMES)
        assert rewritten is not config
        assert rewritten["a"] is untouched

    def test_renames_do_not_cascade(self):
        chained = {"sensor.a": "sensor.b", "sensor.b": "sensor.c"}
        assert references.replace_tokens("sensor.a sensor.b", chained) == "sensor.b sensor.c"

    def test_lists_keep_being_lists(self):
        rewritten = references.rewrite_structure([WASHER], RENAMES)
        assert rewritten == [LAUNDRY]
        assert isinstance(rewritten, list)


class TestLabels:
    """What the operator is shown for the thing that holds the reference."""

    def test_automation_is_named_by_its_alias(self):
        item = {"id": "1699", "alias": "Living Room Clock - time sync watchdog"}
        assert (
            references.label_for("automation", 0, item)
            == 'automation "Living Room Clock - time sync watchdog"'
        )

    def test_falls_back_to_id_then_key_then_position(self):
        assert references.label_for("automation", 3, {"id": "1699"}) == 'automation "1699"'
        assert references.label_for("script", "bedtime", {}) == 'script "bedtime"'
        assert references.label_for("automation", 3, {}) == "automation #4"

    def test_scripts_are_a_mapping_and_scenes_a_list(self):
        scripts = {"bedtime": {"alias": "Bedtime"}, "wake": {}}
        assert references.entries_of("script", scripts) == (
            ('script "Bedtime"', {"alias": "Bedtime"}),
            ('script "wake"', {}),
        )
        scenes = [{"name": "Movie Time"}]
        assert references.entries_of("scene", scenes) == (('scene "Movie Time"', {"name": "Movie Time"}),)

    def test_only_the_rewritten_entry_is_reported(self):
        before = [
            {"alias": "Untouched", "target": {"entity_id": [PROGRAM]}},
            {"alias": "Washer done", "target": {"entity_id": [WASHER]}},
        ]
        after = references.rewrite_structure(before, {WASHER: "sensor.laundry_washer"})
        assert references.changed_labels("automation", before, after) == ('automation "Washer done"',)


class TestHandAuthoredYaml:
    """The operator's own files are reported, with a line number, and never rewritten."""

    def _config(self, tmp_path, **files):
        for name, text in files.items():
            path = tmp_path / name.replace("__", "/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return str(tmp_path)

    def test_hit_is_reported_non_rewritable_with_its_line(self, tmp_path):
        config_dir = self._config(
            tmp_path,
            **{"packages__climate.yaml": "sensor:\n  - platform: history_stats\n    entity_id: sensor.washer\n"},
        )
        hits = references._find_hand_authored(config_dir, BOTH)
        assert hits == (
            (
                frozenset({WASHER}),
                references.Reference("yaml", "packages/climate.yaml line 3", False),
            ),
        )

    def test_every_line_of_a_repeated_hit_is_named(self, tmp_path):
        config_dir = self._config(
            tmp_path, **{"templates.yaml": f"a: {WASHER}\nb: sensor.dryer\nc: {WASHER}\n"}
        )
        (_ids, reference), = references._find_hand_authored(config_dir, BOTH)
        assert reference.where == "templates.yaml lines 1, 3"
        assert reference.rewritable is False

    def test_near_miss_id_is_not_a_hit(self, tmp_path):
        config_dir = self._config(tmp_path, **{"configuration.yaml": f"a: {PROGRAM}\n"})
        assert references._find_hand_authored(config_dir, frozenset({WASHER})) == ()

    def test_absent_files_are_normal(self, tmp_path):
        assert references._find_hand_authored(str(tmp_path), BOTH) == ()

    def test_packages_are_walked_recursively_and_only_for_yaml(self, tmp_path):
        config_dir = self._config(
            tmp_path,
            **{
                "packages__lights__office.yaml": f"a: {WASHER}\n",
                "packages__notes.txt": f"a: {WASHER}\n",
            },
        )
        wheres = [reference.where for _ids, reference in references._find_hand_authored(config_dir, BOTH)]
        assert wheres == ["packages/lights/office.yaml line 1"]

    def test_any_unmanaged_yaml_in_the_root_counts_as_hand_authored(self, tmp_path):
        # A file added later as a new !include must block a rename without
        # anyone remembering to list it; the three managed files and
        # secrets.yaml are the only exemptions.
        config_dir = self._config(
            tmp_path,
            **{
                "sensors.yaml": f"- entity_id: {WASHER}\n",
                "automations.yaml": f"- target: {WASHER}\n",
                "scripts.yaml": f"- target: {WASHER}\n",
                "scenes.yaml": f"- target: {WASHER}\n",
                "secrets.yaml": f"token: {WASHER}\n",
            },
        )
        wheres = [reference.where for _ids, reference in references._find_hand_authored(config_dir, BOTH)]
        assert wheres == ["sensors.yaml line 1"]


def _entry(title, domain, data=None, options=None):
    return SimpleNamespace(title=title, domain=domain, data=data or {}, options=options or {})


class _FakeConfigEntries:
    def __init__(self, entries):
        self._entries = entries
        self.updated = []

    def async_entries(self):
        return self._entries

    def async_update_entry(self, entry, *, data, options):
        self.updated.append((entry.title, data, options))


def _hass(entries=(), lovelace=None):
    return SimpleNamespace(
        config_entries=_FakeConfigEntries(list(entries)),
        data={"lovelace": lovelace} if lovelace is not None else {},
    )


class TestConfigEntries:
    """A HomeKit filter or a helper's source entity is a reference we can move."""

    def test_hit_names_the_entry_title_and_domain(self):
        hass = _hass([_entry("Home", "homekit", options={"filter": {"include_entities": [WASHER]}})])
        assert references._find_config_entries(hass, BOTH) == (
            (frozenset({WASHER}), references.Reference("config_entry", 'config entry "Home" (homekit)', True)),
        )

    def test_data_and_options_are_both_searched(self):
        hass = _hass(
            [
                _entry("Washer runtime", "history_stats", data={"entity_id": WASHER}),
                _entry("Program", "history_stats", options={"entity_id": PROGRAM}),
                _entry("Elsewhere", "tv_inputs", data={"entity_id": "sensor.dryer"}),
            ]
        )
        found = {reference.where: ids for ids, reference in references._find_config_entries(hass, BOTH)}
        assert found == {
            'config entry "Washer runtime" (history_stats)': frozenset({WASHER}),
            'config entry "Program" (history_stats)': frozenset({PROGRAM}),
        }

    def test_only_entries_that_matched_are_updated(self):
        touched = _entry("Home", "homekit", options={"filter": {"include_entities": [WASHER, PROGRAM]}})
        hass = _hass([_entry("Elsewhere", "tv_inputs", data={"entity_id": "sensor.dryer"}), touched])
        done = references._rewrite_config_entries(hass, {WASHER: "sensor.laundry_washer"})
        assert done == (references.Reference("config_entry", 'config entry "Home" (homekit)', True),)
        assert hass.config_entries.updated == [
            ("Home", {}, {"filter": {"include_entities": ["sensor.laundry_washer", PROGRAM]}})
        ]


class TestLovelaceDashboards:
    """Dashboards are reached through the lovelace integration's own data."""

    def _dash(self, url_path, mode="storage", title=None):
        return SimpleNamespace(mode=mode, config={"url_path": url_path, "title": title} if title else None)

    def test_labelled_by_title_then_url_path_then_default(self):
        dashboards = {
            None: self._dash(None),
            "energy": self._dash("energy"),
            "misc": self._dash("misc", title="Bits and Bobs"),
        }
        hass = _hass(lovelace=SimpleNamespace(dashboards=dashboards))
        assert [label for label, _dash in references._dashboards(hass)] == [
            'lovelace dashboard "lovelace"',
            'lovelace dashboard "energy"',
            'lovelace dashboard "Bits and Bobs"',
        ]

    def test_no_lovelace_integration_is_not_an_error(self):
        assert references._dashboards(_hass()) == ()
