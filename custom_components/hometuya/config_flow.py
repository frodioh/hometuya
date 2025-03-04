"""Config flow for LocalTuya integration integration."""
import errno
import logging
from importlib import import_module

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant import config_entries, core, exceptions
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_ENTITIES,
    CONF_FRIENDLY_NAME,
    CONF_HOST,
    CONF_ID,
    CONF_MODEL,
    CONF_PLATFORM,
    CONF_SCAN_INTERVAL,
)
from homeassistant.core import callback

from .common import async_config_entry_by_device_id, pytuya

from .const import (
    CONF_LOCAL_KEY,
    CONF_PRODUCT_KEY,
    CONF_PROTOCOL_VERSION,
    CONF_IS_GATEWAY,
    CONF_PARENT_GATEWAY,
    DATA_DISCOVERY,
    DOMAIN,
    PLATFORMS,
    CONF_DPS_STRINGS,
    PARAMETER_GW_ID,
    PARAMETER_IP,
    PARAMETER_PRODUCT_KEY,
    PARAMETER_VERSION,
)
from .discovery import discover

_LOGGER = logging.getLogger(__name__)

PLATFORM_TO_ADD = "platform_to_add"
NO_ADDITIONAL_PLATFORMS = "no_additional_platforms"
DISCOVERED_DEVICE = "discovered_device"

CUSTOM_DEVICE = "(manual)"
CUSTOM_SUB_DEVICE = "(sub-device)"

FLOW_DP = "data_point"

BASIC_INFO_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_FRIENDLY_NAME): str,
        vol.Required(CONF_LOCAL_KEY): str,
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_DEVICE_ID): str,
        vol.Required(CONF_PROTOCOL_VERSION, default="3.3"): vol.In(["3.1", "3.3"]),
        vol.Optional(CONF_IS_GATEWAY): cv.boolean,
        vol.Optional(CONF_SCAN_INTERVAL): int,
        vol.Optional(CONF_PRODUCT_KEY): cv.string,
        vol.Optional(CONF_MODEL): cv.string,
    }
)

DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_ID): cv.string,
        vol.Required(CONF_FRIENDLY_NAME): cv.string,
        vol.Required(CONF_PROTOCOL_VERSION, default="3.3"): vol.In(["3.1", "3.3"]),
        vol.Optional(CONF_SCAN_INTERVAL): int,
        vol.Optional(CONF_HOST): cv.string,
        vol.Optional(CONF_LOCAL_KEY): cv.string,
        vol.Optional(CONF_IS_GATEWAY): cv.boolean,
        vol.Optional(CONF_PARENT_GATEWAY): cv.string,
        vol.Optional(CONF_PRODUCT_KEY): cv.string,
        vol.Optional(CONF_MODEL): cv.string,
    },
)

PICK_ENTITY_SCHEMA = vol.Schema(
    {vol.Required(PLATFORM_TO_ADD, default=PLATFORMS[0]): vol.In(PLATFORMS)}
)

DATA_POINT_SCHEMA = vol.Schema({vol.Required(FLOW_DP): int})


def user_schema(devices, entries):
    """Create schema for user step."""
    devices = {dev_id: dev[PARAMETER_IP] for dev_id, dev in devices.items()}
    devices.update(
        {
            ent.data[CONF_DEVICE_ID]: ent.data[CONF_FRIENDLY_NAME]
            for ent in entries
            if ent.source != SOURCE_IMPORT
        }
    )
    device_list = [f"{key} ({value})" for key, value in devices.items()]
    return vol.Schema(
        {
            vol.Required(DISCOVERED_DEVICE): vol.In(
                device_list + [CUSTOM_DEVICE, CUSTOM_SUB_DEVICE]
            )
        }
    )


def sub_device_schema(devices):
    """Create schema for sub-device step."""
    device_list = [
        f"{device[CONF_DEVICE_ID]} ({device[CONF_FRIENDLY_NAME]})" for device in devices
    ]
    return vol.Schema(
        {
            vol.Required(CONF_PARENT_GATEWAY): vol.In(device_list),
            vol.Required(CONF_FRIENDLY_NAME): cv.string,
            vol.Required(CONF_DEVICE_ID): cv.string,
            vol.Optional(CONF_PRODUCT_KEY): str,
        },
    )


