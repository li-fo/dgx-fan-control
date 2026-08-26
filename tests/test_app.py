import pytest

from dgx_fan.app import main


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--help"])
    assert error.value.code == 0
    assert "--config" in capsys.readouterr().out


def test_invalid_config_fails_before_hardware() -> None:
    with pytest.raises(SystemExit, match="configuration error"):
        main(["--config", "does-not-exist.toml"])
