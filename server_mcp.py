"""
mcp_bridge.server_mcp
~~~~~~~~~~~~~~~~~~~~~~
Espone tutti i tool MCP aggregati come un **server MCP HTTP/SSE**,
accessibile da qualsiasi client MCP sulla LAN (Claude Desktop, altri bridge, ecc.).

Il server fa da proxy/aggregatore:
    LAN client (MCP) ──HTTP SSE──► questo server ──stdio──► [blender, mnheme, scrapling, ...]

Avvio rapido::

    python -m mcp_bridge --serve-mcp
    python -m mcp_bridge --serve-mcp --host 0.0.0.0 --mcp-port 9000

Oppure da codice::

    from mcp_bridge.server_mcp import MCPProxyServer
    server = MCPProxyServer.from_config("mcp_servers.json")
    await server.run(host="0.0.0.0", port=9000)
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import (
    CallToolResult,
    TextContent,
    Tool,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route

from .bridge import MCPBridge
from .config import BridgeConfig, load_config
from .registry import mcp_result_to_string

logger = logging.getLogger(__name__)


class MCPProxyServer:
    """
    Server MCP HTTP/SSE che aggrega e ri-espone i tool di più server MCP locali.

    I client MCP remoti lo vedono come un unico server con tutti i tool
    dei server sottostanti (usando nomi qualificati per evitare collisioni).

    Parameters
    ----------
    config : BridgeConfig
    use_qualified_names : bool
        Se True (default), i tool sono esposti come 'server__tool'.
        Necessario quando più server hanno tool con lo stesso nome.
    """

    def __init__(
        self,
        config: BridgeConfig,
        use_qualified_names: bool = True,
    ) -> None:
        self._config = config
        self._use_qualified_names = use_qualified_names
        self._bridge: MCPBridge | None = None
        self._mcp_server = Server("mcp-bridge-proxy")
        self._ready = asyncio.Event()

    # ── factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_config(
        cls,
        mcp_config_path: str | Path | None = None,
        env_path: str | Path | None = None,
        use_qualified_names: bool = True,
    ) -> "MCPProxyServer":
        config = load_config(mcp_config_path, env_path)
        return cls(config, use_qualified_names)

    # ── avvio ─────────────────────────────────────────────────────────────────

    async def run(
        self,
        host: str = "0.0.0.0",
        port: int = 9000,
    ) -> None:
        """Connette i server MCP locali e avvia il server HTTP/SSE."""
        import uvicorn

        # Connetti il bridge ai server MCP locali (senza LLM)
        self._bridge = MCPBridge(self._config)
        errors = await self._bridge.connect()
        if errors:
            logger.warning("Server MCP non connessi: %s", list(errors.keys()))

        # Registra i handler MCP
        self._register_handlers()
        self._ready.set()

        # Costruisci l'app Starlette con il transport SSE
        app = self._build_starlette_app()

        logger.info("MCP Proxy Server in ascolto su http://%s:%d/sse", host, port)
        config = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        try:
            await server.serve()
        finally:
            if self._bridge:
                await self._bridge.disconnect()

    # ── handler MCP ───────────────────────────────────────────────────────────

    def _register_handlers(self) -> None:
        """Registra list_tools e call_tool sul server MCP."""
        assert self._bridge is not None

        @self._mcp_server.list_tools()
        async def list_tools() -> list[Tool]:
            entries = self._bridge.registry.all_entries()
            result = []
            for entry in entries:
                tool = entry.tool
                name = entry.qualified_name if self._use_qualified_names else tool.name
                result.append(Tool(
                    name=name,
                    description=tool.description or f"Tool dal server '{entry.server_name}'",
                    inputSchema=tool.inputSchema or {"type": "object", "properties": {}},
                ))
            return result

        @self._mcp_server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
            assert self._bridge is not None

            # Risolvi nome → entry
            entry = self._bridge.registry.resolve(name)
            if entry is None:
                return [TextContent(type="text", text=f"[ERRORE] Tool '{name}' non trovato")]

            try:
                raw = await self._bridge.client.call_tool(entry.tool.name, arguments)
                text = mcp_result_to_string(raw)
            except Exception as exc:
                text = f"[ERRORE] {exc}"
                logger.error("Errore tool '%s': %s", name, exc)

            return [TextContent(type="text", text=text)]

    # ── Starlette app ─────────────────────────────────────────────────────────

    def _build_starlette_app(self) -> Starlette:
        sse_transport = SseServerTransport("/messages/")

        async def handle_sse(request: Request) -> Response:
            async with sse_transport.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await self._mcp_server.run(
                    streams[0],
                    streams[1],
                    self._mcp_server.create_initialization_options(),
                )
            return Response()

        async def handle_health(request: Request) -> Response:
            connected = list(self._bridge.client.get_connections().keys()) if self._bridge else []
            tool_count = len(self._bridge.registry.all_entries()) if self._bridge else 0
            body = json.dumps({
                "status": "ok",
                "connected_servers": connected,
                "total_tools": tool_count,
            })
            return Response(content=body, media_type="application/json")

        return Starlette(
            routes=[
                Route("/health", handle_health),
                Mount("/", app=sse_transport.handle_post_message),
                Route("/sse", handle_sse),
            ]
        )
