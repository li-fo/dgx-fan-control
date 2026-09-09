from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path

import httpx

from dgx_fan.app import DGXFanApp
from dgx_fan.config import EndpointConfig, load_config
from dgx_fan.dcgm import DCGMCollector
from dgx_fan.history import HistoryService
from dgx_fan.monitor_app import DGXFanMonitorApp
from dgx_fan.settings import SettingsCommandServer, control_socket_path


async def _request(path: Path, request: dict[str, object]) -> dict[str, object]:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write((json.dumps(request) + "\n").encode())
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), 2))
        assert isinstance(response, dict)
        return response
    finally:
        writer.close()
        await writer.wait_closed()


def test_primary_history_construction_does_not_create_database(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(Path("config.example.toml").read_text())
    DGXFanApp(load_config(config_path))
    assert not (tmp_path / "data" / "history.sqlite3").exists()


def test_dcgm_response_reaches_history_through_read_only_unix_socket(
    tmp_path: Path, monkeypatch: object
) -> None:
    async def exercise() -> None:
        endpoint = EndpointConfig("one", "One", "http://test/metrics")
        history = HistoryService(tmp_path / "config.toml", (endpoint,))
        await history.start()
        captured = time.time()

        def archive(_endpoint: EndpointConfig, text: str | None, error: str | None) -> None:
            history.submit("one", "dcgm", text, captured, 2.0, error)

        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                text="""# TYPE DCGM_FI_DEV_GPU_TEMP gauge
DCGM_FI_DEV_GPU_TEMP{UUID=\"gpu-0\"} 50
DCGM_FI_DEV_GPU_UTIL{UUID=\"gpu-0\"} 30
DCGM_FI_DEV_FB_USED{UUID=\"gpu-0\"} 100
DCGM_FI_DEV_FB_FREE{UUID=\"gpu-0\"} 900
DCGM_FI_DEV_FB_RESERVED{UUID=\"gpu-0\"} 0
""",
            )
        )
        original_client = httpx.AsyncClient

        def client(*args: object, **kwargs: object) -> httpx.AsyncClient:
            return original_client(*args, transport=transport, **kwargs)

        monkeypatch.setattr("dgx_fan.dcgm.httpx.AsyncClient", client)
        collector = DCGMCollector((endpoint,), 1, 10, archive_callback=archive)
        assert (await collector.collect_endpoint(endpoint)).healthy

        class Service:
            source_id = "owner"

            def response(self) -> dict[str, object]:
                return {"ok": True}

            def begin_close(self) -> None:
                return None

            async def close(self, _grace: float = 2.0) -> bool:
                return True

        monitor_socket = tmp_path / "monitor.sock"
        socket_path = control_socket_path(monitor_socket)
        server = SettingsCommandServer(socket_path, Service(), False, history.query)  # type: ignore[arg-type]
        await server.start()
        try:
            config_path = tmp_path / "monitor-config.toml"
            config_path.write_text(Path("config.example.toml").read_text())
            monitor = DGXFanMonitorApp(
                replace(load_config(config_path), web=replace(load_config(config_path).web, socket_path=monitor_socket))
            )
            async with asyncio.timeout(2):
                attempt = 0
                while True:
                    attempt += 1
                    response = await _request(
                        socket_path,
                        {
                            "version": 1,
                            "operation": "read-history",
                            "request_id": f"one-{attempt}",
                            "source_id": "",
                            "endpoint_id": "one",
                            "start": captured - 1,
                            "end": captured + 1,
                            "width": 4,
                        },
                    )
                    if any(value is not None for value in response["series"]["temperature"]):
                        break
                    await asyncio.sleep(0.01)
            assert response["ok"] is True
            assert response["series"]["power"] == [None] * 4
            from_monitor = await monitor.query_history("one", captured - 1, captured + 1, 4)
            assert from_monitor["ok"] is True and from_monitor["endpoint_id"] == "one"
        finally:
            await server.close()
            await history.close()

    asyncio.run(exercise())
