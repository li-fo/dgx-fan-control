from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from ipaddress import ip_address
from math import isfinite
from pathlib import Path
from urllib.parse import urlparse

from rich.color import Color, ColorParseError

from .models import Stage


class ConfigError(ValueError):
    """A user-actionable configuration error."""


@dataclass(frozen=True)
class EndpointConfig:
    id: str
    name: str
    url: str
    memory_source: str = "dcgm"
    node_exporter_url: str | None = None


@dataclass(frozen=True)
class CollectionConfig:
    interval_seconds: float
    timeout_seconds: float
    stale_after_seconds: float
    retry_count: int = 0
    retry_delay_seconds: float = 10.0


@dataclass(frozen=True)
class ControlConfig:
    enabled_at_startup: bool
    max_speed_percent: int
    hysteresis_celsius: float
    emergency_temperature_celsius: float
    recovery_seconds: float
    stages: tuple[Stage, ...]
    fan_endpoint_ids: tuple[str, str]
    fallback_speed_percent: int = 100
    fan_mode: str = "independent"


@dataclass(frozen=True)
class HardwareConfig:
    backend: str
    pwm_gpio_bcm: tuple[int, int]
    pwm_frequency_hz: int
    pwm_inverted: bool
    tach_gpio_bcm: tuple[int, int]
    pulses_per_revolution: tuple[int, int]
    startup_boost_seconds: float
    stall_timeout_seconds: float
    pwm_chip_path: str = "/sys/class/pwm/pwmchip0"
    gpio_chip_path: str = "/dev/gpiochip0"
    shutdown_mode: str = "full"


@dataclass(frozen=True)
class DashboardColors:
    """Optional foreground colors for the dashboard's three chart metrics."""

    memory: str | None = None
    utilization: str | None = None
    temperature: str | None = None


