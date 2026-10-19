"""Export and import of a site's fields and settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.pvoutput_uploader.config_io import (
    ConfigImportError,
    export_config,
    merge_fields,
    parse_config,
    to_yaml,
)

OPTIONS = {
    "fields": [
        {
            "field": "v2", "entity_id": "sensor.pv_power", "constant": None,
            "name": "Power", "active": True, "aggregate": "average",
        },
        {
            "field": "v1", "entity_id": "sensor.today_pv", "constant": None,
            "name": "Energy", "active": True, "aggregate": "energy_today",
        },
        {
            "field": "v5", "entity_id": "sensor.outside", "constant": None,
            "name": "Ambient", "active": False, "aggregate": "average", "source_unit": "°F",
        },
        {
            "field": "v7", "entity_id": None, "constant": None, "name": "Grid",
            "active": True, "aggregate": "average", "direction": "download",
        },
        {
            "field": "b1", "entity_id": "sensor.battery_power", "constant": None,
            "name": "Battery", "active": True, "aggregate": "average",
        },
        {
            "field": "b3", "entity_id": None, "constant": 5544.0, "name": "Size",
            "active": True, "aggregate": "last",
        },
        {
            "field": "b6", "entity_id": None, "constant": 3, "name": "State",
            "active": True, "aggregate": "last",
        },
    ],
    "donor_mode": "donor",
    "upload_interval": "10",
    "retention_days": 120,
    "output_fallback": True,
}


def test_export() -> None:
    config = export_config(OPTIONS)
    assert config["pvoutput_uploader"] == 1
    # In PVOutput's field order, readable keys, no API key or system ID.
    assert [f["field"] for f in config["fields"]] == ["v1", "v2", "v5", "v7", "b1", "b3", "b6"]
    assert config["fields"][0] == {
        "field": "v1", "sensor": "sensor.today_pv", "name": "Energy", "mode": "send",
        "value_per_interval": "energy_today",
    }
    assert config["fields"][2]["mode"] == "record_only"
    assert config["fields"][2]["unit"] == "°F"
    assert config["fields"][3] == {
        "field": "v7", "name": "Grid", "mode": "download", "value_per_interval": "average",
    }
    assert config["fields"][5]["constant"] == 5544
    assert config["settings"] == {
        "account_type": "donor",
        "upload_interval": 10,
        "keep_history_days": 120,
        "daily_output_fallback": True,
    }
    text = to_yaml(config)
    assert "api" not in text.lower()
    assert text.startswith("pvoutput_uploader: 1\n")


def test_round_trip() -> None:
    parsed = parse_config(to_yaml(export_config(OPTIONS)))
    by_field = {m["field"]: m for m in parsed.fields}
    assert by_field["v1"] == {
        "field": "v1", "entity_id": "sensor.today_pv", "constant": None, "name": "Energy",
        "active": True, "aggregate": "energy_today", "direction": "upload",
    }
    assert by_field["v5"]["active"] is False
    assert by_field["v5"]["source_unit"] == "°F"
    assert by_field["v7"]["direction"] == "download"
    assert by_field["v7"]["entity_id"] is None
    assert by_field["b3"]["constant"] == 5544.0
    assert by_field["b6"]["constant"] == 3
    assert parsed.settings == {
        "donor_mode": "donor",
        "upload_interval": "10",
        "retention_days": 120,
        "output_fallback": True,
    }
    assert parsed.entities == [
        "sensor.today_pv", "sensor.pv_power", "sensor.outside", "sensor.battery_power",
    ]


def test_minimal_setup() -> None:
    # A shared setup may give only what matters; the rest takes the defaults.
    parsed = parse_config(
        "pvoutput_uploader: 1\nfields:\n  - field: V2\n    sensor: sensor.pv_power\n"
    )
    assert parsed.fields == [
        {
            "field": "v2", "entity_id": "sensor.pv_power", "constant": None,
            "name": "Power Generation", "active": True, "aggregate": "average",
            "direction": "upload",
        }
    ]
    assert parsed.settings == {}


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("fields: [", "import_not_yaml"),
        ("just text", "import_not_a_config"),
        ("fields: []", "import_not_a_config"),
        ("pvoutput_uploader: 9\nfields: []", "import_newer_format"),
        ("pvoutput_uploader: 1\nfields:\n  - sensor: sensor.x", "import_bad_field"),
        ("pvoutput_uploader: 1\nfields:\n  - field: v99\n    sensor: sensor.x", "import_unknown_field"),
        ("pvoutput_uploader: 1\nfields:\n  - field: v2", "import_bad_value"),
        (
            "pvoutput_uploader: 1\nfields:\n  - field: v2\n    sensor: sensor.x\n    constant: 3",
            "import_bad_value",
        ),
        (
            "pvoutput_uploader: 1\nfields:\n  - field: b1\n    mode: download",
            "import_bad_value",
        ),
        (
            "pvoutput_uploader: 1\nfields:\n  - field: b6\n    constant: 12",
            "import_bad_value",
        ),
        (
            "pvoutput_uploader: 1\nfields:\n  - field: v2\n    sensor: sensor.x\n"
            "    value_per_interval: energy_today",
            "import_bad_value",
        ),
        (
            "pvoutput_uploader: 1\nfields:\n  - field: v2\n    sensor: sensor.x\n"
            "  - field: v2\n    sensor: sensor.y",
            "import_duplicate_field",
        ),
        (
            "pvoutput_uploader: 1\nfields: []\nsettings:\n  keep_history_days: 5",
            "import_bad_value",
        ),
        (
            "pvoutput_uploader: 1\nfields: []\nsettings:\n  upload_interval: 7",
            "import_bad_value",
        ),
        # Quoted, so a string: bool("false") would be True.
        (
            'pvoutput_uploader: 1\nfields: []\nsettings:\n  daily_output_fallback: "false"',
            "import_bad_value",
        ),
        # NaN, infinity and booleans are no fixed value.
        ("pvoutput_uploader: 1\nfields:\n  - field: b3\n    constant: .nan", "import_bad_value"),
        ("pvoutput_uploader: 1\nfields:\n  - field: b3\n    constant: .inf", "import_bad_value"),
        ("pvoutput_uploader: 1\nfields:\n  - field: b3\n    constant: true", "import_bad_value"),
    ],
)
def test_parse_errors(text: str, key: str) -> None:
    with pytest.raises(ConfigImportError) as err:
        parse_config(text)
    assert err.value.key == key


def test_merge_and_replace() -> None:
    current = [{"field": "v2", "entity_id": "sensor.mine"}, {"field": "b1", "entity_id": "sensor.b"}]
    imported = [{"field": "v2", "entity_id": "sensor.theirs"}, {"field": "b2", "entity_id": "sensor.soc"}]
    # Merge: what is configured here stays; only new fields are added.
    merged = merge_fields(current, imported, replace=False)
    assert [(m["field"], m["entity_id"]) for m in merged] == [
        ("v2", "sensor.mine"), ("b1", "sensor.b"), ("b2", "sensor.soc"),
    ]
    # Replace: b2 without b1 is refused (PVOutput ignores battery fields without it).
    with pytest.raises(ConfigImportError) as err:
        merge_fields(current, imported, replace=True)
    assert err.value.key == "import_b1_required"


SETUPS = sorted((Path(__file__).parent.parent / "setups").glob("*.yaml"))


@pytest.mark.parametrize("path", SETUPS, ids=[p.name for p in SETUPS])
def test_shared_setups_parse(path: Path) -> None:
    """Every shared setup in setups/ can be imported."""
    parsed = parse_config(path.read_text(encoding="utf-8"))
    assert parsed.fields
    merge_fields([], parsed.fields, replace=True)


def test_there_are_shared_setups() -> None:
    assert SETUPS
