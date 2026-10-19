"""Config and options flow for PVOutput Uploader."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, CONF_API_KEY
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
    DateTimeSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)
from homeassistant.util import dt as dt_util

from .api import PVOutputAuthError, PVOutputClient, PVOutputError
from .builder import unit_supported
from .const import (
    AGG_ENERGY_TODAY,
    BATTERY_POWER,
    BATTERY_STATE,
    BATTERY_STATE_CODES,
    CONF_DETECTED_DONOR,
    CONF_DETECTED_INTERVAL,
    CONF_DONOR_MODE,
    CONF_FIELDS,
    CONF_OUTPUT_FALLBACK,
    CONF_RETENTION_DAYS,
    CONF_SYSTEM_ID,
    CONF_SYSTEM_NAME,
    CONF_UPLOAD_INTERVAL,
    DEFAULT_INTERVAL_MINUTES,
    DEFAULT_RETENTION_DAYS,
    DIRECTION_DOWNLOAD,
    DOMAIN,
    DONOR_AUTO,
    DONOR_NO,
    DONOR_YES,
    FIELD_CATALOG,
    FIELD_KEYS,
    FIELDS_BY_KEY,
    INTERVAL_AUTO,
    KIND_ENERGY,
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
    apply_mode,
    downloadable,
    upload_unit,
    UPLOAD_INTERVALS,
)
from .config_io import (
    ConfigImportError,
    ParsedConfig,
    export_config,
    merge_fields,
    parse_config,
    to_yaml,
)
from .services import (
    ATTR_END,
    ATTR_METHOD,
    ATTR_OVERWRITE,
    ATTR_PASSIVE_FIELDS,
    ATTR_START,
    ATTR_UPLOAD,
    ATTR_WITHOUT_BATTERY,
)
from .uploader import REUPLOAD_AUTO, REUPLOAD_OUTPUTS, REUPLOAD_STATUSES, Uploader

CONF_CONFIG = "config"
CONF_IMPORT_MODE = "import_mode"
CONF_IMPORT_SETTINGS = "import_settings"
IMPORT_MERGE = "merge"
IMPORT_REPLACE = "replace"
ATTR_FROM = "from"
FROM_FAILURE = "since_failure"
FROM_TIME = "from_time"

ENTITY_SELECTOR = EntitySelector(EntitySelectorConfig(domain=["sensor", "input_number"]))
CONSTANT_SELECTOR = NumberSelector(
    NumberSelectorConfig(mode=NumberSelectorMode.BOX, step="any")
)

DEFAULT_OPTIONS: dict[str, Any] = {
    CONF_FIELDS: [],
    CONF_DONOR_MODE: DONOR_AUTO,
    CONF_UPLOAD_INTERVAL: INTERVAL_AUTO,
    CONF_RETENTION_DAYS: DEFAULT_RETENTION_DAYS,
    CONF_OUTPUT_FALLBACK: False,
}


class PVOutputUploaderConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the API key and system ID."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the first step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            system_id = str(user_input[CONF_SYSTEM_ID]).strip()
            await self.async_set_unique_id(system_id)
            self._abort_if_unique_id_configured()
            client = PVOutputClient(
                async_get_clientsession(self.hass), user_input[CONF_API_KEY], system_id
            )
            try:
                system = await client.get_system()
            except PVOutputAuthError:
                errors["base"] = "invalid_auth"
            except PVOutputError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=system.name or f"PVOutput {system_id}",
                    data={
                        CONF_API_KEY: user_input[CONF_API_KEY],
                        CONF_SYSTEM_ID: system_id,
                        CONF_SYSTEM_NAME: system.name,
                        CONF_DETECTED_INTERVAL: system.interval,
                        CONF_DETECTED_DONOR: system.donor,
                    },
                    options=deepcopy(DEFAULT_OPTIONS),
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): TextSelector(),
                    vol.Required(CONF_SYSTEM_ID): TextSelector(),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return PVOutputUploaderOptionsFlow()


class PVOutputUploaderOptionsFlow(OptionsFlow):
    """Fields, history actions and settings, picked from a menu.

    Every action saves and closes as soon as it is submitted.
    """

    def __init__(self) -> None:
        """Initialize the flow."""
        self._options: dict[str, Any] = {}
        self._new: dict[str, Any] = {}
        self._edit_field: str | None = None
        # Modes shown on the Fields page, and fields still needing a source.
        self._shown_modes: dict[str, str] = {}
        self._needs_source: list[tuple[str, str]] = []
        # A history download whose start PVOutput has no detail for:
        # (start asked, first day with detail, end).
        self._history: tuple[date, date, date] | None = None
        # An import being applied: the form's input, the parsed configuration,
        # and the sensors it names that this installation does not have.
        self._import_input: dict[str, Any] = {}
        self._import: ParsedConfig | None = None
        self._import_missing: list[str] = []
        self._import_donor_only: list[str] = []

    @property
    def _fields(self) -> list[dict[str, Any]]:
        return self._options[CONF_FIELDS]

    def _donor(self) -> bool:
        mode = self._options.get(CONF_DONOR_MODE, DONOR_AUTO)
        if mode == DONOR_AUTO:
            return bool(self.config_entry.data.get(CONF_DETECTED_DONOR))
        return mode == DONOR_YES

    def _mapping(self, key: str) -> dict[str, Any]:
        return next(m for m in self._fields if m[MAP_FIELD] == key)

    def _field_options(self, keys: list[str]) -> list[SelectOptionDict]:
        return [
            SelectOptionDict(value=k, label=f"{k} · {FIELDS_BY_KEY[k].name}" + (
                f" ({upload_unit(k)})" if upload_unit(k) else ""
            ))
            for k in keys
        ]

    def _used_fields(self) -> list[str]:
        used = {m[MAP_FIELD] for m in self._fields}
        return [f.key for f in FIELD_CATALOG if f.key in used]

    def _free_fields(self) -> list[str]:
        """Return the fields not used yet that the account may send.

        Battery fields b2-b6 are offered once b1 (battery power) is configured:
        PVOutput ignores them without it.
        """
        used = {m[MAP_FIELD] for m in self._fields}
        donor = self._donor()
        return [
            f.key
            for f in FIELD_CATALOG
            if f.key not in used
            and (donor or not f.donor)
            and (not f.battery or f.key == BATTERY_POWER or BATTERY_POWER in used)
        ]

    def _field_selector(self, keys: list[str], mode: SelectSelectorMode) -> SelectSelector:
        return SelectSelector(
            SelectSelectorConfig(options=self._field_options(keys), mode=mode)
        )

    def _describe(self) -> str:
        donor = self._donor()
        lines = []
        for m in sorted(self._fields, key=lambda m: FIELD_KEYS.index(m[MAP_FIELD])):
            field = FIELDS_BY_KEY[m[MAP_FIELD]]
            if m.get(MAP_ENTITY):
                source = f"`{m[MAP_ENTITY]}`, {m.get(MAP_AGGREGATE) or field.default_aggregate}"
            elif m.get(MAP_CONSTANT) is not None:
                source = f"constant {m.get(MAP_CONSTANT)}"
            else:
                source = "no source in Home Assistant"
            unit = upload_unit(field.key, m.get(MAP_AGGREGATE), m.get(MAP_UNIT))
            if unit:
                source += f" {unit}"
            if m.get(MAP_DIRECTION) == DIRECTION_DOWNLOAD:
                state = "download from PVOutput"
            else:
                state = "active" if m.get(MAP_ACTIVE, True) else "passive"
            if field.donor and not donor:
                state += ", needs a donor account"
            lines.append(
                f"- **{field.key}** {m.get(MAP_NAME) or field.name}: {source} ({state})"
            )
        return "\n".join(lines) or "_No fields yet. Start with **Add field**._"

    def _unit_error(self, field: str, entity_id: str, aggregate: str, fallback: str | None = None) -> str | None:
        """Return an error key if the sensor's unit does not fit the field."""
        kind = KIND_ENERGY if aggregate == AGG_ENERGY_TODAY else FIELDS_BY_KEY[field].kind
        state = self.hass.states.get(entity_id)
        unit = (state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) if state else None) or fallback
        if not unit and kind != "raw":
            return "unit_required"
        return None if unit_supported(kind, unit) else "unit_mismatch"

    def _constant_selector(self, field: str) -> NumberSelector | SelectSelector:
        """Label fixed values with the known PVOutput unit; b6 is a list of states."""
        if field == BATTERY_STATE:
            return SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=str(code), label=f"{code} · {label}")
                        for code, label in BATTERY_STATE_CODES.items()
                    ],
                    mode=SelectSelectorMode.LIST,
                )
            )
        config = NumberSelectorConfig(mode=NumberSelectorMode.BOX, step="any")
        if unit := upload_unit(field):
            config["unit_of_measurement"] = unit
        return NumberSelector(config)

    def _details_schema(self, field: str, has_entity: bool) -> dict[Any, Any]:
        """Name, active flag and, for sensors, how values are combined."""
        fdef = FIELDS_BY_KEY[field]
        schema: dict[Any, Any] = {
            vol.Required(MAP_NAME): TextSelector(),
            vol.Required(MAP_ACTIVE): BooleanSelector(),
        }
        if has_entity or (fdef.kind == "raw" and not fdef.battery):
            schema[vol.Optional(MAP_UNIT)] = TextSelector()
        if has_entity and len(fdef.aggregates) > 1:
            schema[vol.Required(MAP_AGGREGATE)] = SelectSelector(
                SelectSelectorConfig(
                    options=list(fdef.aggregates),
                    translation_key="aggregate",
                )
            )
        return schema

    def _save(self) -> ConfigFlowResult:
        return self.async_create_entry(data=self._options)

    def _uploader(self) -> Uploader | None:
        if self.config_entry.state is not ConfigEntryState.LOADED:
            return None
        return self.config_entry.runtime_data.uploader

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the main menu."""
        self._options = deepcopy(DEFAULT_OPTIONS) | deepcopy(dict(self.config_entry.options))
        menu = []
        fields = self._describe()
        if self._free_fields():
            menu.append("add_field")
        else:
            # Nothing left to add: no Add field, and say why.
            fields += "\n\n" + self._all_in_use()
        if self._fields:
            menu += ["edit_field", "fields", "remove_field"]
        menu += [
            "reupload",
            "import_history",
            "download_history",
            "export_config",
            "import_config",
            "settings",
        ]
        detected = self.config_entry.data.get(CONF_DETECTED_INTERVAL, DEFAULT_INTERVAL_MINUTES)
        return self.async_show_menu(
            step_id="init",
            menu_options=menu,
            description_placeholders={
                "fields": fields,
                "account": "donor" if self._donor() else "standard",
                "detected_interval": str(detected),
            },
        )

    # Add

    def _all_in_use(self) -> str:
        """Explain why Add field is not offered."""
        if self._donor():
            return (
                "_Every PVOutput field is configured, so **Add field** is not shown. "
                "Edit or remove a field to change what is sent._"
            )
        return (
            "_Every field a standard account can send (v1-v6) is configured, so "
            "**Add field** is not shown. Extended (v7-v12) and battery (b1-b6) fields "
            "need a PVOutput donor account._"
        )

    def _field_available(self, field: str) -> bool:
        """Check a field against the options as saved now, not as first shown.

        The forms list only free fields, but another Configure window (or a
        stale form) may have added the same field in the meantime.
        """
        self._options = deepcopy(DEFAULT_OPTIONS) | deepcopy(dict(self.config_entry.options))
        return field in self._free_fields()

    def _field_available_any(self) -> bool:
        """Return whether any field can still be added, as saved now."""
        self._options = deepcopy(DEFAULT_OPTIONS) | deepcopy(dict(self.config_entry.options))
        return bool(self._free_fields())

    def _add_field(self, new: dict[str, Any]) -> ConfigFlowResult:
        """Save a new field, unless it was taken meanwhile."""
        if not self._field_available(new[MAP_FIELD]):
            return self.async_abort(
                reason="field_unavailable",
                description_placeholders={"field": new[MAP_FIELD]},
            )
        self._fields.append(new)
        self._new = {}
        return self._save()

    async def async_step_add_field(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose where the value comes from."""
        if not self._field_available_any():
            return self.async_abort(reason="no_free_fields")
        return self.async_show_menu(
            step_id="add_field", menu_options=["add_sensor", "add_constant", "add_download"]
        )

    async def async_step_add_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a sensor and the PVOutput field it feeds."""
        errors: dict[str, str] = {}
        if user_input is not None:
            field = user_input[MAP_FIELD]
            entity_id = user_input[MAP_ENTITY]
            if not self._field_available(field):
                errors[MAP_FIELD] = "field_unavailable"
            # Extended fields start as "average"; energy since midnight is checked later.
            elif (error := self._unit_error(field, entity_id, FIELDS_BY_KEY[field].default_aggregate)) == "unit_mismatch":
                errors["base"] = error
            else:
                self._new = {MAP_FIELD: field, MAP_ENTITY: entity_id, MAP_CONSTANT: None}
                return await self.async_step_sensor_details()
        if not self._field_available_any():
            return self.async_abort(reason="no_free_fields")
        schema = vol.Schema(
            {
                vol.Required(MAP_ENTITY): ENTITY_SELECTOR,
                vol.Required(MAP_FIELD): self._field_selector(
                    self._free_fields(), SelectSelectorMode.DROPDOWN
                ),
            }
        )
        return self.async_show_form(
            step_id="add_sensor",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def async_step_sensor_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name the field and choose whether it is sent."""
        field = self._new[MAP_FIELD]
        fdef = FIELDS_BY_KEY[field]
        errors: dict[str, str] = {}
        if user_input is not None:
            aggregate = user_input.get(MAP_AGGREGATE, fdef.default_aggregate)
            if error := self._unit_error(field, self._new[MAP_ENTITY], aggregate, user_input.get(MAP_UNIT, "").strip() or None):
                errors["base"] = error
            else:
                new = self._new | {
                    MAP_NAME: user_input[MAP_NAME],
                    MAP_ACTIVE: user_input[MAP_ACTIVE],
                    MAP_AGGREGATE: aggregate,
                }
                if unit := user_input.get(MAP_UNIT, "").strip():
                    new[MAP_UNIT] = unit
                return self._add_field(new)
        defaults = user_input or {
            MAP_NAME: fdef.name,
            MAP_ACTIVE: True,
            MAP_AGGREGATE: fdef.default_aggregate,
        }
        return self.async_show_form(
            step_id="sensor_details",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(self._details_schema(field, True)), defaults
            ),
            description_placeholders={
                "field": f"{field} · {fdef.name}",
                "entity": self._new[MAP_ENTITY],
            },
            errors=errors,
        )

    async def async_step_add_constant(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """A fixed value sent with every status, such as the battery size."""
        errors: dict[str, str] = {}
        if user_input is not None:
            field = user_input[MAP_FIELD]
            new = {
                MAP_FIELD: field,
                MAP_ENTITY: None,
                MAP_CONSTANT: user_input.get(MAP_CONSTANT),
                MAP_NAME: user_input.get(MAP_NAME) or FIELDS_BY_KEY[field].name,
                MAP_ACTIVE: user_input[MAP_ACTIVE],
                MAP_AGGREGATE: FIELDS_BY_KEY[field].default_aggregate,
            }
            if unit := user_input.get(MAP_UNIT, "").strip():
                new[MAP_UNIT] = unit
            if not self._field_available(field):
                errors[MAP_FIELD] = "field_unavailable"
            elif field == BATTERY_STATE:
                # The state is picked from a list on the next screen.
                self._new = new
                return await self.async_step_battery_state()
            elif new[MAP_CONSTANT] is None:
                errors[MAP_CONSTANT] = "constant_required"
            else:
                return self._add_field(new)
        if not self._field_available_any():
            return self.async_abort(reason="no_free_fields")
        schema = vol.Schema(
            {
                vol.Required(MAP_FIELD): self._field_selector(
                    self._free_fields(), SelectSelectorMode.DROPDOWN
                ),
                vol.Optional(MAP_CONSTANT): CONSTANT_SELECTOR,
                vol.Optional(MAP_NAME): TextSelector(),
                vol.Optional(MAP_UNIT): TextSelector(),
                vol.Required(MAP_ACTIVE, default=True): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="add_constant",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def async_step_battery_state(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the fixed battery state (b6) from PVOutput's codes."""
        if user_input is not None:
            return self._add_field(
                self._new
                | {MAP_CONSTANT: _constant_value(BATTERY_STATE, user_input[MAP_CONSTANT])}
            )
        return self.async_show_form(
            step_id="battery_state",
            data_schema=vol.Schema(
                {vol.Required(MAP_CONSTANT): self._constant_selector(BATTERY_STATE)}
            ),
        )

    # Edit and remove

    async def async_step_edit_field(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the field to change."""
        if user_input is not None:
            self._edit_field = user_input[MAP_FIELD]
            return await self.async_step_edit_details()
        return self.async_show_form(
            step_id="edit_field",
            data_schema=vol.Schema(
                {
                    vol.Required(MAP_FIELD): self._field_selector(
                        self._used_fields(), SelectSelectorMode.LIST
                    )
                }
            ),
        )

    async def async_step_edit_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change a field, showing the inputs of its kind: sensor or constant."""
        assert self._edit_field is not None
        field = self._edit_field
        fdef = FIELDS_BY_KEY[field]
        mapping = self._mapping(field)
        is_sensor = bool(mapping.get(MAP_ENTITY))
        # A download-only field may be given a sensor or a fixed value (or neither).
        no_source = not _has_source(mapping)
        errors: dict[str, str] = {}
        if user_input is not None:
            aggregate = user_input.get(
                MAP_AGGREGATE, mapping.get(MAP_AGGREGATE) or fdef.default_aggregate
            )
            if no_source:
                is_sensor = bool(user_input.get(MAP_ENTITY))
            if no_source and is_sensor and user_input.get(MAP_CONSTANT) is not None:
                errors["base"] = "entity_or_constant"
            elif is_sensor and (
                error := self._unit_error(field, user_input[MAP_ENTITY], aggregate, user_input.get(MAP_UNIT, "").strip() or None)
            ):
                errors["base"] = error
            else:
                mapping.update(
                    {
                        MAP_NAME: user_input[MAP_NAME],
                        MAP_ACTIVE: user_input[MAP_ACTIVE],
                        MAP_AGGREGATE: aggregate,
                    }
                )
                if unit := user_input.get(MAP_UNIT, "").strip():
                    mapping[MAP_UNIT] = unit
                else:
                    mapping.pop(MAP_UNIT, None)
                if is_sensor:
                    mapping[MAP_ENTITY] = user_input[MAP_ENTITY]
                elif user_input.get(MAP_CONSTANT) is not None:
                    mapping[MAP_CONSTANT] = _constant_value(field, user_input[MAP_CONSTANT])
                return self._save()
        source: dict[Any, Any]
        if no_source:
            source = {
                vol.Optional(MAP_ENTITY): ENTITY_SELECTOR,
                vol.Optional(MAP_CONSTANT): self._constant_selector(field),
            }
        elif is_sensor:
            source = {vol.Required(MAP_ENTITY): ENTITY_SELECTOR}
        else:
            source = {vol.Required(MAP_CONSTANT): self._constant_selector(field)}
        schema = vol.Schema(source | self._details_schema(field, is_sensor or no_source))
        defaults = user_input or {
            k: v for k, v in mapping.items() if v is not None and k != MAP_FIELD
        }
        if field == BATTERY_STATE and not user_input and mapping.get(MAP_CONSTANT) is not None:
            defaults[MAP_CONSTANT] = str(int(mapping[MAP_CONSTANT]))
        return self.async_show_form(
            step_id="edit_details",
            data_schema=self.add_suggested_values_to_schema(schema, defaults),
            description_placeholders={"field": f"{field} · {fdef.name}"},
            errors=errors,
        )

    async def async_step_fields(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Send, Record only or Download for every field, on one page.

        Choosing Send or Record only for a field with no source in Home
        Assistant (added as download only) asks for one next.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            # Only the choices changed on this page are applied, to the
            # configuration as saved now: a field changed elsewhere meanwhile keeps
            # that change.
            changed = {
                field: mode
                for field, mode in user_input.items()
                if mode != self._shown_modes.get(field)
            }
            self._options = deepcopy(DEFAULT_OPTIONS) | deepcopy(dict(self.config_entry.options))
            self._needs_source = []
            # Checked again against the account and fields as saved now: the
            # page may be stale (e.g. donor status changed meanwhile).
            for mapping in self._fields:
                mode = changed.get(mapping[MAP_FIELD])
                if mode is not None and mode not in self._modes_for(mapping):
                    errors[mapping[MAP_FIELD]] = "mode_not_allowed"
            if not errors:
                for mapping in self._fields:
                    mode = changed.get(mapping[MAP_FIELD])
                    if mode is None:
                        continue
                    if mode != MODE_DOWNLOAD and not _has_source(mapping):
                        self._needs_source.append((mapping[MAP_FIELD], mode))
                    else:
                        apply_mode(mapping, mode)
                if self._needs_source:
                    return await self.async_step_field_source()
                return self._save()
        order = {key: n for n, key in enumerate(FIELD_KEYS)}
        fields = sorted(self._fields, key=lambda m: order[m[MAP_FIELD]])
        self._shown_modes = {m[MAP_FIELD]: _mode(m) for m in fields}
        schema: dict[Any, Any] = {}
        for m in fields:
            modes = self._modes_for(m)
            schema[vol.Required(m[MAP_FIELD], default=_mode(m))] = SelectSelector(
                SelectSelectorConfig(
                    options=modes,
                    translation_key="field_mode",
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        return self.async_show_form(
            step_id="fields",
            data_schema=vol.Schema(schema),
            description_placeholders={"fields": self._describe()},
            errors=errors,
        )

    def _modes_for(self, mapping: dict[str, Any]) -> list[str]:
        """Return the modes a field may be set to on the Fields page.

        Send and Record only always (a field without a source gets one next);
        Download for fields PVOutput returns, v7-v12 for donors only. A field
        already set to Download keeps the option, so the page can show it.
        """
        modes = [MODE_SEND, MODE_RECORD]
        fdef = FIELDS_BY_KEY[mapping[MAP_FIELD]]
        if downloadable(fdef.key) and (
            self._donor() or not fdef.donor or _mode(mapping) == MODE_DOWNLOAD
        ):
            modes.append(MODE_DOWNLOAD)
        return modes

    async def async_step_field_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the source of a download-only field set to Send or Record only."""
        field, mode = self._needs_source[0]
        fdef = FIELDS_BY_KEY[field]
        errors: dict[str, str] = {}
        if user_input is not None:
            entity_id = user_input.get(MAP_ENTITY)
            constant = user_input.get(MAP_CONSTANT)
            if bool(entity_id) == (constant is not None):
                errors["base"] = "entity_or_constant"
            elif entity_id and (
                error := self._unit_error(field, entity_id, fdef.default_aggregate)
            ) == "unit_mismatch":
                errors["base"] = error
            else:
                mapping = self._mapping(field)
                if entity_id:
                    mapping[MAP_ENTITY] = entity_id
                else:
                    mapping[MAP_CONSTANT] = _constant_value(field, constant)
                apply_mode(mapping, mode)
                self._needs_source.pop(0)
                if self._needs_source:
                    return await self.async_step_field_source()
                return self._save()
        return self.async_show_form(
            step_id="field_source",
            data_schema=vol.Schema(
                {
                    vol.Optional(MAP_ENTITY): ENTITY_SELECTOR,
                    vol.Optional(MAP_CONSTANT): self._constant_selector(field),
                }
            ),
            description_placeholders={"field": f"{field} · {fdef.name}"},
            errors=errors,
        )

    async def async_step_add_download(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """A field read from PVOutput (no source in Home Assistant needed)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            field = user_input[MAP_FIELD]
            if not self._field_available(field) or not downloadable(field):
                errors[MAP_FIELD] = "field_unavailable"
            else:
                new = {
                    MAP_FIELD: field,
                    MAP_ENTITY: None,
                    MAP_CONSTANT: None,
                    MAP_NAME: user_input.get(MAP_NAME) or FIELDS_BY_KEY[field].name,
                    MAP_ACTIVE: True,
                    MAP_AGGREGATE: FIELDS_BY_KEY[field].default_aggregate,
                    MAP_DIRECTION: DIRECTION_DOWNLOAD,
                }
                if unit := user_input.get(MAP_UNIT, "").strip():
                    new[MAP_UNIT] = unit
                return self._add_field(new)
        free = [f for f in self._free_fields() if downloadable(f)]
        if not free:
            return self.async_abort(reason="no_free_fields")
        schema = vol.Schema(
            {
                vol.Required(MAP_FIELD): self._field_selector(free, SelectSelectorMode.DROPDOWN),
                vol.Optional(MAP_NAME): TextSelector(),
                vol.Optional(MAP_UNIT): TextSelector(),
            }
        )
        return self.async_show_form(
            step_id="add_download",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def async_step_remove_field(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove fields."""
        errors: dict[str, str] = {}
        if user_input is not None:
            drop = set(user_input[CONF_FIELDS])
            keep = [m for m in self._fields if m[MAP_FIELD] not in drop]
            if BATTERY_POWER in drop and any(
                FIELDS_BY_KEY[m[MAP_FIELD]].battery for m in keep
            ):
                errors["base"] = "b1_required"
            else:
                self._options[CONF_FIELDS] = keep
                return self._save()
        return self.async_show_form(
            errors=errors,
            step_id="remove_field",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_FIELDS, default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=self._field_options(self._used_fields()),
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    # History

    async def async_step_reupload(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Send the history log again, since the first failure or from a time."""
        if (uploader := self._uploader()) is None:
            return self.async_abort(reason="not_loaded")
        errors: dict[str, str] = {}
        today = dt_util.now().date()
        first_unsent = await uploader.async_first_unsent()
        if user_input is not None:
            end = (
                _local_datetime(user_input[ATTR_END])
                if user_input.get(ATTR_END)
                else dt_util.now()
            )
            start: datetime | None = None
            if user_input[ATTR_FROM] == FROM_FAILURE:
                if first_unsent is None:
                    errors["base"] = "nothing_unsent"
                else:
                    start = dt_util.as_local(dt_util.utc_from_timestamp(first_unsent))
            elif not user_input.get(ATTR_START):
                errors[ATTR_START] = "start_required"
            else:
                start = _local_datetime(user_input[ATTR_START])
            if start is not None and end < start:
                errors["base"] = "end_before_start"
            if start is not None and not errors:
                result = await uploader.async_reupload(
                    start,
                    end,
                    user_input[ATTR_METHOD],
                    without_battery=user_input.get(ATTR_WITHOUT_BATTERY, False),
                    passive_fields=user_input.get(ATTR_PASSIVE_FIELDS, []),
                )
                await self.config_entry.runtime_data.async_request_refresh()
                return self.async_abort(
                    reason="reupload_queued",
                    description_placeholders={
                        "status_days": str(result["status_days"]),
                        "output_days": str(result["output_days"]),
                    },
                )
        schema = vol.Schema(
            {
                vol.Required(ATTR_FROM): SelectSelector(
                    SelectSelectorConfig(
                        options=[FROM_FAILURE, FROM_TIME],
                        translation_key="reupload_from",
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Optional(ATTR_START): DateTimeSelector(),
                vol.Optional(ATTR_END): DateTimeSelector(),
                vol.Required(ATTR_METHOD): SelectSelector(
                    SelectSelectorConfig(
                        options=[REUPLOAD_AUTO, REUPLOAD_STATUSES, REUPLOAD_OUTPUTS],
                        translation_key="reupload_method",
                    )
                ),
            }
        )
        passive = [m.field for m in uploader.passive_mappings]
        if passive:
            # Passive fields are recorded but not sent; a reupload may send them.
            schema = schema.extend(
                {
                    vol.Optional(ATTR_PASSIVE_FIELDS, default=passive): SelectSelector(
                        SelectSelectorConfig(
                            options=self._field_options(
                                [f.key for f in FIELD_CATALOG if f.key in passive]
                            ),
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            )
        defaults = user_input or {
            ATTR_FROM: FROM_FAILURE if first_unsent is not None else FROM_TIME,
            ATTR_START: (today - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00"),
            ATTR_END: dt_util.now().strftime("%Y-%m-%d %H:%M:00"),
            ATTR_METHOD: REUPLOAD_AUTO,
        }
        summary = uploader.summary
        return self.async_show_form(
            step_id="reupload",
            data_schema=self.add_suggested_values_to_schema(schema, defaults),
            description_placeholders={
                "status_days": str(uploader.status_days),
                "first_unsent": dt_util.as_local(
                    dt_util.utc_from_timestamp(first_unsent)
                ).strftime("%Y-%m-%d %H:%M")
                if first_unsent is not None
                else "none, everything recorded was sent",
                "pending": str(summary.get("pending", 0)),
                "failed": str(summary.get("failed", 0)),
                "partial": str(summary.get("partial", 0)),
                "expired": str(summary.get("expired", 0)),
            },
            errors=errors,
        )

    async def async_step_import_history(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Fill the history log from Home Assistant's recorder."""
        if (uploader := self._uploader()) is None:
            return self.async_abort(reason="not_loaded")
        errors: dict[str, str] = {}
        now = dt_util.now()
        if user_input is not None:
            start = _local_datetime(user_input[ATTR_START])
            end = _local_datetime(user_input[ATTR_END]) if user_input.get(ATTR_END) else now
            if end <= start:
                errors["base"] = "end_before_start"
            else:
                result = await uploader.async_import(
                    start,
                    end,
                    user_input[ATTR_OVERWRITE],
                    user_input[ATTR_UPLOAD],
                    passive_fields=user_input.get(ATTR_PASSIVE_FIELDS, []),
                )
                await self.config_entry.runtime_data.async_request_refresh()
                return self.async_abort(
                    reason="import_done",
                    description_placeholders={
                        "from_states": str(result["from_states"]),
                        "from_statistics": str(result["from_statistics"]),
                    },
                )
        schema = vol.Schema(
            {
                vol.Required(ATTR_START): DateTimeSelector(),
                vol.Optional(ATTR_END): DateTimeSelector(),
                vol.Required(ATTR_OVERWRITE, default=False): BooleanSelector(),
                vol.Required(ATTR_UPLOAD, default=False): BooleanSelector(),
            }
        )
        passive = [m.field for m in uploader.passive_mappings]
        if passive:
            # Sent with the import (when Send to PVOutput is on), for its period only.
            schema = schema.extend(
                {
                    vol.Optional(ATTR_PASSIVE_FIELDS, default=passive): SelectSelector(
                        SelectSelectorConfig(
                            options=self._field_options(
                                [f.key for f in FIELD_CATALOG if f.key in passive]
                            ),
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            )
        defaults = user_input or {
            ATTR_START: (now - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00"),
        }
        return self.async_show_form(
            step_id="import_history",
            data_schema=self.add_suggested_values_to_schema(schema, defaults),
            errors=errors,
        )

    # Settings

    # Export and import of fields and settings

    async def async_step_export_config(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the fields and settings as YAML, to copy (no API key or system ID)."""
        if user_input is not None:
            return self.async_abort(reason="export_closed")
        text = to_yaml(export_config(self._options))
        schema = vol.Schema(
            {vol.Optional(CONF_CONFIG): TextSelector(TextSelectorConfig(multiline=True))}
        )
        return self.async_show_form(
            step_id="export_config",
            data_schema=self.add_suggested_values_to_schema(schema, {CONF_CONFIG: text}),
            description_placeholders={"count": str(len(self._fields))},
        )

    def _import_form(
        self, errors: dict[str, str] | None = None, detail: str = ""
    ) -> ConfigFlowResult:
        schema = vol.Schema(
            {
                vol.Required(CONF_CONFIG): TextSelector(TextSelectorConfig(multiline=True)),
                vol.Required(CONF_IMPORT_MODE, default=IMPORT_MERGE): SelectSelector(
                    SelectSelectorConfig(
                        options=[IMPORT_MERGE, IMPORT_REPLACE],
                        translation_key="import_mode",
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(CONF_IMPORT_SETTINGS, default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="import_config",
            data_schema=self.add_suggested_values_to_schema(schema, self._import_input),
            description_placeholders={"detail": detail},
            errors=errors or {},
        )

    async def async_step_import_config(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Paste an exported configuration (e.g. a shared setup) to use it here."""
        if user_input is None:
            return self._import_form()
        self._import_input = dict(user_input)
        try:
            parsed = parse_config(user_input[CONF_CONFIG])
        except ConfigImportError as err:
            return self._import_form({"base": err.key}, err.detail)
        if not parsed.fields and not (user_input[CONF_IMPORT_SETTINGS] and parsed.settings):
            return self._import_form({"base": "import_empty"})
        self._import = parsed
        self._import_missing = [
            entity for entity in parsed.entities if self.hass.states.get(entity) is None
        ]
        if self._import_missing:
            return await self.async_step_import_map()
        return await self._apply_import({})

    async def async_step_import_map(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick this installation's sensor for each one the import names but is not here."""
        if user_input is not None:
            return await self._apply_import(
                {entity: user_input.get(entity) for entity in self._import_missing}
            )
        return self.async_show_form(
            step_id="import_map",
            data_schema=vol.Schema(
                {vol.Optional(entity): ENTITY_SELECTOR for entity in self._import_missing}
            ),
            description_placeholders={
                "missing": "\n".join(f"- `{entity}`" for entity in self._import_missing)
            },
        )

    async def _apply_import(self, remap: dict[str, str | None]) -> ConfigFlowResult:
        """Save the imported fields (and settings, if asked) to the options as saved now."""
        assert self._import is not None
        replace = self._import_input[CONF_IMPORT_MODE] == IMPORT_REPLACE
        fields: list[dict[str, Any]] = []
        for mapping in self._import.fields:
            mapping = dict(mapping)
            entity = mapping.get(MAP_ENTITY)
            if entity in remap:
                if remap[entity]:
                    mapping[MAP_ENTITY] = remap[entity]
                elif mapping.get(MAP_DIRECTION) == DIRECTION_DOWNLOAD:
                    # Downloading needs no sensor: keep it, without one.
                    mapping[MAP_ENTITY] = None
                else:
                    continue  # No sensor here: the field is left out.
            fields.append(mapping)
        self._options = deepcopy(DEFAULT_OPTIONS) | deepcopy(dict(self.config_entry.options))
        try:
            merged = merge_fields(self._fields, fields, replace)
        except ConfigImportError as err:
            return self._import_form({"base": err.key}, err.detail)
        # Only the imported fields that are kept are checked: when merging, a field
        # configured here wins and the imported one is not used.
        taken = [m for m in merged if any(m is f for f in fields)]
        if wrong := [
            m[MAP_FIELD]
            for m in taken
            if m.get(MAP_ENTITY)
            and self._unit_error(
                m[MAP_FIELD], m[MAP_ENTITY], m[MAP_AGGREGATE], m.get(MAP_UNIT)
            )
        ]:
            return self._import_form({"base": "import_unit_mismatch"}, ", ".join(wrong))
        self._options[CONF_FIELDS] = merged
        if self._import_input[CONF_IMPORT_SETTINGS]:
            self._options.update(self._import.settings)
        # Donor-only fields can be imported on a standard account (as when an account
        # stops being a donor): configured, but not sent or downloaded until it is
        # one. Said before saving, so it is a decision, not a surprise.
        if not self._donor():
            self._import_donor_only = [
                m[MAP_FIELD] for m in taken if FIELDS_BY_KEY[m[MAP_FIELD]].donor
            ]
            if self._import_donor_only:
                return await self.async_step_import_donor()
        return self._save()

    async def async_step_import_donor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm donor-only fields on a standard account."""
        if user_input is not None:
            return self._save()
        return self.async_show_form(
            step_id="import_donor",
            data_schema=vol.Schema({}),
            description_placeholders={"fields": ", ".join(self._import_donor_only)},
        )

    # PVOutput history

    async def async_step_download_history(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Download PVOutput history (every column) into statistics."""
        if (uploader := self._uploader()) is None:
            return self.async_abort(reason="not_loaded")
        archive = uploader.archive
        errors: dict[str, str] = {}
        yesterday = dt_util.now().date() - timedelta(days=1)
        if user_input is not None:
            start = date.fromisoformat(user_input[ATTR_START])
            end = min(
                date.fromisoformat(user_input[ATTR_END]) if user_input.get(ATTR_END) else yesterday,
                yesterday,
            )
            if end < start:
                errors["base"] = "end_before_start"
            elif not uploader._rate_ok():
                # No requests to spare for probing now: queue it as asked.
                return await self._queue_history(start, end)
            else:
                first = await archive.async_first_detail(start, end)
                if first is None:
                    return self.async_abort(
                        reason="no_history_detail",
                        description_placeholders={
                            "start": start.isoformat(),
                            "end": end.isoformat(),
                        },
                    )
                if first == start:
                    return await self._queue_history(start, end)
                self._history = (start, first, end)
                return await self.async_step_download_history_partial()
        schema = vol.Schema(
            {
                vol.Required(ATTR_START): DateSelector(),
                vol.Optional(ATTR_END): DateSelector(),
            }
        )
        summary = archive.summary
        return self.async_show_form(
            step_id="download_history",
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or {ATTR_END: yesterday.isoformat()}
            ),
            description_placeholders={
                "first": summary.get("first") or "none yet",
                "days": str(summary.get("days", 0)),
                "busy": (
                    f"A download is running ({archive.days_left} days left); a new one replaces it."
                    if archive.backfill
                    else ""
                ),
            },
            errors=errors,
        )

    async def async_step_download_history_partial(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Warn that PVOutput has no detail for the start of the range."""
        assert self._history is not None
        start, first, end = self._history
        if user_input is not None:
            return await self._queue_history(first, end)
        return self.async_show_form(
            step_id="download_history_partial",
            data_schema=vol.Schema({}),
            description_placeholders={
                "start": start.isoformat(),
                "first": first.isoformat(),
                "end": end.isoformat(),
                "missing": str((first - start).days),
            },
        )

    async def _queue_history(self, start: date, end: date) -> ConfigFlowResult:
        uploader = self._uploader()
        assert uploader is not None
        await uploader.archive.async_queue(start, end)
        await self.config_entry.runtime_data.async_request_refresh()
        return self.async_abort(
            reason="history_download_queued",
            description_placeholders={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "days": str((end - start).days + 1),
            },
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Account type, upload interval and history settings."""
        if user_input is not None:
            user_input[CONF_RETENTION_DAYS] = int(user_input[CONF_RETENTION_DAYS])
            self._options.update(user_input)
            return self._save()
        schema = vol.Schema(
            {
                vol.Required(CONF_DONOR_MODE): SelectSelector(
                    SelectSelectorConfig(
                        options=[DONOR_AUTO, DONOR_YES, DONOR_NO],
                        translation_key="donor_mode",
                    )
                ),
                vol.Required(CONF_UPLOAD_INTERVAL): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=INTERVAL_AUTO, label="From PVOutput"),
                            *(
                                SelectOptionDict(value=str(m), label=f"{m} minutes")
                                for m in UPLOAD_INTERVALS
                            ),
                        ]
                    )
                ),
                vol.Required(CONF_RETENTION_DAYS): NumberSelector(
                    NumberSelectorConfig(
                        min=30, max=3650, step=1, mode=NumberSelectorMode.BOX,
                        unit_of_measurement="days",
                    )
                ),
                vol.Required(CONF_OUTPUT_FALLBACK): BooleanSelector(),
            }
        )
        current = {k: self._options.get(k, v) for k, v in DEFAULT_OPTIONS.items()}
        current[CONF_UPLOAD_INTERVAL] = str(current[CONF_UPLOAD_INTERVAL])
        return self.async_show_form(
            step_id="settings",
            data_schema=self.add_suggested_values_to_schema(schema, current),
            description_placeholders={
                "detected_interval": str(
                    self.config_entry.data.get(CONF_DETECTED_INTERVAL, DEFAULT_INTERVAL_MINUTES)
                ),
                "detected_account": "donor"
                if self.config_entry.data.get(CONF_DETECTED_DONOR)
                else "standard",
            },
        )


def _local_datetime(value: str) -> datetime:
    """Parse a datetime selector value as local time."""
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        raise vol.Invalid(f"Invalid date and time: {value}")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_util.get_default_time_zone())


def _constant_value(field: str, value: Any) -> float | int:
    """Return a fixed value as stored; b6 states come from a list as text."""
    if field == BATTERY_STATE:
        return int(value)
    return value


def _has_source(mapping: dict[str, Any]) -> bool:
    """Return whether a field has an HA sensor or fixed value to upload."""
    return bool(mapping.get(MAP_ENTITY)) or mapping.get(MAP_CONSTANT) is not None


def _mode(mapping: dict[str, Any]) -> str:
    """Return a field's mode: send, record_only or download."""
    if mapping.get(MAP_DIRECTION) == DIRECTION_DOWNLOAD:
        return MODE_DOWNLOAD
    return MODE_SEND if mapping.get(MAP_ACTIVE, True) else MODE_RECORD
