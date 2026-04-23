"""Configuration management and slot definitions."""

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
SLOTS_CONFIG_PATH = BASE_DIR / CONFIG_DIR / "slots_config.json"
STORE_CONFIG_PATH = BASE_DIR / CONFIG_DIR / "store_config.json"
ATTRIBUTES_CONFIG_PATH = BASE_DIR / CONFIG_DIR / "attributes_config.json"
HARDWARE_CONFIG_PATH = BASE_DIR / CONFIG_DIR / "hardware_config.json"
ENV_PATH = BASE_DIR / ".env"

SPIRIT_SLOTS = [f"Slot_{i}" for i in range(1, 9)]
MIXER_SLOTS = [f"Slot_{c}" for c in "ABCD"]
ALL_SLOTS = SPIRIT_SLOTS + MIXER_SLOTS


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def save_json(data: dict, path: Path):
    with open(path, "w") as f:
        json.dump(data, f, indent=4)


def init_missing_configs():
    """Create missing config files with defaults if they don't exist."""

    if not CONFIG_DIR.exists():
        CONFIG_DIR.mkdir()
    # Create config.json with all slots but empty
    if not SLOTS_CONFIG_PATH.exists():
        config_data = {
            "poll_interval_seconds": 5,
            "slot_mapping": {slot: "" for slot in ALL_SLOTS},
            "spirit_measures_cl": "2"
        }
        save_json(config_data, SLOTS_CONFIG_PATH)

    # Create store_config.json empty (will be populated from API)
    if not STORE_CONFIG_PATH.exists():
        store_data = {
            "last_fetched": None,
            "available_spirits": [],
            "available_mixers": [],
            "products": []
        }
        save_json(store_data, STORE_CONFIG_PATH)

    # Create attributes_config.json empty (will be populated from API)
    if not ATTRIBUTES_CONFIG_PATH.exists():
        attributes_data = {
            "attribute_ids": {},
            "attribute_slugs": {},
            "term_slugs": {},
            "bottle_properties": {}
        }
        save_json(attributes_data, ATTRIBUTES_CONFIG_PATH)

    # hardware_config.json must exist (user-defined machine geometry)
    if not HARDWARE_CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"hardware_config.json not found at {HARDWARE_CONFIG_PATH}. "
            "Please create it with slot positions and sensor settings."
        )


class BarbotConfig:
    """Hardware configuration: slot mapping and runtime slot state."""

    def __init__(self, data: dict, path: Path = SLOTS_CONFIG_PATH):
        self._path = path
        self.poll_interval: int = data.get("poll_interval_seconds", 5)
        self.slot_mapping: dict[str, str] = dict(data.get("slot_mapping", {}))
        self.empty_slots: set[str] = set()
        self._rebuild_lookup()

    @classmethod
    def load(cls, path: Path = SLOTS_CONFIG_PATH) -> "BarbotConfig":
        return cls(load_json(path), path)

    def save(self):
        save_json({
            "poll_interval_seconds": self.poll_interval,
            "slot_mapping": self.slot_mapping,
        }, self._path)

    def _rebuild_lookup(self):
        self.ingredient_to_slot: dict[str, str] = {
            v: k for k, v in self.slot_mapping.items()
        }

    def set_slot(self, slot: str, ingredient: str):
        self.slot_mapping[slot] = ingredient
        self._rebuild_lookup()

    def clear_slot(self, slot: str):
        self.slot_mapping.pop(slot, None)
        self._rebuild_lookup()

    def mounted_ingredients(self) -> set[str]:
        return {
            ing for slot, ing in self.slot_mapping.items()
            if slot not in self.empty_slots
        }

    def slot_ingredient(self, slot: str) -> str | None:
        return self.slot_mapping.get(slot)

    @staticmethod
    def is_spirit_slot(slot: str) -> bool:
        return slot in SPIRIT_SLOTS

    @staticmethod
    def is_mixer_slot(slot: str) -> bool:
        return slot in MIXER_SLOTS

    @staticmethod
    def is_valid_slot(slot: str) -> bool:
        return slot in ALL_SLOTS


