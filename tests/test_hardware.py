import sys
import threading
from errno import EBUSY
from pathlib import Path
from types import SimpleNamespace

import pytest

from dgx_fan import hardware as hardware_module
from dgx_fan.config import HardwareConfig
from dgx_fan.hardware import FakeHardware, RaspberryPiHardware
from dgx_fan.models import FanReading


def _config(chip: Path = Path("/sys/class/pwm/pwmchip0"), gpio: str = "/dev/gpiochip0") -> HardwareConfig:
    return HardwareConfig(
        "raspberry-pi", (18, 19), 25000, False, (23, 24), (2, 2), 1, 5, str(chip), gpio
    )


def _pwm_chip(tmp_path: Path) -> Path:
    chip = tmp_path / "pwmchip0"
    chip.mkdir()
    for channel in (0, 1):
        pwm = chip / f"pwm{channel}"
        pwm.mkdir()
        for name in ("enable", "period", "duty_cycle"):
            (pwm / name).write_text("")
    (chip / "export").write_text("")
    return chip


class _Request:
    def __init__(self) -> None:
        self.released = False
        self.calls = 0

    def wait_edge_events(self, timeout: float) -> bool:
        self.calls += 1
        assert isinstance(timeout, float) and 0 < timeout <= 1
        return self.calls == 1

    def read_edge_events(self) -> list[object]:
        return [SimpleNamespace(line_offset=23), SimpleNamespace(line_offset=23), SimpleNamespace(line_offset=24)]

    def release(self) -> None:
        self.released = True


def _gpiod(request: _Request) -> SimpleNamespace:
    def settings(**kwargs: object) -> dict[str, object]:
        assert kwargs == {"direction": "input", "edge_detection": "falling", "bias": "pull-up"}
        return kwargs

    return SimpleNamespace(
        LineSettings=settings,
        line=SimpleNamespace(
            Direction=SimpleNamespace(INPUT="input"),
            Edge=SimpleNamespace(FALLING="falling"),
            Bias=SimpleNamespace(PULL_UP="pull-up"),
        ),
        request_lines=lambda path, **kwargs: request,
    )


def test_fake_hardware_safe_release() -> None:
    hardware = FakeHardware()
    hardware.set_duties((20, 80))
    hardware.release()
    hardware.release()
    assert hardware.released and hardware.duties == (100, 100)


def test_fake_hardware_stops_only_for_opted_in_normal_shutdown() -> None:
    hardware = FakeHardware("off")
    hardware.release(normal_shutdown=True)
    assert hardware.duties == (0, 0)
    hardware.set_duties((20, 80))
    hardware.release(normal_shutdown=False)
    assert hardware.duties == (100, 100)


def test_fake_hardware_simulates_running_and_allows_stall_probe() -> None:
    hardware = FakeHardware()
    hardware.set_duties((50, 0))
    assert tuple(fan.state for fan in hardware.readings(0)) == ("RUNNING", "STOPPED")
    assert tuple(fan.rpm for fan in hardware.readings(0)) == (600.0, 0)
    hardware.set_readings((FanReading(None, "NO TACH"), FanReading(800, "RUNNING")))
    assert hardware.readings(1)[0].state == "NO TACH"


def test_pwm_channel_mapping_and_missing_overlay_error(tmp_path: Path) -> None:
    assert RaspberryPiHardware._channel_for_gpio(12) == RaspberryPiHardware._channel_for_gpio(18) == 0
    assert RaspberryPiHardware._channel_for_gpio(13) == RaspberryPiHardware._channel_for_gpio(19) == 1
    with pytest.raises(RuntimeError, match="dtoverlay=pwm-2chan"):
        RaspberryPiHardware(_config(tmp_path / "missing"))


def test_existing_pwm_channel_reconfiguration_order_and_inversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hardware = object.__new__(RaspberryPiHardware)
    hardware.config = HardwareConfig(
        "raspberry-pi", (18, 19), 25000, True, (23, 24), (2, 2), 1, 5, str(_pwm_chip(tmp_path))
    )
    hardware._period_ns = 40000
    hardware._pwm_paths = []
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        RaspberryPiHardware,
        "_write",
        staticmethod(lambda path, value, action: calls.append((path.name, value))),
    )
    hardware._prepare_pwm_channels()
    assert calls[:5] == [("enable", "0"), ("duty_cycle", "0"), ("period", "40000"), ("duty_cycle", "0"), ("enable", "1")]
    assert hardware._duty_ns(20) == 32000 and hardware._safe_duty_ns() == 0


