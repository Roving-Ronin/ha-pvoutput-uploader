"""Export and import of a site's fields and settings (YAML).

The export holds what is in the site's options: the field mappings and the
settings. The API key and system ID are in the entry's data and are never
part of it, so an export can be shared as it is, e.g. as a ready-made setup
for an inverter or battery integration.

Format (version 1)::

    pvoutput_uploader: 1
    fields:
      - field: v1
        sensor: sensor.today_s_pv_generation   # or: constant: 5544
        name: Energy Generation
        mode: send                             # send, record_only, download
        value_per_interval: energy_today       # average, last, energy_today
        unit: kWh                              # only for sensors without a unit
    settings:
      account_type: auto                       # auto, donor, standard
      upload_interval: auto                    # auto, 5, 10, 15
      keep_history_days: 91
      daily_output_fallback: false
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
import math
from typing import Any

import yaml

from .const import (
    BATTERY_POWER,
    BATTERY_STATE,
    BATTERY_STATE_CODES,
    CONF_DONOR_MODE,
    CONF_FIELDS,
    CONF_OUTPUT_FALLBACK,
    CONF_RETENTION_DAYS,
    CONF_UPLOAD_INTERVAL,
    DEFAULT_RETENTION_DAYS,
    DIRECTION_DOWNLOAD,
    DONOR_AUTO,
    DONOR_NO,
    DONOR_YES,
    FIELDS_BY_KEY,
    INTERVAL_AUTO,
    MAP_ACTIVE,
    MAP_AGGREGATE,
    MAP_CONSTANT,
    MAP_DIRECTION,
    MAP_ENTITY,
    MAP_FIELD,
    MAP_NAME,
    MAP_UNIT,
    MODE_DOWNLOAD,
    MODE_RECORD,
    MODE_SEND,
    UPLOAD_INTERVALS,
    apply_mode,
    downloadable,
)

FORMAT_KEY = "pvoutput_uploader"
FORMAT_VERSION = 1
MODES = (MODE_SEND, MODE_RECORD, MODE_DOWNLOAD)
RETENTION_RANGE = (30, 3650)


class ConfigImportError(Exception):
    """The pasted configuration cannot be imported."""

    def __init__(self, key: str, detail: str = "") -> None:
        """Initialize with a translation key and the detail it names."""
        super().__init__(f"{key}: {detail}")
        self.key = key
        self.detail = detail


@dataclass(slots=True)
class ParsedConfig:
    """A validated configuration, in the options' own format."""

    fields: list[dict[str, Any]]
    settings: dict[str, Any] = dataclass_field(default_factory=dict)

    @property
    def entities(self) -> list[str]:
        """Return the sensors the fields use, in field order, once each."""
        seen: list[str] = []
        for m in self.fields:
            if (entity := m.get(MAP_ENTITY)) and entity not in seen:
                seen.append(entity)
        return seen


def _mode(mapping: dict[str, Any]) -> str:
    if mapping.get(MAP_DIRECTION) == DIRECTION_DOWNLOAD:
        return MODE_DOWNLOAD
    return MODE_SEND if mapping.get(MAP_ACTIVE, True) else MODE_RECORD


def export_config(options: dict[str, Any]) -> dict[str, Any]:
    """Return a site's fields and settings in the export format."""
    order = {key: n for n, key in enumerate(FIELDS_BY_KEY)}
    fields = []
    for m in sorted(options.get(CONF_FIELDS, []), key=lambda m: order[m[MAP_FIELD]]):
        fdef = FIELDS_BY_KEY[m[MAP_FIELD]]
        item: dict[str, Any] = {"field": fdef.key}
        if m.get(MAP_ENTITY):
            item["sensor"] = m[MAP_ENTITY]
        elif m.get(MAP_CONSTANT) is not None:
            constant = m[MAP_CONSTANT]
            item["constant"] = int(constant) if float(constant).is_integer() else constant
        item["name"] = m.get(MAP_NAME) or fdef.name
        item["mode"] = _mode(m)
        item["value_per_interval"] = m.get(MAP_AGGREGATE) or fdef.default_aggregate
        if m.get(MAP_UNIT):
            item["unit"] = m[MAP_UNIT]
        fields.append(item)
    interval = options.get(CONF_UPLOAD_INTERVAL, INTERVAL_AUTO)
    return {
        FORMAT_KEY: FORMAT_VERSION,
        "fields": fields,
        "settings": {
            "account_type": options.get(CONF_DONOR_MODE, DONOR_AUTO),
            "upload_interval": interval if interval == INTERVAL_AUTO else int(interval),
            "keep_history_days": int(options.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS)),
            "daily_output_fallback": bool(options.get(CONF_OUTPUT_FALLBACK, False)),
        },
    }


def to_yaml(config: dict[str, Any]) -> str:
    """Return an exported configuration as YAML."""
    return yaml.safe_dump(config, sort_keys=False, allow_unicode=True, width=100)


