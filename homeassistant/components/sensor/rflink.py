"""
Support for Rflink lights.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/light.rflink/
"""
import asyncio
from functools import partial
import logging

from homeassistant.components import group
import homeassistant.components.rflink as rflink

from . import DOMAIN

DEPENDENCIES = ['rflink']

_LOGGER = logging.getLogger(__name__)

KNOWN_DEVICE_IDS = []

SENSOR_KEYS_AND_UNITS = {
    'temperature': '°C',
    'humidity': '%',
}


@asyncio.coroutine
def async_setup_platform(hass, config, async_add_devices, discovery_info=None):
    """Setup the Rflink platform."""
    # add new (unconfigured) devices to user desired group
    if config.get('new_devices_group'):
        new_devices_group = yield from group.Group.async_create_group(
            hass, config.get('new_devices_group'), [], True)
    else:
        new_devices_group = None

    @asyncio.coroutine
    def add_new_device(event):
        """Check if device is known, otherwise create device entity."""
        packet = event.data[rflink.ATTR_PACKET]
        device_id = rflink.serialize_id(packet)
        if device_id not in KNOWN_DEVICE_IDS:
            KNOWN_DEVICE_IDS.append(device_id)
            rflinksensor = partial(RflinkSensor, device_id, hass)
            devices = []
            # create entity for each value in this packet
            for sensor_key, unit in SENSOR_KEYS_AND_UNITS.items():
                if sensor_key in packet:
                    devices.append(rflinksensor(sensor_key, unit))
            yield from async_add_devices(devices)
            # make sure the packet is processed by the new entities
            for device in devices:
                device.match_packet(packet)

            # maybe add to new devices group
            if new_devices_group:
                yield from new_devices_group.async_update_tracked_entity_ids(
                    list(new_devices_group.tracking) + [
                        device.entity_id for device in devices])

    hass.bus.async_listen(rflink.RFLINK_EVENT[DOMAIN], add_new_device)


class RflinkSensor(rflink.RflinkDevice):
    """Representation of a Rflink sensor."""

    # used for matching bus events
    domain = DOMAIN
    # packets can contain multiple values
    # which value this entity is bound to
    value_key = None

    def __init__(self, device_id, hass, value_key, unit, **kwargs):
        """Handle sensor specific args and super init."""
        self._value_key = value_key
        self._unit = unit
        super().__init__(device_id, hass, **kwargs)

    def _handle_packet(self, packet):
        """Domain specific packet handler."""
        self._state = packet[self._value_key]

    @property
    def unit_of_measurement(self):
        """Return measurement unit."""
        return self._unit

    @property
    def state(self):
        """Return value."""
        return self._state
