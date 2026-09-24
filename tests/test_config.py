from pathlib import Path

import pytest

from dgx_fan.config import ConfigError, DashboardColors, load_config, resolve_config_path


def _config() -> str:
    return """version = 2
[[dgx]]
id = "one"
name = "One"
url = "http://one:9400/metrics"
[collection]
interval_seconds = 2
timeout_seconds = 1
stale_after_seconds = 6
[control]
fan_endpoint_ids = ["one", "one"]
enabled_at_startup = true
max_speed_percent = 90
hysteresis_celsius = 2
emergency_temperature_celsius = 75
recovery_seconds = 10
[[control.stages]]
max_temperature_celsius = 45
speed_percent = 20
[[control.stages]]
max_temperature_celsius = 55
speed_percent = 50
[[control.stages]]
max_temperature_celsius = 70
speed_percent = 80
[[control.stages]]
speed_percent = 100
[hardware]
backend = "fake"
pwm_gpio_bcm = [18, 19]
pwm_frequency_hz = 25000
pwm_inverted = true
tach_gpio_bcm = [23, 24]
pulses_per_revolution = [2, 2]
startup_boost_seconds = 1
stall_timeout_seconds = 5
"""


def test_resolution_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DGX_FAN_CONFIG", "environment.toml")
    assert resolve_config_path("flag.toml") == Path("flag.toml")
    assert resolve_config_path(None) == Path("environment.toml")
    monkeypatch.delenv("DGX_FAN_CONFIG")
    assert resolve_config_path(None) == Path("config.toml")


def test_load_valid_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config())
    config = load_config(path)
    assert config.control.stages[-1].max_temperature_celsius is None
    assert config.hardware.backend == "fake"
    assert config.hardware.pwm_gpio_bcm == (18, 19)
    assert config.hardware.pwm_chip_path == "/sys/class/pwm/pwmchip0"
    assert config.hardware.gpio_chip_path == "/dev/gpiochip0"
    assert config.hardware.shutdown_mode == "full"
    assert config.control.fan_endpoint_ids == ("one", "one")
    assert config.control.fallback_speed_percent == 100
    assert config.control.fan_mode == "independent"
    assert config.dashboard_colors == DashboardColors()
    assert config.collection.retry_count == 0
    assert config.collection.retry_delay_seconds == 10.0
    assert config.endpoints[0].memory_source == "dcgm"
    assert config.endpoints[0].node_exporter_url is None
    assert config.web.enabled is False
    assert config.web.host == "127.0.0.1"
    assert config.web.port == 8000
    assert config.web.allow_control is False
    assert config.web.socket_path == (path.parent / ".dgx-fan-monitor.sock").resolve()
    assert config.graph_view == "graph-1"


@pytest.mark.parametrize("view", ["graph-0", "Graph-2", 2, ["graph-2"]])
def test_rejects_invalid_dashboard_graph_view(tmp_path: Path, view: object) -> None:
    path = tmp_path / "config.toml"
    rendered = '["graph-2"]' if isinstance(view, list) else (repr(view) if not isinstance(view, str) else f'"{view}"')
    path.write_text(_config() + f"\n[dashboard]\ngraph_view = {rendered}\n")
    with pytest.raises(ConfigError, match=r"dashboard\.graph_view"):
        load_config(path)


def test_loads_graph_two(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config() + '\n[dashboard]\ngraph_view = "graph-2"\n')
    assert load_config(path).graph_view == "graph-2"


def test_loads_opt_in_web_monitor_settings(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        _config()
        + """
[web]
enabled = true
host = "::1"
port = 8123
socket_path = "/tmp/dgx-fan-monitor.sock"
allow_control = true
"""
    )
    web = load_config(path).web
    assert web.enabled is True
    assert web.host == "::1"
    assert web.port == 8123
    assert web.allow_control is True
    assert str(web.socket_path) == "/tmp/dgx-fan-monitor.sock"


def test_loads_explicit_trusted_lan_wildcard_web_monitor_setting(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config() + '\n[web]\nenabled = true\nhost = "0.0.0.0"\n')
    assert load_config(path).web.host == "0.0.0.0"