def test_ebusy_export_waits_for_deterministically_delayed_pwm_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chip = tmp_path / "pwmchip0"
    chip.mkdir()
    (chip / "export").write_text("")
    hardware = object.__new__(RaspberryPiHardware)
    hardware.config = _config(chip)
    hardware._period_ns = 40000
    hardware._pwm_paths = []
    exports: list[str] = []
    polls: list[float] = []
    original_write_text = Path.write_text

    def write_text(path: Path, value: str, *args: object, **kwargs: object) -> int:
        if path == chip / "export":
            exports.append(value)
            raise OSError(EBUSY, "already exported")
        return original_write_text(path, value, *args, **kwargs)

    def delayed_sleep(seconds: float) -> None:
        polls.append(seconds)
        path = chip / f"pwm{len(polls) - 1}"
        path.mkdir()
        for name in ("enable", "period", "duty_cycle"):
            original_write_text(path / name, "")

    monkeypatch.setattr(Path, "write_text", write_text)
    monkeypatch.setattr(hardware_module.time, "sleep", delayed_sleep)
    hardware._prepare_pwm_channels()
    assert exports == ["0", "1"]
    assert polls == [0.01, 0.01]
    assert [path.name for path in hardware._pwm_paths] == ["pwm0", "pwm1"]
    assert all((path / "enable").read_text() == "1" for path in hardware._pwm_paths)


def test_second_channel_setup_failure_safe_fulls_every_discovered_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chip = _pwm_chip(tmp_path)
    writes: list[tuple[str, str, str]] = []
    failed = True

    def write(path: Path, value: str, action: str) -> None:
        nonlocal failed
        writes.append(("cleanup" if not failed else "setup", str(path), value))
        if path == chip / "pwm1" / "period" and failed:
            failed = False
            writes.append(("failure", "marker", "marker"))
            raise RuntimeError("second channel period failed")

    monkeypatch.setattr(RaspberryPiHardware, "_write", staticmethod(write))
    with pytest.raises(RuntimeError, match="second channel period failed"):
        RaspberryPiHardware(_config(chip))
    marker = writes.index(("failure", "marker", "marker"))
    cleanup = {(path, value) for _phase, path, value in writes[marker + 1 :]}
    for channel in ("pwm0", "pwm1"):
        assert (str(chip / channel / "duty_cycle"), "40000") in cleanup
        assert (str(chip / channel / "enable"), "1") in cleanup


def test_linux_pwm_setup_tach_rpm_and_safe_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chip, request = _pwm_chip(tmp_path), _Request()
    monkeypatch.setitem(sys.modules, "gpiod", _gpiod(request))
    hardware = RaspberryPiHardware(_config(chip))
    for _ in range(20):
        if request.calls:
            break
        threading.Event().wait(0.01)
    assert (chip / "pwm0" / "period").read_text() == "40000"
    assert (chip / "pwm1" / "period").read_text() == "40000"
    assert (chip / "pwm0" / "enable").read_text() == "1"
    hardware.set_duties((20, 80))
    assert (chip / "pwm0" / "duty_cycle").read_text() == "8000"
    assert (chip / "pwm1" / "duty_cycle").read_text() == "32000"
    sample_at = hardware._last[0][1] + 1.0
    readings = hardware.readings(sample_at)
    assert readings[0].rpm == 60.0 and readings[1].rpm == 30.0
    hardware.release()
    hardware.release()
    assert request.released
    assert (chip / "pwm0" / "duty_cycle").read_text() == "40000"
    assert (chip / "pwm1" / "enable").read_text() == "1"