class AttributesConfig:
    """Cached WooCommerce attributes with bottle properties."""

    def __init__(self, data: dict, path: Path = ATTRIBUTES_CONFIG_PATH):
        self._path = path
        self._data = data

    @classmethod
    def load(cls, path: Path = ATTRIBUTES_CONFIG_PATH) -> "AttributesConfig":
        try:
            return cls(load_json(path), path)
        except FileNotFoundError:
            return cls({}, path)

    def save(self):
        save_json(self._data, self._path)

    # -- Properties ----------------------------------------------------------

    @property
    def attribute_ids(self) -> dict[str, int]:
        """Map attribute slug to attribute ID."""
        return self._data.get("attribute_ids", {})

    @property
    def attribute_slugs(self) -> dict[str, str]:
        """Map display name to attribute slug."""
        return self._data.get("attribute_slugs", {})

    @property
    def term_slugs(self) -> dict[str, dict[str, str]]:
        """Map {attr_slug: {term_name: term_slug}}."""
        return self._data.get("term_slugs", {})

    @property
    def bottle_properties(self) -> dict[str, dict[str, dict]]:
        """Map {attr_slug: {term_slug: {id, name, bottle_size, viscosity}}}."""
        return self._data.get("bottle_properties", {})

    # -- Setters ----------------------------------------------------------

    def update_attributes(self, attribute_ids: dict, attribute_slugs: dict,
                         term_slugs: dict, bottle_properties: dict):
        """Update all attribute-related data."""
        self._data["attribute_ids"] = attribute_ids
        self._data["attribute_slugs"] = attribute_slugs
        self._data["term_slugs"] = term_slugs
        self._data["bottle_properties"] = bottle_properties


class HardwareConfig:
    """Machine geometry and hardware parameters loaded from hardware_config.json."""

    def __init__(self, data: dict, path: Path = HARDWARE_CONFIG_PATH):
        self._path = path
        x = data.get("x_axis", {})
        self.x_max: int = x.get("max_steps", 6000)
        self.x_idle: int = x.get("idle_position", 3000)
        self.steps_per_mm: float = x.get("steps_per_mm", 10)

        self.slot_positions: dict[str, int] = data.get("slot_positions", {})

        optic = data.get("spirit_optic", {})
        self.pour_duration_ms: int = optic.get("pour_duration_ms", 1500)
        self.settle_duration_ms: int = optic.get("settle_duration_ms", 500)
        self.servo_pour_angle: int = optic.get("pour_angle", 90)
        self.servo_close_angle: int = optic.get("close_angle", 180)
        self.servo_settle_ms: int = optic.get("servo_settle_ms", 100)

        self.x_accel: int | None = x.get("accel")
        self.x_max_speed: int | None = x.get("max_speed")

        self.forbidden_servo_zones: list[dict] = data.get("forbidden_servo_zones", [])
        self.pump_tubing_compensation_g: float = data.get("pump_tubing_compensation_g", 0.0)

        hat = data.get("hat_serial", data.get("serial", {}))  # fallback to old key
        self.serial_port: str | None = hat.get("port", None)
        self.serial_baud: int = hat.get("baud", 115200)

        pump = data.get("pump_serial", {})
        self.pump_port: str | None = pump.get("port", None)
        self.pump_baud: int = pump.get("baud", 115200)

        neo = data.get("neopixel_serial", {})
        self.neo_port: str | None = neo.get("port", None)
        self.neo_baud: int = neo.get("baud", 115200)

    @classmethod
    def load(cls, path: Path = HARDWARE_CONFIG_PATH) -> "HardwareConfig":
        return cls(load_json(path), path)

    def in_forbidden_servo_zone(self, position: int) -> "str | None":
        """Return zone label if position is forbidden for servo opening, else None."""
        for z in self.forbidden_servo_zones:
            if z.get("min", 0) <= position <= z.get("max", 0):
                return z.get("label", "forbidden zone")
        return None

    def position_for_slot(self, slot: str) -> int | None:
        return self.slot_positions.get(slot)

    def set_slot_position(self, slot: str, steps: int):
        self.slot_positions[slot] = steps

    def save(self):
        x_axis: dict = {
            "max_steps":     self.x_max,
            "idle_position": self.x_idle,
            "steps_per_mm":  self.steps_per_mm,
        }
        if self.x_accel is not None:
            x_axis["accel"] = self.x_accel
        if self.x_max_speed is not None:
            x_axis["max_speed"] = self.x_max_speed

        save_json({
            "x_axis": x_axis,
            "slot_positions": self.slot_positions,
            "pump_tubing_compensation_g": self.pump_tubing_compensation_g,
            "spirit_optic": {
                "pour_duration_ms":   self.pour_duration_ms,
                "settle_duration_ms": self.settle_duration_ms,
                "pour_angle":         self.servo_pour_angle,
                "close_angle":        self.servo_close_angle,
                "servo_settle_ms":    self.servo_settle_ms,
            },
            "forbidden_servo_zones": self.forbidden_servo_zones,
            "hat_serial": {
                "port": self.serial_port,
                "baud": self.serial_baud,
            },
            "pump_serial": {
                "port": self.pump_port,
                "baud": self.pump_baud,
            },
            "neopixel_serial": {
                "port": self.neo_port,
                "baud": self.neo_baud,
            },
        }, self._path)
