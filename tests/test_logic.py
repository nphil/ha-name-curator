"""Behavior of the naming decisions: what gets shortened, what is left alone."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

import pytest

_SPEC = spec_from_file_location(
    "name_curator_logic",
    Path(__file__).resolve().parents[1] / "custom_components/name_curator/logic.py",
)
logic = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = logic
_SPEC.loader.exec_module(logic)

OPTS = logic.Options()
LIVING = logic.Area("living_room", "Living Room")
OFFICE = logic.Area("nitin_s_office", "Nitin's Office", aliases=("Office",))
KITCHEN = logic.Area("kitchen", "Kitchen")


@pytest.mark.parametrize(
    ("name", "prefix", "expected"),
    [
        ("Living Room Vent Fan", "Living Room", "Vent Fan"),
        ("living room vent fan", "Living Room", "Vent fan"),
        ("Living Room: Vent Fan", "Living Room", "Vent Fan"),
        ("Living Room - Vent Fan", "Living Room", "Vent Fan"),
        ("Nitin's Office AC", "Nitin's Office", "AC"),
        ("Living Room IKEA Repeater", "Living Room", "IKEA Repeater"),
        ("Living Room", "Living Room", None),
        ("Living Room ", "Living Room", None),
        ("Living Roomba", "Living Room", None),
        ("Vent Fan Living Room", "Living Room", None),
        ("Kitchen Lights", "Living Room", None),
    ],
)
def test_strip_prefix(name, prefix, expected):
    assert logic.strip_prefix(name, prefix) == expected


def test_strip_prefix_accepts_area_alias():
    assert logic.strip_prefix("Office Canvas Lights", OFFICE.prefixes) == "Canvas Lights"
    assert logic.strip_prefix("Nitin's Office Printer", OFFICE.prefixes) == "Printer"


def test_device_shortened_via_name_by_user_only():
    device = logic.Device("d1", "Living Room Vent Fan", None, "living_room")
    change = logic.plan_device(device, LIVING, OPTS)
    assert change == logic.DeviceChange("d1", "Living Room Vent Fan", "Vent Fan")


def test_device_user_override_is_what_gets_checked():
    device = logic.Device("d1", "0x00158d0001", "Kitchen Humidity Sensor", "kitchen")
    assert logic.plan_device(device, KITCHEN, OPTS).new == "Humidity Sensor"
    already = logic.Device("d1", "Kitchen Humidity Sensor", "Humidity Sensor", "kitchen")
    assert logic.plan_device(already, KITCHEN, OPTS) is None


def test_device_without_area_or_prefix_untouched():
    assert logic.plan_device(logic.Device("d", "Kitchen Lights", None, None), None, OPTS) is None
    assert logic.plan_device(logic.Device("d", "Kitchen Lights", None, "living_room"), LIVING, OPTS) is None


def test_device_strip_disabled_by_option():
    opts = logic.Options(strip_devices=False)
    assert logic.plan_device(logic.Device("d", "Kitchen Lights", None, "kitchen"), KITCHEN, opts) is None


class TestMove:
    def test_restores_integration_name_when_leaving_area(self):
        device = logic.Device("d", "Living Room Vent Fan", "Vent Fan", None)
        change = logic.plan_device_move(device, LIVING, None, OPTS)
        assert change == logic.DeviceChange("d", "Vent Fan", None, kind="restore")

    def test_restores_when_moved_to_unrelated_area(self):
        device = logic.Device("d", "Living Room Vent Fan", "Vent Fan", "kitchen")
        assert logic.plan_device_move(device, LIVING, KITCHEN, OPTS).kind == "restore"

    def test_leaves_operator_chosen_names_alone(self):
        device = logic.Device("d", "Living Room Vent Fan", "Big Fan", None)
        assert logic.plan_device_move(device, LIVING, None, OPTS) is None

    def test_no_restore_when_new_area_also_prefixes_integration_name(self):
        device = logic.Device("d", "Office Canvas Lights", "Canvas Lights", "nitin_s_office")
        assert logic.plan_device_move(device, OFFICE, OFFICE, OPTS) is None

    def test_option_off(self):
        device = logic.Device("d", "Living Room Vent Fan", "Vent Fan", None)
        assert logic.plan_device_move(device, LIVING, None, logic.Options(restore_on_move=False)) is None


class TestSourceRename:
    def test_follows_integration_rename_with_prefix(self):
        device = logic.Device("d", "Living Room Big Fan", "Vent Fan", "living_room")
        change = logic.plan_device_rename(device, "Living Room Vent Fan", LIVING, OPTS)
        assert change == logic.DeviceChange("d", "Vent Fan", "Big Fan")

    def test_drops_override_when_new_name_has_no_prefix(self):
        device = logic.Device("d", "Big Fan", "Vent Fan", "living_room")
        change = logic.plan_device_rename(device, "Living Room Vent Fan", LIVING, OPTS)
        assert change == logic.DeviceChange("d", "Vent Fan", None, kind="restore")

    def test_operator_override_is_not_ours_so_untouched(self):
        device = logic.Device("d", "Living Room Big Fan", "Couch Fan", "living_room")
        assert logic.plan_device_rename(device, "Living Room Vent Fan", LIVING, OPTS) is None

    def test_noop_when_rename_yields_same_display(self):
        device = logic.Device("d", "Living Room: Vent Fan", "Vent Fan", "living_room")
        assert logic.plan_device_rename(device, "Living Room Vent Fan", LIVING, OPTS) is None


def _entity(**kw):
    base = dict(
        entity_id="light.x", device_id="d1", area_id=None, name=None,
        original_name=None, has_entity_name=True,
    )
    base.update(kw)
    return logic.Entity(**base)


class TestEntity:
    def test_registry_override_is_shortened(self):
        e = _entity(name="Main Hallway Lights")
        area = logic.Area("main_hallway", "Main Hallway")
        assert logic.plan_entity(e, area, OPTS) == logic.EntityChange("light.x", "Main Hallway Lights", "Lights")

    def test_legacy_naming_uses_original_name(self):
        e = _entity(has_entity_name=False, original_name="Kitchen Island Lights")
        assert logic.plan_entity(e, KITCHEN, OPTS).new == "Island Lights"

    def test_derived_name_is_left_to_the_device(self):
        e = _entity(original_name="Temperature")  # has_entity_name, no override
        assert logic.plan_entity(e, KITCHEN, OPTS) is None

    def test_doors_are_never_shortened(self):
        e = _entity(entity_id="binary_sensor.kitchen_door", name="Kitchen Door", device_class="door")
        assert logic.plan_entity(e, KITCHEN, OPTS) is None

    def test_automations_scripts_scenes_never_shortened(self):
        for eid in ("automation.a", "script.s", "scene.c"):
            e = _entity(entity_id=eid, device_id=None, area_id="kitchen", name="Kitchen - Lights")
            assert logic.plan_entity(e, KITCHEN, OPTS) is None
        e = _entity(entity_id="automation.a", device_id=None, area_id="kitchen", name="Kitchen - Lights")
        assert logic.plan_entity(e, KITCHEN, logic.Options(excluded_domains=frozenset())).new == "Lights"

    def test_disabled_and_config_entities_skipped(self):
        assert logic.plan_entity(_entity(name="Kitchen X", disabled=True), KITCHEN, OPTS) is None
        assert logic.plan_entity(_entity(name="Kitchen X", entity_category="config"), KITCHEN, OPTS) is None

    def test_option_off(self):
        e = _entity(name="Kitchen Lights")
        assert logic.plan_entity(e, KITCHEN, logic.Options(strip_entities=False)) is None


def _snapshot(devices, entities, areas=(LIVING, KITCHEN, OFFICE)):
    return logic.Snapshot(
        {a.id: a for a in areas}, {d.id: d for d in devices}, {e.entity_id: e for e in entities}
    )


class TestPlan:
    def test_entity_area_override_beats_device_area(self):
        dev = logic.Device("d1", "Hub", None, "living_room")
        ent = _entity(entity_id="light.k", area_id="kitchen", has_entity_name=False, original_name="Kitchen Lights")
        result = logic.plan(_snapshot([dev], [ent]), OPTS)
        assert [c.new for c in result.entities] == ["Lights"]
        assert result.devices == []

    def test_device_less_entity_with_area(self):
        ent = _entity(entity_id="binary_sensor.g", device_id=None, area_id="kitchen", name="Kitchen Occupancy")
        assert logic.plan(_snapshot([], [ent]), OPTS).entities[0].new == "Occupancy"

    def test_alias_added_for_exposed_entities_whose_friendly_name_changes(self):
        dev = logic.Device("d1", "Living Room Vent Fan", None, "living_room")
        derived = _entity(entity_id="fan.v", friendly_name="Living Room Vent Fan", exposed_to_assist=True)
        unexposed = _entity(entity_id="sensor.t", original_name="Temperature",
                            friendly_name="Living Room Vent Fan Temperature")
        override = _entity(entity_id="light.l", device_id=None, area_id="kitchen", name="Kitchen Lights",
                           friendly_name="Kitchen Lights", exposed_to_assist=True)
        untouched = _entity(entity_id="light.o", device_id="d2", friendly_name="Other", exposed_to_assist=True)
        result = logic.plan(_snapshot([dev, logic.Device("d2", "Other", None, None)],
                                      [derived, unexposed, override, untouched]), OPTS)
        assert {(a.entity_id, a.alias) for a in result.aliases} == {
            ("fan.v", "Living Room Vent Fan"), ("light.l", "Kitchen Lights"),
        }

    def test_existing_alias_not_duplicated_and_option_off(self):
        dev = logic.Device("d1", "Living Room Vent Fan", None, "living_room")
        ent = _entity(entity_id="fan.v", friendly_name="Living Room Vent Fan", exposed_to_assist=True,
                      aliases=("Living Room Vent Fan",))
        assert logic.plan(_snapshot([dev], [ent]), OPTS).aliases == []
        ent2 = _entity(entity_id="fan.v", friendly_name="Living Room Vent Fan", exposed_to_assist=True)
        assert logic.plan(_snapshot([dev], [ent2]), logic.Options(assist_aliases=False)).aliases == []

    def test_idempotent(self):
        dev = logic.Device("d1", "Living Room Vent Fan", None, "living_room")
        snap = _snapshot([dev], [])
        first = logic.plan(snap, OPTS)
        after = _snapshot([logic.Device("d1", "Living Room Vent Fan", first.devices[0].new, "living_room")], [])
        assert not logic.plan(after, OPTS)


class TestComposeObjectId:
    def test_thing_repeating_the_area_collapses_to_one_prefix(self):
        assert (
            logic.compose_object_id("nitin_s_office", "nitin_s_standing_desk", "height")
            == "nitin_s_office_standing_desk_height"
        )

    def test_unrelated_thing_keeps_every_token(self):
        assert logic.compose_object_id("living_room", "vent_fan", "speed") == "living_room_vent_fan_speed"

    def test_collapsing_is_whole_tokens_only(self):
        assert logic.compose_object_id("room", "roomba", "battery") == "room_roomba_battery"

    def test_missing_thing_or_suffix_contributes_nothing(self):
        assert logic.compose_object_id("kitchen", "humidity_sensor", "") == "kitchen_humidity_sensor"
        assert logic.compose_object_id("nitin_s_office", "", "occupancy") == "nitin_s_office_occupancy"
        assert logic.compose_object_id("", "roborock_s8", "battery") == "roborock_s8_battery"

    def test_thing_identical_to_the_area_leaves_the_area_standing(self):
        assert logic.compose_object_id("nitin_s_office", "nitin_s_office", "") == "nitin_s_office"

    def test_refuses_to_mint_an_empty_object_id(self):
        with pytest.raises(ValueError):
            logic.compose_object_id("", "", "")


def _plan_id(
    entity_id,
    *,
    area="nitin_s_office",
    thing="nitin_s_standing_desk",
    suffix="height",
    stems=("uplift_desk",),
    taken=(),
):
    """The standing desk: integration "Uplift Desk", renamed "Nitin's Standing Desk"."""
    return logic.plan_entity_id(
        entity_id,
        area_slug=area,
        thing_slug=thing,
        suffix_slug=suffix,
        stem_slugs=stems,
        taken=frozenset(taken),
    )


class TestPlanEntityId:
    def test_conventional_id_is_left_alone(self):
        assert _plan_id("sensor.nitin_s_office_standing_desk_height") is None

    def test_id_minted_from_the_integration_name_follows_the_rename(self):
        assert _plan_id("sensor.uplift_desk_height") == logic.IdChange(
            "sensor.uplift_desk_height",
            "sensor.nitin_s_office_standing_desk_height",
            logic.ID_REASON_INTEGRATION_NAME,
            False,
        )

    def test_id_spelling_a_dropped_display_name_is_stale_not_integration(self):
        change = _plan_id(
            "sensor.nitin_s_office_sit_stand_desk_height", stems=("uplift_desk", "sit_stand_desk")
        )
        assert change.new_entity_id == "sensor.nitin_s_office_standing_desk_height"
        assert change.reason == logic.ID_REASON_STALE_THING

    def test_unrecognised_id_is_never_guessed_at(self):
        # An id the operator minted by hand. It lacks the area prefix and its
        # tail is the right suffix, which is all the prototype rule looked at
        # before it flagged 947 of 4016 entities. No stem, no rename.
        assert _plan_id("sensor.office_desk_height", stems=("uplift_desk", "nitin_s_standing_desk")) is None
        # A Z-Wave node nobody has ever renamed: same answer.
        assert logic.plan_entity_id(
            "sensor.node_014_air_temperature",
            area_slug="kitchen",
            thing_slug="fridge_sensor",
            suffix_slug="air_temperature",
            stem_slugs=("zooz_zse44",),
            taken=frozenset(),
        ) is None
        # A legacy id that is nothing but its suffix names no device at all.
        assert logic.plan_entity_id(
            "sensor.temperature",
            area_slug="kitchen",
            thing_slug="fridge_sensor",
            suffix_slug="temperature",
            stem_slugs=("zooz_zse44",),
            taken=frozenset(),
        ) is None

    def test_missing_integration_name_matches_nothing(self):
        assert _plan_id("sensor.uplift_desk_height", stems=("",)) is None

    def test_suffix_that_is_not_the_remainder_is_left_alone(self):
        assert _plan_id("sensor.uplift_desk_position") is None

    def test_entity_without_a_name_of_its_own_renames_only_when_nothing_trails(self):
        assert _plan_id("switch.uplift_desk", suffix="") == logic.IdChange(
            "switch.uplift_desk",
            "switch.nitin_s_office_standing_desk",
            logic.ID_REASON_INTEGRATION_NAME,
            False,
        )
        # Dropping "child_lock" would land on the id above and eat its history.
        assert _plan_id("switch.uplift_desk_child_lock", suffix="") is None

    def test_longest_stem_wins_over_a_shorter_one_that_also_fits(self):
        change = _plan_id("sensor.uplift_desk_height", stems=("uplift", "uplift_desk"))
        assert change.new_entity_id == "sensor.nitin_s_office_standing_desk_height"

    def test_taken_target_is_reported_and_flagged_rather_than_dropped(self):
        change = _plan_id(
            "sensor.uplift_desk_height", taken=("sensor.nitin_s_office_standing_desk_height",)
        )
        assert change.conflict is True
        assert change.new_entity_id == "sensor.nitin_s_office_standing_desk_height"

    def test_area_change_recomposes_a_thing_that_did_not_change(self):
        change = logic.plan_entity_id(
            "sensor.living_room_air_purifier_pm2_5",
            area_slug="nitin_s_office",
            thing_slug="air_purifier",
            suffix_slug="pm2_5",
            stem_slugs=("winix_c545", "living_room_air_purifier"),
            taken=frozenset(),
        )
        assert change.new_entity_id == "sensor.nitin_s_office_air_purifier_pm2_5"
        assert change.reason == logic.ID_REASON_STALE_THING

    def test_device_with_no_area_yet_still_gets_its_new_name(self):
        change = _plan_id("sensor.uplift_desk_height", area="")
        assert change.new_entity_id == "sensor.nitin_s_standing_desk_height"

    def test_nothing_to_compose_leaves_the_id_alone(self):
        assert _plan_id("sensor.uplift_desk_height", area="", thing="", suffix="") is None


def test_options_from_mapping_defaults_and_clamps():
    opts = logic.options_from_mapping(None)
    assert opts.excluded_device_classes == {"door", "garage_door"}
    assert opts.excluded_domains == {"automation", "script", "scene"}
    assert opts.debounce_seconds == 5
    opts = logic.options_from_mapping(
        {"excluded_device_classes": [], "excluded_domains": "automation, group", "debounce_seconds": 900.0}
    )
    assert opts.excluded_device_classes == frozenset()
    assert opts.excluded_domains == {"automation", "group"}
    assert opts.debounce_seconds == logic.MAX_DEBOUNCE_SECONDS


def test_describe_lists_every_change():
    snap = _snapshot([logic.Device("d1", "Living Room Vent Fan", None, "living_room")], [])
    p = logic.Plan(
        devices=[logic.DeviceChange("d1", "Living Room Vent Fan", "Vent Fan")],
        entities=[logic.EntityChange("light.k", "Kitchen Lights", "Lights")],
        aliases=[logic.AliasChange("fan.v", "Living Room Vent Fan")],
    )
    text = logic.describe(p, snap, dry_run=True)
    assert "would be" in text and "Living Room Vent Fan → **Vent Fan** · Living Room" in text
    assert "`light.k`: Kitchen Lights → **Lights**" in text and "“Living Room Vent Fan”" in text
    assert logic.describe(logic.Plan(), snap) == "Nothing to change."
