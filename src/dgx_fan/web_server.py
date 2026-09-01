"""Request-aware adapter for the read-only textual-serve browser endpoint."""

from __future__ import annotations

from typing import Any

import aiohttp_jinja2
from aiohttp import web
from textual_serve.server import Server, to_int


class RequestOriginServer(Server):
    """Render browser URLs from the request, never from the listener bind host.

    ``0.0.0.0`` is a valid address on which to listen, but it is not a browser
    destination.  textual-serve stores that bind value in ``public_url``;
    override only the index context so each page points back at the origin the
    client actually requested.  Forwarded proxy headers are deliberately not
    used: this project does not configure or trust a proxy boundary.
    """

    @aiohttp_jinja2.template("app_index.html")
    async def handle_index(self, request: web.Request) -> dict[str, Any]:
        return self._index_context(request)

    def _index_context(self, request: web.Request) -> dict[str, Any]:
        """Build the template context from this request's direct origin."""
        router = request.app.router
        font_size = to_int(request.query.get("fontsize", "16"), 16)
        origin = f"{request.scheme}://{request.host}"

        def get_url(route: str, **args: str) -> str:
            path = router[route].url_for(**args)
            return f"{origin}{path}"

        def get_websocket_url(route: str, **args: str) -> str:
            url = get_url(route, **args)
            scheme = "wss" if request.scheme == "https" else "ws"
            return f"{scheme}:{url.split(':', 1)[1]}"

        return {
            "font_size": font_size,
            "app_websocket_url": get_websocket_url("websocket"),
            "config": {"static": {"url": get_url("static", filename="/").rstrip("/") + "/"}},
            "application": {"name": self.title},
        }
