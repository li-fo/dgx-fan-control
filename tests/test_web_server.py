from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path

from aiohttp import ClientSession, WSMsgType, web
from aiohttp.test_utils import make_mocked_request

from dgx_fan.web_server import RequestOriginServer


def test_request_origin_server_uses_request_host_not_wildcard_bind() -> None:
    async def exercise() -> None:
        server = RequestOriginServer("unused", host="0.0.0.0", port=8000)
        app = await server._make_app()
        request = make_mocked_request("GET", "/", headers={"Host": "192.168.1.7:8000"}, app=app)
        context = server._index_context(request)
        assert context["app_websocket_url"] == "ws://192.168.1.7:8000/ws"
        assert context["config"]["static"]["url"] == "http://192.168.1.7:8000/static/"
        assert "0.0.0.0" not in str(context)

    asyncio.run(exercise())


def test_request_origin_server_keeps_loopback_origin() -> None:
    async def exercise() -> None:
        server = RequestOriginServer("unused", host="0.0.0.0", port=8000)
        app = await server._make_app()
        request = make_mocked_request("GET", "/", headers={"Host": "127.0.0.1:8000"}, app=app)
        context = server._index_context(request)
        assert context["app_websocket_url"] == "ws://127.0.0.1:8000/ws"
        assert context["config"]["static"]["url"] == "http://127.0.0.1:8000/static/"

    asyncio.run(exercise())


def test_wildcard_listener_serves_request_origin_and_monitor_websocket(tmp_path: Path) -> None:
    """The rendered page and real WebSocket both work when bind host is wildcard."""

    async def exercise() -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text(Path("config.example.toml").read_text().replace("enabled = false", "enabled = true"))
        program = "from dgx_fan.monitor_app import main; main()"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(program)} --config {shlex.quote(str(config_path))}"
        server = RequestOriginServer(command, host="0.0.0.0", port=0)
        runner = web.AppRunner(await server._make_app())
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", 0)
        await site.start()
        socket = site._server.sockets[0]
        port = socket.getsockname()[1]
        try:
            async with ClientSession() as client:
                response = await asyncio.wait_for(client.get(f"http://127.0.0.1:{port}/"), timeout=4)
                page = await response.text()
                assert response.status == 200
                assert f"ws://127.0.0.1:{port}/ws" in page
                assert "0.0.0.0" not in page
                websocket = await asyncio.wait_for(client.ws_connect(f"http://127.0.0.1:{port}/ws"), timeout=4)
                try:
                    for _ in range(80):
                        message = await asyncio.wait_for(websocket.receive(), timeout=4)
                        if message.type == WSMsgType.BINARY and b"DGX Dashboard" in message.data:
                            break
                    else:
                        raise AssertionError("monitor WebSocket did not deliver a Textual frame")
                finally:
                    await websocket.close()
        finally:
            await asyncio.wait_for(runner.cleanup(), timeout=6)

    asyncio.run(exercise())
