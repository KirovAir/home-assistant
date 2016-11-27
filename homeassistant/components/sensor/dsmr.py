"""
Support for Dutch Smart Meter Requirements.

Also known as: Smartmeter or P1 port.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/sensor.dsmr/

Technical overview:

DSMR is a standard to which Dutch smartmeters must comply. It specifies that
the smartmeter must send out a 'telegram' every 10 seconds over a serial port.

The contents of this telegram differ between version but they generally consist
of lines with 'obis' (Object Identification System, a numerical ID for a value)
followed with the value and unit.

This module sets up a asynchronous reading loop using the `dsmr_parser` module
which waits for a complete telegram, parser it and puts it on an async queue as
a dictionary of `obis`/object mapping. The numeric value and unit of each value
can be read from the objects attributes. Because the `obis` are know for each
DSMR version the Entities for this component are create during bootstrap.

Another loop (DSMR class) is setup which reads the telegram queue,
stores/caches the latest telegram and notifies the Entities that the telegram
has been updated.
"""
import asyncio
import logging
from datetime import timedelta

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import (
    CONF_PORT, EVENT_HOMEASSISTANT_STOP, STATE_UNKNOWN)
from homeassistant.helpers.entity import Entity

_LOGGER = logging.getLogger(__name__)

REQUIREMENTS = ['dsmr_parser==0.4']

CONF_DSMR_VERSION = 'dsmr_version'

DEFAULT_DSMR_VERSION = '2.2'
DEFAULT_PORT = '/dev/ttyUSB0'
DOMAIN = 'dsmr'

ICON_GAS = 'mdi:fire'
ICON_POWER = 'mdi:flash'

# Smart meter sends telegram every 10 seconds
MIN_TIME_BETWEEN_UPDATES = timedelta(seconds=10)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.string,
    vol.Optional(CONF_DSMR_VERSION, default=DEFAULT_DSMR_VERSION): vol.All(
        cv.string, vol.In(['4', '2.2'])),
})


@asyncio.coroutine
def async_setup_platform(hass, config, async_add_devices, discovery_info=None):
    """Set up the DSMR sensor."""
    # Suppress logging
    logging.getLogger('dsmr_parser').setLevel(logging.ERROR)

    from dsmr_parser import obis_references as obis
    from dsmr_parser.protocol import create_dsmr_reader

    dsmr_version = config[CONF_DSMR_VERSION]

    # Define list of name,obis mappings to generate entities
    obis_mapping = [
        ['Power Consumption', obis.CURRENT_ELECTRICITY_USAGE],
        ['Power Production', obis.CURRENT_ELECTRICITY_DELIVERY],
        ['Power Tariff', obis.ELECTRICITY_ACTIVE_TARIFF],
        ['Power Consumption (low)', obis.ELECTRICITY_USED_TARIFF_1],
        ['Power Consumption (normal)', obis.ELECTRICITY_USED_TARIFF_2],
        ['Power Production (low)', obis.ELECTRICITY_DELIVERED_TARIFF_1],
        ['Power Production (normal)', obis.ELECTRICITY_DELIVERED_TARIFF_2],
    ]

    # Generate device entities
    devices = [DSMREntity(name, obis) for name, obis in obis_mapping]

    # Protocol version specific obis
    if dsmr_version == '4':
        gas_obis = obis.HOURLY_GAS_METER_READING
    else:
        gas_obis = obis.GAS_METER_READING

    # add gas meter reading and derivative for usage
    devices += [
        DSMREntity('Gas Consumption', gas_obis),
        DerivativeDSMREntity('Hourly Gas Consumption', gas_obis),
    ]

    yield from async_add_devices(devices)

    def update_entities_telegram(telegram):
        """Update entities with latests telegram & trigger state update."""
        # Make all device entities aware of new telegram
        for device in devices:
            device.telegram = telegram
            hass.async_add_job(device.async_update_ha_state)

    # Creates a asyncio.Protocol for reading DSMR telegrams from serial
    # and calls update_entities_telegram to update entities on arrival
    dsmr = create_dsmr_reader(config[CONF_PORT], config[CONF_DSMR_VERSION],
                              update_entities_telegram, loop=hass.loop)

    # Start DSMR asycnio.Protocol reader
    transport, _ = yield from hass.loop.create_task(dsmr)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, transport.close)


def get_last_state_from_db(entity_id):
    """Get the most recent state for this entity from database."""

    states = recorder.get_model('States')
    try:
        last_state = recorder.execute(
            recorder.query('States').filter(
                (states.entity_id == entity_id) &
                (states.last_changed == states.last_updated) &
                (states.state != 'unknown')
            ).order_by(states.state_id.desc()).limit(1))
    except TypeError:
        return
    except RuntimeError:
        return

    if not last_state:
        return

    return last_state[0].state


class DSMREntity(Entity):
    """Entity reading values from DSMR telegram."""

    def __init__(self, name, obis):
        """"Initialize entity."""
        self._name = name
        self._obis = obis
        self.telegram = {}
        self._state = 10

    def get_dsmr_object_attr(self, attribute):
        """Read attribute from last received telegram for this DSMR object."""
        # Make sure telegram contains an object for this entities obis
        if self._obis not in self.telegram:
            return None

        # get the attibute value if the object has it
        dsmr_object = self.telegram[self._obis]
        return getattr(dsmr_object, attribute, None)

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def icon(self):
        """Icon to use in the frontend, if any."""
        if 'Power' in self._name:
            return ICON_POWER
        elif 'Gas' in self._name:
            return ICON_GAS

    @property
    def state(self):
        """Return the state of sensor, if available, translate if needed."""
        from dsmr_parser import obis_references as obis

        value = self.get_dsmr_object_attr('value')

        if self._obis == obis.ELECTRICITY_ACTIVE_TARIFF:
            return self.translate_tariff(value)
        else:
            if value:
                return value
            else:
                return STATE_UNKNOWN

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement of this entity, if any."""
        return self.get_dsmr_object_attr('unit')

    @staticmethod
    def translate_tariff(value):
        """Convert 2/1 to normal/low."""
        # DSMR V2.2: Note: Rate code 1 is used for low rate and rate code 2 is
        # used for normal rate.
        if value == '0002':
            return 'normal'
        elif value == '0001':
            return 'low'
        else:
            return STATE_UNKNOWN


class DerivativeDSMREntity(DSMREntity):
    """Calculated derivative for values where the DSMR doesn't offer one.

    Gas readings are only reported per hour and don't offer a rate only
    the current meter reading. This entity converts subsequents readings
    into a hourly rate.
    """

    _previous_reading = None
    _previous_timestamp = None
    _state = STATE_UNKNOWN

    @property
    def state(self):
        """Return current hourly rate, recalculate if needed."""
        # entity_id = 'sensor.' + slugify(self._name)

        # check if the timestamp for the object differs from the previous one
        timestamp = self.get_dsmr_object_attr('datetime')
        if timestamp and timestamp != self._previous_timestamp:
            current_reading = self.get_dsmr_object_attr('value')

            if self._previous_reading is None:
                # can't calculate rate without previous datapoint
                # just store current point
                pass
            else:
                # recalculate the rate
                diff = current_reading - self._previous_reading
                self._state = diff

            self._previous_reading = current_reading
            self._previous_timestamp = timestamp

        return self._state

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement of this entity, per hour, if any."""
        unit = self.get_dsmr_object_attr('unit')
        if unit:
            return unit + '/h'
