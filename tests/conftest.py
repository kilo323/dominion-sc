import sys
import types

"""Pytest fixture file that injects minimal Home Assistant and requests/bs4
stubs into sys.modules so unit tests can import integration modules without
the full Home Assistant runtime.

This provides the small subset used by the tests: sensor enums/classes,
helpers.storage.Store stub, update coordinator, and constants.
"""

# Create a minimal stub for parts of the Home Assistant package
ha = types.ModuleType("homeassistant")
components = types.ModuleType("homeassistant.components")
sensor_mod = types.ModuleType("homeassistant.components.sensor")

# Minimal classes / enums used by sensor.py
class SensorDeviceClass:
    ENERGY = "energy"
    GAS = "gas"
    MONETARY = "monetary"
    TIMESTAMP = "timestamp"


class SensorStateClass:
    TOTAL_INCREASING = "total_increasing"
    TOTAL = "total"


class SensorEntity:
    def __init__(self, *args, **kwargs):
        pass


class SensorEntityDescription(dict):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        for k, v in kwargs.items():
            setattr(self, k, v)


sensor_mod.SensorDeviceClass = SensorDeviceClass
sensor_mod.SensorEntity = SensorEntity
sensor_mod.SensorEntityDescription = SensorEntityDescription
sensor_mod.SensorStateClass = SensorStateClass

components.sensor = sensor_mod
ha.components = components

# Other small stubs
ha.config_entries = types.SimpleNamespace(ConfigEntry=object)
ha.const = types.SimpleNamespace(
    CURRENCY_DOLLAR="$",
    EntityCategory=types.SimpleNamespace(DIAGNOSTIC="diagnostic"),
    UnitOfEnergy=types.SimpleNamespace(KILO_WATT_HOUR="kWh"),
    UnitOfVolume=types.SimpleNamespace(CUBIC_FEET="ft3"),
)
ha.core = types.SimpleNamespace(HomeAssistant=object)

# Create helper submodules under homeassistant.helpers
device_registry_mod = types.ModuleType("homeassistant.helpers.device_registry")
setattr(device_registry_mod, "DeviceEntryType", types.SimpleNamespace(SERVICE="service"))

entity_mod = types.ModuleType("homeassistant.helpers.entity")
setattr(entity_mod, "DeviceInfo", dict)

entity_platform_mod = types.ModuleType("homeassistant.helpers.entity_platform")
setattr(entity_platform_mod, "AddEntitiesCallback", object)

update_coordinator_mod = types.ModuleType("homeassistant.helpers.update_coordinator")
class CoordinatorEntity:
    def __init__(self, coordinator=None):
        self.coordinator = coordinator
    @classmethod
    def __class_getitem__(cls, item):
        return cls

setattr(update_coordinator_mod, "CoordinatorEntity", CoordinatorEntity)

class UpdateFailed(Exception):
    pass

class DataUpdateCoordinator:
    def __init__(self, hass, logger, name: str = "", update_interval=None):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
    def async_set_updated_data(self, data):
        self.data = data
    @classmethod
    def __class_getitem__(cls, item):
        return cls

setattr(update_coordinator_mod, "DataUpdateCoordinator", DataUpdateCoordinator)
setattr(update_coordinator_mod, "UpdateFailed", UpdateFailed)

storage_mod = types.ModuleType("homeassistant.helpers.storage")
class StoreStub:
    def __init__(self, hass, version, key):
        self._hass = hass
        self._version = version
        self._key = key
    async def async_load(self):
        return None
    async def async_save(self, state):
        return None
    @classmethod
    def __class_getitem__(cls, item):
        # Support subscription like Store[dict[str, Any]] used in code
        return cls

setattr(storage_mod, "Store", StoreStub)

# Minimal restore_state submodule used by some entities
restore_state_mod = types.ModuleType("homeassistant.helpers.restore_state")
class RestoreEntity:
    def __init__(self, *args, **kwargs):
        pass

setattr(restore_state_mod, "RestoreEntity", RestoreEntity)

# Attach helper submodules to ha
ha.helpers = types.ModuleType("homeassistant.helpers")
ha.helpers.device_registry = device_registry_mod
ha.helpers.entity = entity_mod
ha.helpers.entity_platform = entity_platform_mod
ha.helpers.update_coordinator = update_coordinator_mod
ha.helpers.storage = storage_mod
ha.helpers.restore_state = restore_state_mod

# Register modules in sys.modules so normal imports find them
sys.modules["homeassistant"] = ha
sys.modules["homeassistant.components"] = components
sys.modules["homeassistant.components.sensor"] = sensor_mod
sys.modules["homeassistant.config_entries"] = ha.config_entries
sys.modules["homeassistant.const"] = ha.const
sys.modules["homeassistant.core"] = ha.core
sys.modules["homeassistant.helpers"] = ha.helpers
sys.modules["homeassistant.helpers.device_registry"] = device_registry_mod
sys.modules["homeassistant.helpers.entity"] = entity_mod
sys.modules["homeassistant.helpers.entity_platform"] = entity_platform_mod
sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator_mod
sys.modules["homeassistant.helpers.storage"] = storage_mod
sys.modules["homeassistant.helpers.restore_state"] = restore_state_mod

# Do not stub external third-party packages (requests, bs4) here; prefer
# installing them into the test venv so real implementations are used.