@pytest.mark.parametrize(
    ("extra", "match"),
    [
        ('enabled = "yes"', r"web\.enabled"),
        ('host = "localhost"', r"web\.host"),
        ('host = "192.168.0.0/24"', r"web\.host"),
        ('host = "192.168.1.20"', r"web\.host"),
        ('host = "::"', r"web\.host"),
        ('host = "fe80::1"', r"web\.host"),
        ('host = "127.0.0.1"\nport = 0', r"web\.port"),
        ('socket_path = "relative.sock"', r"web\.socket_path"),
        ('unknown = true', r"web contains unknown key"),
        ('allow_control = "yes"', r"web\.allow_control"),
    ],
)
def test_rejects_unsafe_web_monitor_settings(tmp_path: Path, extra: str, match: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config() + f"\n[web]\n{extra}\n")
    with pytest.raises(ConfigError, match=match):
        load_config(path)


def test_loads_node_exporter_memory_source(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        _config().replace(
            'url = "http://one:9400/metrics"',
            'url = "http://one:9400/metrics"\nmemory_source = "node-exporter"\nnode_exporter_url = "https://one:9100/metrics"',
        )
    )
    endpoint = load_config(path).endpoints[0]
    assert endpoint.memory_source == "node-exporter"
    assert endpoint.node_exporter_url == "https://one:9100/metrics"


@pytest.mark.parametrize(
    ("extra", "match"),
    [
        ('memory_source = "other"', r"memory_source"),
        ('memory_source = ["dcgm"]', r"memory_source"),
        ('memory_source = "node-exporter"', r"node_exporter_url is required"),
        ('node_exporter_url = "http://one:9100/metrics"', r"node_exporter_url requires"),
        ('memory_source = "node-exporter"\nnode_exporter_url = "ftp://one/metrics"', r"node_exporter_url"),
    ],
)
def test_rejects_invalid_node_exporter_combinations(
    tmp_path: Path, extra: str, match: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config().replace('[collection]', f'{extra}\n[collection]'))
    with pytest.raises(ConfigError, match=match):
        load_config(path)


def test_loads_optional_collection_retry_settings(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        _config().replace(
            "stale_after_seconds = 6",
            "stale_after_seconds = 6\nretry_count = 3\nretry_delay_seconds = 2.5",
        )
    )
    config = load_config(path)
    assert config.collection.retry_count == 3
    assert config.collection.retry_delay_seconds == 2.5


@pytest.mark.parametrize("value", [0, 35, 100])
def test_loads_optional_control_fallback_speed_percent(tmp_path: Path, value: int) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config().replace("max_speed_percent = 90", f"max_speed_percent = 90\nfallback_speed_percent = {value}"))
    assert load_config(path).control.fallback_speed_percent == value


@pytest.mark.parametrize("value", ["-1", "101", "1.5", "true", '"35"'])
def test_rejects_invalid_control_fallback_speed_percent(tmp_path: Path, value: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config().replace("max_speed_percent = 90", f"max_speed_percent = 90\nfallback_speed_percent = {value}"))
    with pytest.raises(ConfigError, match=r"control\.fallback_speed_percent"):
        load_config(path)


@pytest.mark.parametrize("value", ['"together"', "1", "true", "[\"linked\"]"])
def test_rejects_invalid_control_fan_mode(tmp_path: Path, value: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config().replace("max_speed_percent = 90", f"max_speed_percent = 90\nfan_mode = {value}"))
    with pytest.raises(ConfigError, match=r"control\.fan_mode"):
        load_config(path)


def test_loads_optional_control_fan_mode(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config().replace("max_speed_percent = 90", 'max_speed_percent = 90\nfan_mode = "linked"'))
    assert load_config(path).control.fan_mode == "linked"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("retry_count", "-1"),
        ("retry_count", "1.5"),
        ("retry_count", "true"),
        ("retry_delay_seconds", "-1"),
        ("retry_delay_seconds", "nan"),
        ("retry_delay_seconds", "true"),
    ],
)
def test_rejects_invalid_collection_retry_settings(
    tmp_path: Path, key: str, value: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config().replace("stale_after_seconds = 6", f"stale_after_seconds = 6\n{key} = {value}"))
    with pytest.raises(ConfigError, match=key):
        load_config(path)


def test_loads_optional_linux_device_paths_without_schema_change(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        _config()
        + """
pwm_chip_path = "/tmp/pwmchip0"
gpio_chip_path = "/tmp/gpiochip0"
"""
    )
    config = load_config(path)
    assert config.hardware.pwm_chip_path == "/tmp/pwmchip0"
    assert config.hardware.gpio_chip_path == "/tmp/gpiochip0"


