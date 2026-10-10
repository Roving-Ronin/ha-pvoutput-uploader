"""Config and options flow for PVOutput Uploader."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, CONF_API_KEY
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
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
)

from .api import PVOutputAuthError, PVOutputClient, PVOutputError
from .builder import unit_supported
from .const import (
    AGG_ENERGY_TODAY,
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
    DOMAIN,
    DONOR_AUTO,
    DONOR_NO,
    DONOR_YES,
    FIELD_CATALOG,
    FIELDS_BY_KEY,
    INTERVAL_AUTO,
    KIND_ENERGY,
    MAP_ACTIVE,
    MAP_AGGREGATE,
    MAP_CONSTANT,
    MAP_ENTITY,
    MAP_FIELD,
    MAP_NAME,
    UPLOAD_INTERVALS,
)

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
    """Field mappings and upload settings, edited from a menu."""

    def __init__(self) -> None:
        """Initialize the flow."""
        self._options: dict[str, Any] = {}
        self._new: dict[str, Any] = {}
        self._edit_field: str | None = None

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
            SelectOptionDict(value=k, label=f"{k} · {FIELDS_BY_KEY[k].name}")
            for k in keys
        ]

    def _describe(self) -> str:
        donor = self._donor()
        lines = []
        for m in sorted(self._fields, key=lambda m: list(FIELDS_BY_KEY).index(m[MAP_FIELD])):
            field = FIELDS_BY_KEY[m[MAP_FIELD]]
            source = m.get(MAP_ENTITY) or f"constant {m.get(MAP_CONSTANT)}"
            state = "active" if m.get(MAP_ACTIVE, True) else "passive"
            if field.donor and not donor:
                state += ", needs donor so not sent"
            aggregate = m.get(MAP_AGGREGATE) or field.default_aggregate
            lines.append(
                f"- **{field.key}** {m.get(MAP_NAME) or field.name}: "
                f"`{source}` ({aggregate}, {state})"
            )
        return "\n".join(lines) or "No fields yet."

    def _unit_error(self, field: str, entity_id: str, aggregate: str) -> str | None:
        """Return an error key if the sensor's unit does not fit the field."""
        kind = KIND_ENERGY if aggregate == AGG_ENERGY_TODAY else FIELDS_BY_KEY[field].kind
        state = self.hass.states.get(entity_id)
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) if state else None
        return None if unit_supported(kind, unit) else "unit_mismatch"

    def _free_fields(self) -> list[str]:
        """Return the fields not used yet that the account may send."""
        used = {m[MAP_FIELD] for m in self._fields}
        donor = self._donor()
        return [f.key for f in FIELD_CATALOG if f.key not in used and (donor or not f.donor)]

    def _field_selector(self, keys: list[str]) -> SelectSelector:
        return SelectSelector(
            SelectSelectorConfig(
                options=self._field_options(keys), mode=SelectSelectorMode.DROPDOWN
            )
        )

    def _details_schema(self, field: str, has_entity: bool) -> dict[Any, Any]:
        """Name, active flag and, for sensors, how values are combined."""
        fdef = FIELDS_BY_KEY[field]
        schema: dict[Any, Any] = {
            vol.Required(MAP_NAME): TextSelector(),
            vol.Required(MAP_ACTIVE): BooleanSelector(),
        }
        if has_entity and len(fdef.aggregates) > 1:
            schema[vol.Required(MAP_AGGREGATE)] = SelectSelector(
                SelectSelectorConfig(
                    options=list(fdef.aggregates),
                    translation_key="aggregate",
                )
            )
        return schema

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the main menu."""
        if not self._options:
            self._options = deepcopy(DEFAULT_OPTIONS) | deepcopy(dict(self.config_entry.options))
        menu = []
        if self._free_fields():
            menu += ["add_sensor", "add_constant"]
        if self._fields:
            menu += ["edit_field", "remove_field"]
        menu += ["settings", "save"]
        detected = self.config_entry.data.get(CONF_DETECTED_INTERVAL, DEFAULT_INTERVAL_MINUTES)
        return self.async_show_menu(
            step_id="init",
            menu_options=menu,
            description_placeholders={
                "fields": self._describe(),
                "account": "donor" if self._donor() else "standard",
                "detected_interval": str(detected),
            },
        )

    # Add

    async def async_step_add_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a sensor and the PVOutput field it feeds."""
        errors: dict[str, str] = {}
        if user_input is not None:
            field = user_input[MAP_FIELD]
            entity_id = user_input[MAP_ENTITY]
            # Extended fields start as "average"; energy since midnight is checked later.
            if error := self._unit_error(field, entity_id, FIELDS_BY_KEY[field].default_aggregate):
                errors["base"] = error
            else:
                self._new = {MAP_FIELD: field, MAP_ENTITY: entity_id, MAP_CONSTANT: None}
                return await self.async_step_sensor_details()
        schema = vol.Schema(
            {
                vol.Required(MAP_ENTITY): ENTITY_SELECTOR,
                vol.Required(MAP_FIELD): self._field_selector(self._free_fields()),
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
            if error := self._unit_error(field, self._new[MAP_ENTITY], aggregate):
                errors["base"] = error
            else:
                self._fields.append(
                    self._new
                    | {
                        MAP_NAME: user_input[MAP_NAME],
                        MAP_ACTIVE: user_input[MAP_ACTIVE],
                        MAP_AGGREGATE: aggregate,
                    }
                )
                self._new = {}
                return await self.async_step_init()
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
        if user_input is not None:
            field = user_input[MAP_FIELD]
            self._fields.append(
                {
                    MAP_FIELD: field,
                    MAP_ENTITY: None,
                    MAP_CONSTANT: user_input[MAP_CONSTANT],
                    MAP_NAME: user_input.get(MAP_NAME) or FIELDS_BY_KEY[field].name,
                    MAP_ACTIVE: user_input[MAP_ACTIVE],
                    MAP_AGGREGATE: FIELDS_BY_KEY[field].default_aggregate,
                }
            )
            return await self.async_step_init()
        schema = vol.Schema(
            {
                vol.Required(MAP_FIELD): self._field_selector(self._free_fields()),
                vol.Required(MAP_CONSTANT): CONSTANT_SELECTOR,
                vol.Optional(MAP_NAME): TextSelector(),
                vol.Required(MAP_ACTIVE, default=True): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="add_constant", data_schema=schema)

    # Edit

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
                    vol.Required(MAP_FIELD): SelectSelector(
                        SelectSelectorConfig(
                            options=self._field_options(
                                [f.key for f in FIELD_CATALOG if f.key in {m[MAP_FIELD] for m in self._fields}]
                            ),
                            mode=SelectSelectorMode.LIST,
                        )
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
        errors: dict[str, str] = {}
        if user_input is not None:
            aggregate = user_input.get(
                MAP_AGGREGATE, mapping.get(MAP_AGGREGATE) or fdef.default_aggregate
            )
            if is_sensor and (
                error := self._unit_error(field, user_input[MAP_ENTITY], aggregate)
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
                if is_sensor:
                    mapping[MAP_ENTITY] = user_input[MAP_ENTITY]
                else:
                    mapping[MAP_CONSTANT] = user_input[MAP_CONSTANT]
                self._edit_field = None
                return await self.async_step_init()
        source: dict[Any, Any] = (
            {vol.Required(MAP_ENTITY): ENTITY_SELECTOR}
            if is_sensor
            else {vol.Required(MAP_CONSTANT): CONSTANT_SELECTOR}
        )
        schema = vol.Schema(source | self._details_schema(field, is_sensor))
        defaults = user_input or {
            k: v for k, v in mapping.items() if v is not None and k != MAP_FIELD
        }
        return self.async_show_form(
            step_id="edit_details",
            data_schema=self.add_suggested_values_to_schema(schema, defaults),
            description_placeholders={"field": f"{field} · {fdef.name}"},
            errors=errors,
        )

    # Remove

    async def async_step_remove_field(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove fields."""
        if user_input is not None:
            drop = set(user_input[CONF_FIELDS])
            self._options[CONF_FIELDS] = [m for m in self._fields if m[MAP_FIELD] not in drop]
            return await self.async_step_init()
        return self.async_show_form(
            step_id="remove_field",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_FIELDS, default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=self._field_options(
                                [f.key for f in FIELD_CATALOG if f.key in {m[MAP_FIELD] for m in self._fields}]
                            ),
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    # Settings and save

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Account type, upload interval and history settings."""
        if user_input is not None:
            user_input[CONF_RETENTION_DAYS] = int(user_input[CONF_RETENTION_DAYS])
            self._options.update(user_input)
            return await self.async_step_init()
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
        )

    async def async_step_save(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Store the options."""
        return self.async_create_entry(data=self._options)