def options_schema(entities):
    """Create schema for options."""
    entity_names = [
        f"{entity[CONF_ID]} {entity[CONF_FRIENDLY_NAME]}" for entity in entities
    ]
    return vol.Schema(
        {
            vol.Required(CONF_FRIENDLY_NAME): str,
            vol.Required(CONF_HOST): str,
            vol.Required(CONF_LOCAL_KEY): str,
            vol.Required(CONF_PROTOCOL_VERSION, default="3.3"): vol.In(["3.1", "3.3"]),
            vol.Optional(CONF_SCAN_INTERVAL): int,
            vol.Optional(CONF_IS_GATEWAY, default=False): cv.boolean,
            vol.Optional(
                CONF_ENTITIES, description={"suggested_value": entity_names}
            ): cv.multi_select(entity_names),
            vol.Optional(CONF_PRODUCT_KEY): str,
        }
    )


def options_schema_sub_device(entities):
    """Create schema for sub_device options."""
    entity_names = [
        f"{entity[CONF_ID]} {entity[CONF_FRIENDLY_NAME]}" for entity in entities
    ]

    return vol.Schema(
        {
            vol.Required(CONF_FRIENDLY_NAME): str,
            # vol.Required(CONF_HOST): str,
            # vol.Required(CONF_LOCAL_KEY): str,
            vol.Required(CONF_PROTOCOL_VERSION, default="3.3"): vol.In(["3.1", "3.3"]),
            # vol.Optional(CONF_SCAN_INTERVAL): int,
            # vol.Optional(CONF_IS_GATEWAY, default=False): cv.boolean,
            vol.Required(
                CONF_ENTITIES, description={"suggested_value": entity_names}
            ): cv.multi_select(entity_names),
            vol.Optional(CONF_PRODUCT_KEY): str,
        }
    )


def schema_defaults(schema, dps_list=None, **defaults):
    """Create a new schema with default values filled in."""
    copy = schema.extend({})
    for field, field_type in copy.schema.items():
        if isinstance(field_type, vol.In):
            value = None
            for dps in dps_list or []:
                if dps.startswith(f"{defaults.get(field)} "):
                    value = dps
                    break

            if value in field_type.container:
                field.default = vol.default_factory(value)
                continue

        if field.schema in defaults:
            field.default = vol.default_factory(defaults[field])
    return copy


def dps_string_list(dps_data):
    """Return list of friendly DPS values."""
    return [f"{id} (value: {value})" for id, value in dps_data.items()]


def gen_dps_strings():
    """Generate list of DPS values."""
    return [f"{dp} (value: ?)" for dp in range(1, 256)]


def platform_schema(platform, dps_strings, allow_id=True, yaml=False):
    """Generate input validation schema for a platform."""
    schema = {}
    if yaml:
        # In YAML mode we force the specified platform to match flow schema
        schema[vol.Required(CONF_PLATFORM)] = vol.In([platform])
    if allow_id:
        schema[vol.Required(CONF_ID)] = vol.In(dps_strings)
    schema[vol.Required(CONF_FRIENDLY_NAME)] = str
    return vol.Schema(schema).extend(flow_schema(platform, dps_strings))


def flow_schema(platform, dps_strings):
    """Return flow schema for a specific platform."""
    integration_module = ".".join(__name__.split(".")[:-1])
    return import_module("." + platform, integration_module).flow_schema(dps_strings)


def strip_dps_values(user_input, dps_strings):
    """Remove values and keep only index for DPS config items."""
    stripped = {}
    for field, value in user_input.items():
        if value in dps_strings:
            stripped[field] = int(user_input[field].split(" ")[0])
        else:
            stripped[field] = user_input[field]
    return stripped


def validate_config_schema(config):
    """Valid configuration schema to ensure proper values have been declared"""
    for device in config:
        if device.get(CONF_PARENT_GATEWAY):
            if device.get(CONF_IS_GATEWAY):
                raise vol.Invalid(
                    "Sub-device declared as gateway device at the same time"
                )
        else:
            if not device.get(CONF_HOST):
                raise vol.Invalid("Host not specified")
            if not device.get(CONF_LOCAL_KEY):
                raise vol.Invalid("Local key not specified")

    return config


def config_schema():
    """Build schema used for setting up component."""
    entity_schemas = [
        platform_schema(platform, range(1, 256), yaml=True) for platform in PLATFORMS
    ]
    return vol.Schema(
        {
            DOMAIN: vol.All(
                cv.ensure_list,
                [
                    DEVICE_SCHEMA.extend(
                        {vol.Optional(CONF_ENTITIES): [vol.Any(*entity_schemas)]},
                    ),
                ],
                validate_config_schema,
            )
        },
        extra=vol.ALLOW_EXTRA,
    )


