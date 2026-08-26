from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .models import Stage


class ConfigError(ValueError):
    """A user-actionable configuration error."""


@dataclass(frozen=True)
class EndpointConfig:
    id: str
    name: str
    url: str


@dataclass(frozen=True)
class CollectionConfig:
    interval_seconds: float
    timeout_seconds: float
    stale_after_seconds: float


@dataclass(frozen=True)
class ControlConfig:
    enabled_at_startup: bool
    max_speed_percent: int
    hysteresis_celsius: float
    emergency_temperature_celsius: float
    recovery_seconds: float
    stages: tuple[Stage, ...]


@dataclass(frozen=True)
class HardwareConfig:
    backend: str
    pwm_gpio_bcm: int
    pwm_frequency_hz: int
    pwm_inverted: bool
    tach_gpio_bcm: tuple[int, int]
    pulses_per_revolution: tuple[int, int]
    startup_boost_seconds: float
    stall_timeout_seconds: float


@dataclass(frozen=True)
class AppConfig:
    path: Path
    endpoints: tuple[EndpointConfig, ...]
    collection: CollectionConfig
    control: ControlConfig
    hardware: HardwareConfig


def resolve_config_path(explicit: str | None) -> Path:
    return Path(explicit or os.environ.get("DGX_FAN_CONFIG") or "config.toml")


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a TOML table")
    return value


def _number(value: object, name: str, *, minimum: float = 0) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) < minimum:
        raise ConfigError(f"{name} must be a number >= {minimum}")
    return float(value)


def _integer(value: object, name: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum or (maximum is not None and value > maximum):
        suffix = f" and <= {maximum}" if maximum is not None else ""
        raise ConfigError(f"{name} must be an integer >= {minimum}{suffix}")
    return value


def load_config(path: Path) -> AppConfig:
    try:
        with path.open("rb") as source:
            raw = tomllib.load(source)
    except FileNotFoundError as error:
        raise ConfigError(f"configuration file not found: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid TOML in {path}: {error}") from error
    if raw.get("version") != 1:
        raise ConfigError("version must be 1")
    raw_endpoints = raw.get("dgx")
    if not isinstance(raw_endpoints, list) or not 1 <= len(raw_endpoints) <= 2:
        raise ConfigError("dgx must contain one or two endpoint tables")
    endpoints: list[EndpointConfig] = []
    for index, entry in enumerate(raw_endpoints, start=1):
        item = _mapping(entry, f"dgx[{index}]")
        endpoint_id, name, url = item.get("id"), item.get("name"), item.get("url")
        if not all(isinstance(value, str) and value.strip() for value in (endpoint_id, name, url)):
            raise ConfigError(f"dgx[{index}] id, name, and url must be non-empty strings")
        assert isinstance(endpoint_id, str) and isinstance(name, str) and isinstance(url, str)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigError(f"dgx[{index}].url must be an http(s) URL")
        endpoints.append(EndpointConfig(endpoint_id, name, url))
    if len({item.id for item in endpoints}) != len(endpoints) or len({item.name for item in endpoints}) != len(endpoints):
        raise ConfigError("dgx ids and names must be unique")
    collection = _mapping(raw.get("collection"), "collection")
    collection_config = CollectionConfig(
        _number(collection.get("interval_seconds"), "collection.interval_seconds", minimum=0.1),
        _number(collection.get("timeout_seconds"), "collection.timeout_seconds", minimum=0.1),
        _number(collection.get("stale_after_seconds"), "collection.stale_after_seconds", minimum=0.1),
    )
    if collection_config.stale_after_seconds < collection_config.interval_seconds:
        raise ConfigError("collection.stale_after_seconds must be >= interval_seconds")
    control = _mapping(raw.get("control"), "control")
    stages_raw = control.get("stages")
    if not isinstance(stages_raw, list) or len(stages_raw) != 4:
        raise ConfigError("control.stages must contain exactly four stages")
    stages: list[Stage] = []
    previous_temperature = float("-inf")
    previous_speed = -1
    for index, entry in enumerate(stages_raw):
        item = _mapping(entry, f"control.stages[{index}]")
        maximum = item.get("max_temperature_celsius")
        if index == 3:
            if maximum is not None:
                raise ConfigError("the final control stage must not have max_temperature_celsius")
            temperature = None
        else:
            temperature = _number(maximum, f"control.stages[{index}].max_temperature_celsius")
            if temperature <= previous_temperature:
                raise ConfigError("control stage temperatures must be strictly ascending")
            previous_temperature = temperature
        speed = _integer(item.get("speed_percent"), f"control.stages[{index}].speed_percent", maximum=100)
        if speed < previous_speed:
            raise ConfigError("control stage speeds must be ascending")
        previous_speed = speed
        stages.append(Stage(temperature, speed))
    enabled = control.get("enabled_at_startup")
    if not isinstance(enabled, bool):
        raise ConfigError("control.enabled_at_startup must be true or false")
    control_config = ControlConfig(
        enabled, _integer(control.get("max_speed_percent"), "control.max_speed_percent", minimum=1, maximum=100),
        _number(control.get("hysteresis_celsius"), "control.hysteresis_celsius"),
        _number(control.get("emergency_temperature_celsius"), "control.emergency_temperature_celsius"),
        _number(control.get("recovery_seconds"), "control.recovery_seconds"), tuple(stages),
    )
    hardware = _mapping(raw.get("hardware"), "hardware")
    backend = hardware.get("backend")
    if backend not in {"fake", "raspberry-pi"}:
        raise ConfigError("hardware.backend must be fake or raspberry-pi")
    tach = hardware.get("tach_gpio_bcm")
    ppr = hardware.get("pulses_per_revolution")
    if not isinstance(tach, list) or not isinstance(ppr, list) or len(tach) != 2 or len(ppr) != 2:
        raise ConfigError("hardware tach_gpio_bcm and pulses_per_revolution must each contain two values")
    tach_pair = tuple(_integer(value, f"hardware.tach_gpio_bcm[{i}]") for i, value in enumerate(tach))
    ppr_pair = tuple(_integer(value, f"hardware.pulses_per_revolution[{i}]", minimum=1) for i, value in enumerate(ppr))
    pwm = _integer(hardware.get("pwm_gpio_bcm"), "hardware.pwm_gpio_bcm")
    if pwm in tach_pair or len(set(tach_pair)) != 2:
        raise ConfigError("PWM and tach GPIOs must be distinct")
    inverted = hardware.get("pwm_inverted")
    if not isinstance(inverted, bool):
        raise ConfigError("hardware.pwm_inverted must be true or false")
    return AppConfig(path, tuple(endpoints), collection_config, control_config, HardwareConfig(
        backend, pwm, _integer(hardware.get("pwm_frequency_hz"), "hardware.pwm_frequency_hz", minimum=1), inverted,
        (tach_pair[0], tach_pair[1]), (ppr_pair[0], ppr_pair[1]),
        _number(hardware.get("startup_boost_seconds"), "hardware.startup_boost_seconds"),
        _number(hardware.get("stall_timeout_seconds"), "hardware.stall_timeout_seconds", minimum=0.1),
    ))