def test_partial_pwm_write_attempts_other_channel_then_both_full_speed(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = object.__new__(RaspberryPiHardware)
    hardware.config = _config()
    hardware._period_ns = 40000
    hardware._pwm_paths = (Path("one"), Path("two"))
    writes: list[tuple[str, str]] = []

    def write(path: Path, value: str, action: str) -> None:
        writes.append((str(path), value))
        if path == Path("one/duty_cycle") and value == "8000":
            raise OSError("first channel failed")

    monkeypatch.setattr(RaspberryPiHardware, "_write", staticmethod(write))
    with pytest.raises(RuntimeError, match="one or more fan PWM"):
        hardware.set_duties((20, 30))
    assert ("two/duty_cycle", "12000") in writes
    assert writes.count(("one/duty_cycle", "40000")) == 1
    assert writes.count(("two/duty_cycle", "40000")) == 1


def test_release_waits_before_request_close_and_keeps_pwm_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = object.__new__(RaspberryPiHardware)
    hardware.config = _config()
    hardware._period_ns = 40000
    hardware._pwm_paths = (Path("one"), Path("two"))
    hardware._release_complete = False
    hardware._tach_stop = threading.Event()
    hardware._tach_thread = None
    request = _Request()
    hardware._tach_request = request
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(RaspberryPiHardware, "_write", staticmethod(lambda path, value, action: writes.append((str(path), value))))
    hardware.release()
    assert request.released
    assert ("one/enable", "1") in writes and ("two/enable", "1") in writes


def test_clean_opt_in_release_writes_zero_and_keeps_pwm_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = object.__new__(RaspberryPiHardware)
    hardware.config = HardwareConfig(
        "raspberry-pi", (18, 19), 25000, False, (23, 24), (2, 2), 1, 5, shutdown_mode="off"
    )
    hardware._period_ns = 40000
    hardware._pwm_paths = [Path("one"), Path("two")]
    hardware._release_complete = False
    hardware._tach_stop = threading.Event()
    hardware._tach_thread = None
    hardware._tach_request = None
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        RaspberryPiHardware,
        "_write",
        staticmethod(lambda path, value, action: writes.append((str(path), value))),
    )
    hardware.release(normal_shutdown=True)
    assert ("one/duty_cycle", "0") in writes and ("two/duty_cycle", "0") in writes
    assert ("one/enable", "1") in writes and ("two/enable", "1") in writes


def test_partial_clean_off_failure_attempts_both_then_restores_both_full_and_releases_tach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware = object.__new__(RaspberryPiHardware)
    hardware.config = HardwareConfig(
        "raspberry-pi", (18, 19), 25000, False, (23, 24), (2, 2), 1, 5, shutdown_mode="off"
    )
    hardware._period_ns = 40000
    hardware._pwm_paths = [Path("one"), Path("two")]
    hardware._release_complete = False
    hardware._tach_stop = threading.Event()
    hardware._tach_thread = None
    request = _Request()
    hardware._tach_request = request
    writes: list[tuple[str, str]] = []

    def write(path: Path, value: str, action: str) -> None:
        writes.append((str(path), value))
        if path == Path("one/duty_cycle") and value == "0":
            raise OSError("cannot stop fan one")

    monkeypatch.setattr(RaspberryPiHardware, "_write", staticmethod(write))
    with pytest.raises(RuntimeError, match="failed cleanup"):
        hardware.release(normal_shutdown=True)
    assert ("two/duty_cycle", "0") in writes
    assert writes.count(("one/duty_cycle", "40000")) == 1
    assert writes.count(("two/duty_cycle", "40000")) == 1
    assert request.released


def test_clean_off_tach_join_failure_restores_both_fans_full(monkeypatch: pytest.MonkeyPatch) -> None:
    class AliveThread:
        def join(self, timeout: float) -> None:
            assert timeout == RaspberryPiHardware._TACH_JOIN_SECONDS

        def is_alive(self) -> bool:
            return True

    hardware = object.__new__(RaspberryPiHardware)
    hardware.config = HardwareConfig(
        "raspberry-pi", (18, 19), 25000, False, (23, 24), (2, 2), 1, 5, shutdown_mode="off"
    )
    hardware._period_ns = 40000
    hardware._pwm_paths = [Path("one"), Path("two")]
    hardware._release_complete = False
    hardware._tach_stop = threading.Event()
    hardware._tach_thread = AliveThread()
    request = _Request()
    hardware._tach_request = request
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        RaspberryPiHardware,
        "_write",
        staticmethod(lambda path, value, action: writes.append((str(path), value))),
    )
    with pytest.raises(RuntimeError, match="failed cleanup"):
        hardware.release(normal_shutdown=True)
    assert writes.count(("one/duty_cycle", "0")) == 1
    assert writes.count(("two/duty_cycle", "0")) == 1
    assert writes.count(("one/duty_cycle", "40000")) == 1
    assert writes.count(("two/duty_cycle", "40000")) == 1
    assert not request.released and not hardware._release_complete