async def validate_input(hass: core.HomeAssistant, data):
    """Validate the user input allows us to connect."""
    detected_dps = {}

    interface = None
    try:
        if data.get(CONF_PARENT_GATEWAY):
            parent_dev = async_config_entry_by_device_id(
                hass, data[CONF_PARENT_GATEWAY]
            )
            interface = await pytuya.connect(
                parent_dev.data[CONF_HOST],
                parent_dev.data[CONF_DEVICE_ID],
                parent_dev.data[CONF_LOCAL_KEY],
                float(parent_dev.data[CONF_PROTOCOL_VERSION]),
                is_gateway=True,
            )

            interface.add_sub_device(data[CONF_DEVICE_ID])
            detected_dps = await interface.detect_available_dps(data[CONF_DEVICE_ID])

        else:
            interface = await pytuya.connect(
                data[CONF_HOST],
                data[CONF_DEVICE_ID],
                data[CONF_LOCAL_KEY],
                float(data[CONF_PROTOCOL_VERSION]),
            )

            detected_dps = await interface.detect_available_dps()
    except (ConnectionRefusedError, ConnectionResetError) as ex:
        raise CannotConnect from ex
    except ValueError as ex:
        raise InvalidAuth from ex
    finally:
        if interface:
            await interface.close()

    # Indicate an error if no datapoints found as the rest of the flow
    # won't work in this case
    if not detected_dps:
        raise EmptyDpsList

    return dps_string_list(detected_dps)


class LocaltuyaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for LocalTuya integration."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get options flow for this handler."""
        return LocalTuyaOptionsFlowHandler(config_entry)

    def __init__(self):
        """Initialize a new LocaltuyaConfigFlow."""
        self.basic_info = None
        self.dps_strings = []
        self.platform = None
        self.devices = {}
        self.selected_device = None
        self.entities = []

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            if user_input[DISCOVERED_DEVICE] == CUSTOM_SUB_DEVICE:
                return await self.async_step_basic_sub_device_info()

            if user_input[DISCOVERED_DEVICE] != CUSTOM_DEVICE:
                self.selected_device = user_input[DISCOVERED_DEVICE].split(" ")[0]
            return await self.async_step_basic_info()

        # Use cache if available or fallback to manual discovery
        devices = {}
        data = self.hass.data.get(DOMAIN)
        if data and DATA_DISCOVERY in data:
            devices = data[DATA_DISCOVERY].devices
        else:
            try:
                devices = await discover()
            except OSError as ex:
                if ex.errno == errno.EADDRINUSE:
                    errors["base"] = "address_in_use"
                else:
                    errors["base"] = "discovery_failed"
            except Exception:  # pylint: disable= broad-except
                _LOGGER.exception("Discovery failed")
                errors["base"] = "discovery_failed"

        self.devices = {
            ip: dev
            for ip, dev in devices.items()
            if dev[PARAMETER_GW_ID] not in self._async_current_ids()
        }

        return self.async_show_form(
            step_id="user",
            errors=errors,
            data_schema=user_schema(self.devices, self._async_current_entries()),
        )

    async def async_step_basic_info(self, user_input=None):
        """Handle input of basic info."""
        errors = {}
        if user_input is not None:
            if user_input == FLOW_DP:
                if self.dps_strings:
                    return await self.async_step_pick_entity_type()
            else:
                await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
                self.basic_info = user_input
                if self.selected_device is not None:
                    self.basic_info[CONF_PRODUCT_KEY] = self.devices[
                        self.selected_device
                    ][PARAMETER_PRODUCT_KEY]

            try:
                self.dps_strings = await validate_input(self.hass, user_input)
                return await self.async_step_pick_entity_type()
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except EmptyDpsList:
                if self.basic_info[CONF_IS_GATEWAY]:  # Gateways don't have dps
                    entry = async_config_entry_by_device_id(self.hass, self.unique_id)
                    if entry:
                        self.hass.config_entries.async_update_entry(
                            entry, data=user_input
                        )
                        return self.async_abort(reason="device_updated")
                    return self.async_create_entry(
                        title=user_input[CONF_FRIENDLY_NAME], data=user_input
                    )
                else:
                    return await self.async_step_add_dp()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        # If selected device exists as a config entry, load config from it
        if self.selected_device in self._async_current_ids():
            entry = async_config_entry_by_device_id(self.hass, self.selected_device)
            await self.async_set_unique_id(entry.data[CONF_DEVICE_ID])
            self.basic_info = entry.data.copy()
            self.dps_strings = self.basic_info.pop(CONF_DPS_STRINGS).copy()
            self.entities = self.basic_info.pop(CONF_ENTITIES).copy()
            return await self.async_step_pick_entity_type()

        # Insert default values from discovery if present
        defaults = {}
        defaults.update(user_input or {})
        if self.selected_device is not None:
            device = self.devices[self.selected_device]
            defaults[CONF_HOST] = device.get(PARAMETER_IP)
            defaults[CONF_DEVICE_ID] = device.get(PARAMETER_GW_ID)
            defaults[CONF_PROTOCOL_VERSION] = device.get(PARAMETER_VERSION)

        return self.async_show_form(
            step_id="basic_info",
            data_schema=schema_defaults(BASIC_INFO_SCHEMA, **defaults),
            errors=errors,
        )

    async def async_step_basic_sub_device_info(self, user_input=None):
        """Handle input of basic sub-device info."""
        errors = {}
        if user_input is not None:
            if user_input == FLOW_DP:
                if self.dps_strings:
                    return await self.async_step_pick_entity_type()
            else:
                await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
                # Take only the device ID
                user_input[CONF_PARENT_GATEWAY] = user_input[CONF_PARENT_GATEWAY].split(
                    " "
                )[0]
                user_input[CONF_PROTOCOL_VERSION] = "3.3"
                self.basic_info = user_input

            try:
                self.dps_strings = await validate_input(self.hass, user_input)
                return await self.async_step_pick_entity_type()
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except EmptyDpsList:
                return await self.async_step_add_dp()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        # Populate list of gateways to choose from
        current_entries = self.hass.config_entries.async_entries(DOMAIN)
        devices = [
            entry.data for entry in current_entries if entry.data.get(CONF_IS_GATEWAY)
        ]

        if len(devices) <= 0:
            errors["base"] = "no_gateway_added"

        return self.async_show_form(
            step_id="basic_sub_device_info",
            data_schema=sub_device_schema(devices),
            errors=errors,
        )

    async def async_step_pick_entity_type(self, user_input=None):
        """Handle asking if user wants to add another entity."""
        if user_input is not None:
            if user_input.get(NO_ADDITIONAL_PLATFORMS):
                config = {
                    **self.basic_info,
                    CONF_DPS_STRINGS: self.dps_strings,
                    CONF_ENTITIES: self.entities,
                }
                entry = async_config_entry_by_device_id(self.hass, self.unique_id)
                if entry:
                    self.hass.config_entries.async_update_entry(entry, data=config)
                    return self.async_abort(reason="device_updated")
                return self.async_create_entry(
                    title=config[CONF_FRIENDLY_NAME], data=config
                )

            self.platform = user_input[PLATFORM_TO_ADD]
            return await self.async_step_add_entity()

        # Add a checkbox that allows bailing out from config flow iff at least one
        # entity has been added
        schema = PICK_ENTITY_SCHEMA
        if self.platform is not None or (CONF_IS_GATEWAY in self.basic_info and self.basic_info[CONF_IS_GATEWAY]):
            schema = schema.extend(
                {vol.Required(NO_ADDITIONAL_PLATFORMS, default=True): bool}
            )

        return self.async_show_form(step_id="pick_entity_type", data_schema=schema)

    async def async_step_add_entity(self, user_input=None):
        """Handle adding a new entity."""
        errors = {}
        if user_input is not None:
            already_configured = any(
                switch[CONF_ID] == int(user_input[CONF_ID].split(" ")[0])
                for switch in self.entities
            )
            if not already_configured:
                user_input[CONF_PLATFORM] = self.platform
                self.entities.append(strip_dps_values(user_input, self.dps_strings))
                return await self.async_step_pick_entity_type()

            errors["base"] = "entity_already_configured"

        return self.async_show_form(
            step_id="add_entity",
            data_schema=platform_schema(self.platform, self.dps_strings),
            errors=errors,
            description_placeholders={"platform": self.platform},
        )

    async def async_step_add_dp(self, user_input=None):
        """Handle adding a new data point"""
        errors = {}
        if user_input is not None:
            if user_input.get(FLOW_DP) == 0:
                if self.basic_info.get(CONF_PARENT_GATEWAY):
                    return await self.async_step_basic_sub_device_info(FLOW_DP)
                return await self.async_step_basic_info(FLOW_DP)
            else:
                dp_str = str(user_input[FLOW_DP]) + " (value: Custom)"
                if dp_str not in self.dps_strings:
                    self.dps_strings.append(dp_str)
                return await self.async_step_add_dp()

        return self.async_show_form(
            step_id="add_dp",
            data_schema=DATA_POINT_SCHEMA,
            errors=errors,
        )

    async def async_step_import(self, user_input):
        """Handle import from YAML."""
        await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
        self._abort_if_unique_id_configured(updates=user_input)
        return self.async_create_entry(
            title=f"{user_input[CONF_FRIENDLY_NAME]} (YAML)", data=user_input
        )


class LocalTuyaOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for LocalTuya integration."""

    def __init__(self, config_entry):
        """Initialize localtuya options flow."""
        self.config_entry = config_entry
        self.dps_strings = config_entry.data.get(CONF_DPS_STRINGS, gen_dps_strings())
        self.parent_gateway = None
        self.entities = []
        if not config_entry.data.get(CONF_IS_GATEWAY):
            self.entities = config_entry.data[CONF_ENTITIES]
            if config_entry.data.get(CONF_PARENT_GATEWAY):
                self.parent_gateway = config_entry.data.get(CONF_PARENT_GATEWAY)
        self.data = None

    async def async_step_init(self, user_input=None):
        """Manage basic options."""
        device_id = self.config_entry.data[CONF_DEVICE_ID]
        if user_input is not None:
            self.data = user_input.copy()
            self.data.update(
                {
                    CONF_DEVICE_ID: device_id,
                }
            )
            if self.config_entry.data.get(CONF_IS_GATEWAY):
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    title=self.data[CONF_FRIENDLY_NAME],
                    data=self.data,
                )
                return self.async_create_entry(title="", data={})
            if self.parent_gateway:
                 self.data.update(
                    {
                         CONF_PARENT_GATEWAY: self.parent_gateway,
                    }
                 )
            self.data.update(
                {
                    CONF_ENTITIES: [],
                    CONF_DPS_STRINGS: self.dps_strings,
                }
            )
            if len(user_input[CONF_ENTITIES]) > 0:
                entity_ids = [
                    int(entity.split(" ")[0]) for entity in user_input[CONF_ENTITIES]
                ]
                self.entities = [
                    entity
                    for entity in self.config_entry.data[CONF_ENTITIES]
                    if entity[CONF_ID] in entity_ids
                ]
                return await self.async_step_entity()

        # Not supported for YAML imports
        if self.config_entry.source == config_entries.SOURCE_IMPORT:
            return await self.async_step_yaml_import()

        if self.parent_gateway is not None:
            return self.async_show_form(
                step_id="init",
                data_schema=schema_defaults(
                    options_schema_sub_device(self.entities), **self.config_entry.data
                ),
                description_placeholders={CONF_DEVICE_ID: device_id},
            )
        
        return self.async_show_form(
            step_id="init",
            data_schema=schema_defaults(
                options_schema(self.entities), **self.config_entry.data
            ),
            description_placeholders={CONF_DEVICE_ID: device_id},
        )

    async def async_step_entity(self, user_input=None):
        """Manage entity settings."""
        errors = {}
        if user_input is not None:
            entity = strip_dps_values(user_input, self.dps_strings)
            entity[CONF_ID] = self.current_entity[CONF_ID]
            entity[CONF_PLATFORM] = self.current_entity[CONF_PLATFORM]
            self.data[CONF_ENTITIES].append(entity)

            if len(self.entities) == len(self.data[CONF_ENTITIES]):
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    title=self.data[CONF_FRIENDLY_NAME],
                    data=self.data,
                )
                return self.async_create_entry(title="", data={})

        schema = platform_schema(
            self.current_entity[CONF_PLATFORM], self.dps_strings, allow_id=False
        )
        return self.async_show_form(
            step_id="entity",
            errors=errors,
            data_schema=schema_defaults(
                schema, self.dps_strings, **self.current_entity
            ),
            description_placeholders={
                "id": self.current_entity[CONF_ID],
                "platform": self.current_entity[CONF_PLATFORM],
            },
        )

    async def async_step_yaml_import(self, user_input=None):
        """Manage YAML imports."""
        if user_input is not None:
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="yaml_import")

    @property
    def current_entity(self):
        """Existing configuration for entity currently being edited."""
        return self.entities[len(self.data[CONF_ENTITIES])]


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(exceptions.HomeAssistantError):
    """Error to indicate there is invalid auth."""


class EmptyDpsList(exceptions.HomeAssistantError):
    """Error to indicate no datapoints found."""