@dataclass(frozen=True)
class WebConfig:
    """Optional browser monitor transport settings for loopback or a trusted LAN."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    socket_path: Path | None = None
    allow_control: bool = False


@dataclass(frozen=True)
class AppConfig:
    path: Path
    endpoints: tuple[EndpointConfig, ...]
    collection: CollectionConfig
    control: ControlConfig
    hardware: HardwareConfig
    dashboard_colors: DashboardColors = field(default_factory=DashboardColors)
    web: WebConfig = field(default_factory=WebConfig)


def resolve_config_path(explicit: str | None) -> Path:
    return Path(explicit or os.environ.get("DGX_FAN_CONFIG") or "config.toml")


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a TOML table")
    return value


def _number(value: object, name: str, *, minimum: float = 0) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"{name} must be a finite number >= {minimum}")
    numeric = float(value)
    if not isfinite(numeric) or numeric < minimum:
        raise ConfigError(f"{name} must be a finite number >= {minimum}")
    return numeric


def _integer(value: object, name: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        suffix = f" and <= {maximum}" if maximum is not None else ""
        raise ConfigError(f"{name} must be an integer >= {minimum}{suffix}")
    return value


_COLOR_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}\Z")


def _reject_unknown_keys(table: dict[str, object], name: str, allowed: set[str]) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"{name} contains unknown key: {unknown[0]}")


def _dashboard_color(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} must be a non-empty foreground color name or #RRGGBB")
    normalized = value.lower()
    if normalized == "default" or not (_COLOR_NAME.fullmatch(value) or _HEX_COLOR.fullmatch(value)):
        raise ConfigError(f"{name} must be a foreground color name or exact #RRGGBB")
    try:
        Color.parse(value)
    except ColorParseError as error:
        raise ConfigError(f"{name} must be a valid Rich foreground color") from error
    return normalized


def _dashboard_colors(raw: dict[str, object]) -> DashboardColors:
    dashboard_raw = raw.get("dashboard")
    if dashboard_raw is None:
        return DashboardColors()
    dashboard = _mapping(dashboard_raw, "dashboard")
    _reject_unknown_keys(dashboard, "dashboard", {"colors"})
    colors_raw = dashboard.get("colors")
    if colors_raw is None:
        return DashboardColors()
    colors = _mapping(colors_raw, "dashboard.colors")
    _reject_unknown_keys(colors, "dashboard.colors", {"memory", "utilization", "temperature"})
    return DashboardColors(
        **{key: _dashboard_color(value, f"dashboard.colors.{key}") for key, value in colors.items()}
    )


def _web_config(raw: dict[str, object], config_path: Path) -> WebConfig:
    web_raw = raw.get("web")
    default_socket = (config_path.parent / ".dgx-fan-monitor.sock").resolve()
    if web_raw is None:
        return WebConfig(socket_path=default_socket)
    web = _mapping(web_raw, "web")
    _reject_unknown_keys(web, "web", {"enabled", "host", "port", "socket_path", "allow_control"})
    enabled = web.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("web.enabled must be true or false")
    host = web.get("host", "127.0.0.1")
    if not isinstance(host, str):
        raise ConfigError("web.host must be a numeric IP address")
    try:
        address = ip_address(host)
    except ValueError as error:
        raise ConfigError("web.host must be a numeric IP address") from error
    if not address.is_loopback and host != "0.0.0.0":
        raise ConfigError(
            "web.host must be an IPv4 or IPv6 loopback address, or exactly 0.0.0.0 for a trusted LAN"
        )
    port = _integer(web.get("port", 8000), "web.port", minimum=1, maximum=65535)
    socket_raw = web.get("socket_path")
    if socket_raw is None:
        socket_path = default_socket
    elif not isinstance(socket_raw, str) or not socket_raw or not socket_raw.startswith("/"):
        raise ConfigError("web.socket_path must be an absolute non-empty path")
    else:
        socket_path = Path(socket_raw)
    allow_control = web.get("allow_control", False)
    if not isinstance(allow_control, bool):
        raise ConfigError("web.allow_control must be true or false")
    return WebConfig(enabled, host, port, socket_path, allow_control)


def load_config(path: Path) -> AppConfig:
    try:
        with path.open("rb") as source:
            raw = tomllib.load(source)
    except FileNotFoundError as error:
        raise ConfigError(f"configuration file not found: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid TOML in {path}: {error}") from error
    version = raw.get("version")
    if version != 2:
        if version == 1:
            raise ConfigError(
                "version 1 shared-PWM configuration is unsupported; migrate to version = 2, "
                "set hardware.pwm_gpio_bcm = [18, 19], and set control.fan_endpoint_ids"
            )
        raise ConfigError("version must be 2")
    dashboard_colors = _dashboard_colors(raw)
    web_config = _web_config(raw, path)
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
        memory_source = item.get("memory_source", "dcgm")
        node_exporter_url = item.get("node_exporter_url")
        if not isinstance(memory_source, str) or memory_source not in {"dcgm", "node-exporter"}:
            raise ConfigError(f"dgx[{index}].memory_source must be dcgm or node-exporter")
        if node_exporter_url is not None:
            if not isinstance(node_exporter_url, str) or not node_exporter_url.strip():
                raise ConfigError(f"dgx[{index}].node_exporter_url must be a non-empty http(s) URL")
            node_parsed = urlparse(node_exporter_url)
            if node_parsed.scheme not in {"http", "https"} or not node_parsed.netloc:
                raise ConfigError(f"dgx[{index}].node_exporter_url must be an http(s) URL")
        if memory_source == "node-exporter" and node_exporter_url is None:
            raise ConfigError(f"dgx[{index}].node_exporter_url is required when memory_source is node-exporter")
        if memory_source == "dcgm" and node_exporter_url is not None:
            raise ConfigError(f"dgx[{index}].node_exporter_url requires memory_source = node-exporter")
        endpoints.append(EndpointConfig(endpoint_id, name, url, memory_source, node_exporter_url))
    if len({item.id for item in endpoints}) != len(endpoints) or len(
        {item.name for item in endpoints}
    ) != len(endpoints):
        raise ConfigError("dgx ids and names must be unique")
    collection = _mapping(raw.get("collection"), "collection")
    collection_config = CollectionConfig(
        _number(collection.get("interval_seconds"), "collection.interval_seconds", minimum=0.1),
        _number(collection.get("timeout_seconds"), "collection.timeout_seconds", minimum=0.1),
        _number(
            collection.get("stale_after_seconds"), "collection.stale_after_seconds", minimum=0.1
        ),
        _integer(collection.get("retry_count", 0), "collection.retry_count"),
        _number(
            collection.get("retry_delay_seconds", 10.0),
            "collection.retry_delay_seconds",
        ),
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
        speed = _integer(
            item.get("speed_percent"), f"control.stages[{index}].speed_percent", maximum=100
        )
        if speed < previous_speed:
            raise ConfigError("control stage speeds must be ascending")
        previous_speed = speed
        stages.append(Stage(temperature, speed))
    enabled = control.get("enabled_at_startup")
    if not isinstance(enabled, bool):
        raise ConfigError("control.enabled_at_startup must be true or false")
    fan_endpoint_ids = control.get("fan_endpoint_ids")
    if not isinstance(fan_endpoint_ids, list) or len(fan_endpoint_ids) != 2 or not all(
        isinstance(endpoint_id, str) and endpoint_id.strip() for endpoint_id in fan_endpoint_ids
    ):
        raise ConfigError("control.fan_endpoint_ids must contain exactly two non-empty DGX endpoint IDs")
    endpoint_ids = {endpoint.id for endpoint in endpoints}
    if any(endpoint_id not in endpoint_ids for endpoint_id in fan_endpoint_ids):
        raise ConfigError("control.fan_endpoint_ids must reference configured dgx IDs")
    fan_mode = control.get("fan_mode", "independent")
    if not isinstance(fan_mode, str) or fan_mode not in {"independent", "linked"}:
        raise ConfigError("control.fan_mode must be independent or linked")
    control_config = ControlConfig(
        enabled,
        _integer(
            control.get("max_speed_percent"), "control.max_speed_percent", minimum=1, maximum=100
        ),
        _number(control.get("hysteresis_celsius"), "control.hysteresis_celsius"),
        _number(
            control.get("emergency_temperature_celsius"), "control.emergency_temperature_celsius"
        ),
        _number(control.get("recovery_seconds"), "control.recovery_seconds"),
        tuple(stages),
        (fan_endpoint_ids[0], fan_endpoint_ids[1]),
        _integer(
            control.get("fallback_speed_percent", 100),
            "control.fallback_speed_percent",
            maximum=100,
        ),
        fan_mode,
    )
    hardware = _mapping(raw.get("hardware"), "hardware")
    backend = hardware.get("backend")
    if backend not in {"fake", "raspberry-pi"}:
        raise ConfigError("hardware.backend must be fake or raspberry-pi")
    tach = hardware.get("tach_gpio_bcm")
    ppr = hardware.get("pulses_per_revolution")
    if not isinstance(tach, list) or not isinstance(ppr, list) or len(tach) != 2 or len(ppr) != 2:
        raise ConfigError(
            "hardware tach_gpio_bcm and pulses_per_revolution must each contain two finite integer values"
        )
    tach_pair = tuple(
        _integer(value, f"hardware.tach_gpio_bcm[{i}]") for i, value in enumerate(tach)
    )
    ppr_pair = tuple(
        _integer(value, f"hardware.pulses_per_revolution[{i}]", minimum=1)
        for i, value in enumerate(ppr)
    )
    pwm_raw = hardware.get("pwm_gpio_bcm")
    if not isinstance(pwm_raw, list) or len(pwm_raw) != 2:
        raise ConfigError(
            "hardware.pwm_gpio_bcm must contain exactly two GPIOs (for example [18, 19]); "
            "version 2 does not support shared PWM"
        )
    pwm_pair = tuple(_integer(value, f"hardware.pwm_gpio_bcm[{i}]") for i, value in enumerate(pwm_raw))
    if len(set(pwm_pair)) != 2 or len(set(tach_pair)) != 2 or set(pwm_pair).intersection(tach_pair):
        raise ConfigError("PWM and tach GPIOs must be distinct")
    pwm_channels = ({12, 18}, {13, 19})
    if not all(any(gpio in channel for gpio in pwm_pair) for channel in pwm_channels):
        raise ConfigError(
            "hardware.pwm_gpio_bcm must use one GPIO from PWM0 (12 or 18) and one from PWM1 (13 or 19)"
        )
    inverted = hardware.get("pwm_inverted")
    if not isinstance(inverted, bool):
        raise ConfigError("hardware.pwm_inverted must be true or false")
    pwm_chip_path = hardware.get("pwm_chip_path", "/sys/class/pwm/pwmchip0")
    gpio_chip_path = hardware.get("gpio_chip_path", "/dev/gpiochip0")
    shutdown_mode = hardware.get("shutdown_mode", "full")
    if not isinstance(pwm_chip_path, str) or not pwm_chip_path.startswith("/"):
        raise ConfigError("hardware.pwm_chip_path must be an absolute path")
    if not isinstance(gpio_chip_path, str) or not gpio_chip_path.startswith("/"):
        raise ConfigError("hardware.gpio_chip_path must be an absolute path")
    if not isinstance(shutdown_mode, str) or shutdown_mode not in {"full", "off"}:
        raise ConfigError('hardware.shutdown_mode must be "full" or "off"')
    return AppConfig(
        path,
        tuple(endpoints),
        collection_config,
        control_config,
        HardwareConfig(
            backend,
            (pwm_pair[0], pwm_pair[1]),
            _integer(hardware.get("pwm_frequency_hz"), "hardware.pwm_frequency_hz", minimum=1),
            inverted,
            (tach_pair[0], tach_pair[1]),
            (ppr_pair[0], ppr_pair[1]),
            _number(hardware.get("startup_boost_seconds"), "hardware.startup_boost_seconds"),
            _number(
                hardware.get("stall_timeout_seconds"), "hardware.stall_timeout_seconds", minimum=0.1
            ),
            pwm_chip_path,
            gpio_chip_path,
            shutdown_mode,
        ),
        dashboard_colors,
        web_config,
    )