def _parse_field(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict) or "field" not in item:
        raise ConfigImportError("import_bad_field", str(item)[:60])
    key = str(item["field"]).lower()
    if (fdef := FIELDS_BY_KEY.get(key)) is None:
        raise ConfigImportError("import_unknown_field", key)
    sensor, constant = item.get("sensor"), item.get("constant")
    mode = str(item.get("mode", MODE_SEND))
    if mode not in MODES:
        raise ConfigImportError("import_bad_value", f"{key} mode: {mode}")
    if sensor and constant is not None:
        raise ConfigImportError("import_bad_value", f"{key}: sensor or constant, not both")
    if not sensor and constant is None and mode != MODE_DOWNLOAD:
        raise ConfigImportError("import_bad_value", f"{key}: needs a sensor or a constant")
    if mode == MODE_DOWNLOAD and not downloadable(key):
        raise ConfigImportError("import_bad_value", f"{key}: battery fields cannot be downloaded")
    if sensor and not (isinstance(sensor, str) and "." in sensor):
        raise ConfigImportError("import_bad_value", f"{key} sensor: {sensor}")
    if constant is not None:
        try:
            number = float(constant)
        except (TypeError, ValueError):
            number = math.nan
        # NaN and infinity (YAML .nan, .inf) are no measurement PVOutput can take.
        if isinstance(constant, bool) or not math.isfinite(number):
            raise ConfigImportError("import_bad_value", f"{key} constant: {constant}")
        constant = number
        if key == BATTERY_STATE:
            if not constant.is_integer() or int(constant) not in BATTERY_STATE_CODES:
                raise ConfigImportError("import_bad_value", f"{key} state: {constant}")
            constant = int(constant)
    aggregate = str(item.get("value_per_interval", fdef.default_aggregate))
    if aggregate not in fdef.aggregates:
        raise ConfigImportError("import_bad_value", f"{key} value_per_interval: {aggregate}")
    mapping: dict[str, Any] = {
        MAP_FIELD: key,
        MAP_ENTITY: sensor or None,
        MAP_CONSTANT: constant,
        MAP_NAME: str(item.get("name") or fdef.name),
        MAP_ACTIVE: True,
        MAP_AGGREGATE: aggregate,
    }
    if unit := item.get("unit"):
        mapping[MAP_UNIT] = str(unit)
    apply_mode(mapping, mode)
    return mapping


def _parse_settings(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigImportError("import_bad_value", "settings")
    settings: dict[str, Any] = {}
    if "account_type" in raw:
        if (value := str(raw["account_type"])) not in (DONOR_AUTO, DONOR_YES, DONOR_NO):
            raise ConfigImportError("import_bad_value", f"account_type: {value}")
        settings[CONF_DONOR_MODE] = value
    if "upload_interval" in raw:
        value = str(raw["upload_interval"])
        if value != INTERVAL_AUTO and value not in {str(m) for m in UPLOAD_INTERVALS}:
            raise ConfigImportError("import_bad_value", f"upload_interval: {value}")
        settings[CONF_UPLOAD_INTERVAL] = value
    if "keep_history_days" in raw:
        try:
            days = int(raw["keep_history_days"])
        except (TypeError, ValueError):
            days = 0
        if not RETENTION_RANGE[0] <= days <= RETENTION_RANGE[1]:
            raise ConfigImportError("import_bad_value", f"keep_history_days: {raw['keep_history_days']}")
        settings[CONF_RETENTION_DAYS] = days
    if "daily_output_fallback" in raw:
        # A YAML true or false only: bool("false") would be True.
        if not isinstance(value := raw["daily_output_fallback"], bool):
            raise ConfigImportError("import_bad_value", f"daily_output_fallback: {value!r}")
        settings[CONF_OUTPUT_FALLBACK] = value
    return settings


def parse_config(text: str) -> ParsedConfig:
    """Parse and check a pasted configuration."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise ConfigImportError("import_not_yaml", str(err).splitlines()[0]) from None
    if not isinstance(data, dict) or FORMAT_KEY not in data:
        raise ConfigImportError("import_not_a_config")
    try:
        version = int(data[FORMAT_KEY])
    except (TypeError, ValueError):
        raise ConfigImportError("import_not_a_config") from None
    if version > FORMAT_VERSION:
        raise ConfigImportError("import_newer_format", str(version))
    raw_fields = data.get("fields") or []
    if not isinstance(raw_fields, list):
        raise ConfigImportError("import_bad_value", "fields")
    fields = [_parse_field(item) for item in raw_fields]
    keys = [m[MAP_FIELD] for m in fields]
    if dupes := sorted({k for k in keys if keys.count(k) > 1}):
        raise ConfigImportError("import_duplicate_field", ", ".join(dupes))
    return ParsedConfig(fields, _parse_settings(data.get("settings")))


def merge_fields(
    current: list[dict[str, Any]], imported: list[dict[str, Any]], replace: bool
) -> list[dict[str, Any]]:
    """Return the fields after an import: replaced, or the new ones added.

    Raises when battery fields b2-b6 would be left without b1.
    """
    if replace:
        fields = list(imported)
    else:
        used = {m[MAP_FIELD] for m in current}
        fields = list(current) + [m for m in imported if m[MAP_FIELD] not in used]
    keys = {m[MAP_FIELD] for m in fields}
    battery = sorted(k for k in keys if FIELDS_BY_KEY[k].battery and k != BATTERY_POWER)
    if battery and BATTERY_POWER not in keys:
        raise ConfigImportError("import_b1_required", ", ".join(battery))
    return fields