def test_clean_off_request_release_failure_restores_full_then_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingRequest(_Request):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        def release(self) -> None:
            if self.fail:
                self.fail = False
                raise OSError("temporary request cleanup failure")
            super().release()

    hardware = object.__new__(RaspberryPiHardware)
    hardware.config = HardwareConfig(
        "raspberry-pi", (18, 19), 25000, False, (23, 24), (2, 2), 1, 5, shutdown_mode="off"
    )
    hardware._period_ns = 40000
    hardware._pwm_paths = [Path("one"), Path("two")]
    hardware._release_complete = False
    hardware._tach_stop = threading.Event()
    hardware._tach_thread = None
    request = FailingRequest()
    hardware._tach_request = request
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        RaspberryPiHardware,
        "_write",
        staticmethod(lambda path, value, action: writes.append((str(path), value))),
    )
    with pytest.raises(RuntimeError, match="failed cleanup"):
        hardware.release(normal_shutdown=True)
    assert request is hardware._tach_request and not hardware._release_complete
    assert writes.count(("one/duty_cycle", "40000")) == 1
    assert writes.count(("two/duty_cycle", "40000")) == 1
    hardware.release(normal_shutdown=True)
    assert request.released and hardware._release_complete


def test_release_retries_after_one_safe_full_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    hardware = object.__new__(RaspberryPiHardware)
    hardware.config = _config()
    hardware._period_ns = 40000
    hardware._pwm_paths = [Path("one"), Path("two")]
    hardware._release_complete = False
    hardware._tach_stop = threading.Event()
    hardware._tach_thread = None
    hardware._tach_request = None
    failed = True

    def write(path: Path, value: str, action: str) -> None:
        nonlocal failed
        if path == Path("one/duty_cycle") and failed:
            failed = False
            raise OSError("temporary")

    monkeypatch.setattr(RaspberryPiHardware, "_write", staticmethod(write))
    with pytest.raises(RuntimeError, match="cleanup"):
        hardware.release()
    assert not hardware._release_complete
    hardware.release()
    assert hardware._release_complete


def test_release_retries_a_failed_post_join_request_release(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingRequest(_Request):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        def release(self) -> None:
            if self.fail:
                self.fail = False
                raise OSError("temporary request cleanup failure")
            super().release()

    hardware = object.__new__(RaspberryPiHardware)
    hardware.config = _config()
    hardware._period_ns = 40000
    hardware._pwm_paths = [Path("one"), Path("two")]
    hardware._release_complete = False
    hardware._tach_stop = threading.Event()
    hardware._tach_thread = None
    request = FailingRequest()
    hardware._tach_request = request
    monkeypatch.setattr(RaspberryPiHardware, "_write", staticmethod(lambda *_args: None))
    with pytest.raises(RuntimeError, match="cleanup"):
        hardware.release()
    assert hardware._tach_request is request and not hardware._release_complete
    hardware.release()
    assert request.released and hardware._release_complete


def test_release_writes_safe_full_before_bounded_join_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    class AliveThread:
        def __init__(self) -> None:
            self.joined = False

        def join(self, timeout: float) -> None:
            assert order == ["one", "two"]
            self.joined = True

        def is_alive(self) -> bool:
            return True

    hardware = object.__new__(RaspberryPiHardware)
    hardware.config = _config()
    hardware._period_ns = 40000
    hardware._pwm_paths = [Path("one"), Path("two")]
    hardware._release_complete = False
    hardware._tach_stop = threading.Event()
    order: list[str] = []
    hardware._tach_thread = AliveThread()
    hardware._tach_request = None
    monkeypatch.setattr(
        RaspberryPiHardware,
        "_write",
        staticmethod(lambda path, value, action: order.append(path.parts[0]) if path.name == "duty_cycle" else None),
    )
    with pytest.raises(RuntimeError, match="cleanup"):
        hardware.release()
    assert order == ["one", "two"] and not hardware._release_complete


def test_missing_gpiod_is_actionable_and_constructor_restores_full_speed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chip = _pwm_chip(tmp_path)
    monkeypatch.setitem(sys.modules, "gpiod", None)
    with pytest.raises(RuntimeError, match=r"dgx-fan\[raspberry-pi\].*gpiod"):
        RaspberryPiHardware(_config(chip))
    assert (chip / "pwm0" / "duty_cycle").read_text() == "40000"
