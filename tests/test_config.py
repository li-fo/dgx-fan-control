from pathlib import Path

import pytest

from dgx_fan.config import ConfigError, DashboardColors, load_config, resolve_config_path


def _config() -> str:
    return """version = 1
[[dgx]]
id = "one"
name = "One"
url = "http://one:9400/metrics"
[collection]
interval_seconds = 2
timeout_seconds = 1
stale_after_seconds = 6
[control]
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
pwm_gpio_bcm = 18
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
    assert config.dashboard_colors == DashboardColors()


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
        ("pwm_gpio_bcm", "18"),
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
