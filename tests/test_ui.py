from pathlib import Path

from textual.widgets import Button

from dgx_fan.app import DGXFanApp
from dgx_fan.config import load_config


def test_ui_has_tabs_and_power_toggle() -> None:
    config = load_config(Path("config.example.toml"))
    app = DGXFanApp(config)
    async def exercise() -> None:
        async with app.run_test() as pilot:
            assert app.query_one("#dashboard")
            assert app.query_one("#fan-control")
            app.query_one("#power-toggle", Button).press()
            await pilot.pause()
            assert not app.controller.power
    import asyncio
    asyncio.run(exercise())