def test_loads_opt_in_clean_shutdown_fan_off(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config() + '\nshutdown_mode = "off"\n')
    assert load_config(path).hardware.shutdown_mode == "off"


@pytest.mark.parametrize("value", ['"unexpected"', "true", "1", '["off"]', '{ mode = "off" }'])
def test_rejects_invalid_shutdown_mode(tmp_path: Path, value: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config() + f"\nshutdown_mode = {value}\n")
    with pytest.raises(ConfigError, match=r"hardware\.shutdown_mode"):
        load_config(path)


def test_loads_partial_dashboard_colors_and_normalizes_values(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        _config()
        + """
[dashboard.colors]
memory = "YELLOW"
temperature = "#38BDF8"
"""
    )
    config = load_config(path)
    assert config.dashboard_colors == DashboardColors(memory="yellow", temperature="#38bdf8")


@pytest.mark.parametrize(
    "value",
    [
        '""',
        '" default"',
        '"red "',
        '"default"',
        '"#fff"',
        '"rgb(1,2,3)"',
        '"color(1)"',
        '"bold red"',
        '"on red"',
        '"not-a-color"',
        "1",
        "true",
        '"red\\u001b[31m"',
    ],
)
def test_rejects_unsafe_or_ambiguous_dashboard_colors(tmp_path: Path, value: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config() + f"""\n[dashboard.colors]\nmemory = {value}\n""")
    with pytest.raises(ConfigError, match=r"dashboard\.colors\.memory"):
        load_config(path)


@pytest.mark.parametrize(
    "extra, expected",
    [
        ('dashboard = "red"', "dashboard must be a TOML table"),
        ('[dashboard]\ncolors = "red"', "dashboard.colors must be a TOML table"),
        ('[dashboard]\ncolour = "red"', "dashboard contains unknown key: colour"),
        ('[dashboard.colors]\nother = "red"', "dashboard.colors contains unknown key: other"),
    ],
)
def test_rejects_invalid_dashboard_tables_and_keys(
    tmp_path: Path, extra: str, expected: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config().replace("[[dgx]]", f"{extra}\n[[dgx]]"))
    with pytest.raises(ConfigError, match=expected):
        load_config(path)


def test_rejects_wrong_stage_count(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config().replace("[[control.stages]]\nspeed_percent = 100\n", ""))
    with pytest.raises(ConfigError, match="exactly four"):
        load_config(path)


@pytest.mark.parametrize(
    ("field", "existing"),
    [
        ("interval_seconds", "2"),
        ("timeout_seconds", "1"),
        ("stale_after_seconds", "6"),
        ("max_speed_percent", "90"),
        ("hysteresis_celsius", "2"),
        ("emergency_temperature_celsius", "75"),
        ("recovery_seconds", "10"),
        ("max_temperature_celsius", "45"),
        ("speed_percent", "20"),
        ("pwm_frequency_hz", "25000"),
        ("pulses_per_revolution", "[2, 2]"),
        ("startup_boost_seconds", "1"),
        ("stall_timeout_seconds", "5"),
    ],
)
@pytest.mark.parametrize("nonfinite", ["nan", "+inf", "-inf"])
def test_rejects_nonfinite_numeric_values(
    tmp_path: Path, field: str, existing: str, nonfinite: str
) -> None:
    path = tmp_path / "config.toml"
    content = _config().replace(f"{field} = {existing}", f"{field} = {nonfinite}", 1)
    path.write_text(content)
    with pytest.raises(ConfigError, match="finite|integer"):
        load_config(path)


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("version = 1", "shared-PWM configuration"),
        ('fan_endpoint_ids = ["one"]', "fan_endpoint_ids"),
        ('fan_endpoint_ids = ["missing", "one"]', "reference configured"),
        ("pwm_gpio_bcm = 18", "must contain exactly two GPIOs"),
        ("pwm_gpio_bcm = [18, 12]", "PWM0"),
        ("pwm_gpio_bcm = [18, 18]", "PWM and tach GPIOs"),
    ],
)
def test_rejects_unsafe_dual_fan_migration_inputs(
    tmp_path: Path, replacement: str, message: str
) -> None:
    path = tmp_path / "config.toml"
    target = "version = 2" if replacement.startswith("version") else (
        'fan_endpoint_ids = ["one", "one"]' if replacement.startswith("fan_endpoint") else "pwm_gpio_bcm = [18, 19]"
    )
    path.write_text(_config().replace(target, replacement, 1))
    with pytest.raises(ConfigError, match=message):
        load_config(path)
