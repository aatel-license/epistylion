"""
epistylion.client
~~~~~~~~~~~~~~~~~
Gestisce le connessioni stdio verso i server MCP.
Ogni server gira come sottoprocesso; la comunicazione avviene via stdin/stdout
usando il protocollo MCP (JSON-RPC su stdio).

NOTE sull'architettura anyio/asyncio:
  stdio_client() è un async context manager di anyio che crea internamente
  un TaskGroup e cancel scope. Questi oggetti sono legati al task anyio in
  cui vengono creati e NON possono essere trasferiti tra task diversi.

  Per questo motivo ogni connessione viene tenuta viva dentro il suo task
  dedicato (un asyncio.Task) che rimane attivo per tutta la durata della
  sessione. Il task viene cancellato esplicitamente alla disconnessione.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import Tool

from config import ServerConfig

logger = logging.getLogger(__name__)


class MCPServerConnection:
    """
    Connessione attiva a un server MCP.

    Il lifecycle di stdio_client è interamente gestito dentro un asyncio.Task
    dedicato (_worker_task) che rimane vivo per tutta la sessione.
    La sincronizzazione con il task esterno avviene tramite Event.
    """

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.name = config.name
        self._tools: list[Tool] = []

        self._ready_event: asyncio.Event = asyncio.Event()
        self._stop_event: asyncio.Event = asyncio.Event()
        self._session: ClientSession | None = None
        self._connect_error: Exception | None = None
        self._worker_task: asyncio.Task | None = None

    @property
    def tools(self) -> list[Tool]:
        return self._tools

    @property
    def is_connected(self) -> bool:
        return self._session is not None

    async def connect(self, timeout: float = 30.0) -> None:
        """
        Avvia il worker task e attende che la connessione sia pronta.
        Tutto il codice anyio (stdio_client, TaskGroup, cancel scope)
        vive esclusivamente dentro il worker task.
        """
        self._worker_task = asyncio.create_task(
            self._worker(), name=f"mcp-worker-{self.name}"
        )

        try:
            await asyncio.wait_for(
                asyncio.shield(self._ready_event.wait()),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            self._stop_event.set()
            if self._worker_task:
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except (asyncio.CancelledError, Exception):
                    pass
            raise TimeoutError(f"[{self.name}] Timeout connessione ({timeout}s)")

        if self._connect_error is not None:
            raise self._connect_error

    async def _worker(self) -> None:
        """
        Worker task: apre stdio_client, inizializza la sessione,
        segnala ready e poi attende lo stop.
        Tutto il codice anyio è confinato qui dentro.
        """
        params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env=self.config.full_env(),
        )
        logger.info(
            "[%s] Avvio: %s %s",
            self.name,
            self.config.command,
            " ".join(self.config.args),
        )

        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    self._session = session
                    try:
                        await session.initialize()
                        result = await session.list_tools()
                        self._tools = result.tools
                        logger.info(
                            "[%s] %d tool disponibili", self.name, len(self._tools)
                        )
                    except Exception as exc:
                        self._connect_error = exc
                        self._ready_event.set()
                        return

                    # Connessione OK: segnala al chiamante
                    self._ready_event.set()

                    # Rimane vivo fino alla disconnessione
                    await self._stop_event.wait()

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[%s] Errore worker: %s", self.name, exc, exc_info=True)
            self._connect_error = exc
            self._ready_event.set()
        finally:
            self._session = None

    async def disconnect(self) -> None:
        """Segnala al worker di fermarsi e attende la sua terminazione."""
        self._stop_event.set()
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
        self._session = None

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Esegue un tool sul server."""
        if not self.is_connected:
            raise RuntimeError(f"Server '{self.name}' non connesso")
        logger.debug("[%s] call_tool(%s, %s)", self.name, tool_name, arguments)
        result = await self._session.call_tool(tool_name, arguments)
        return result.content


class MCPClient:
    """
    Client multi-server MCP.
    Gestisce il ciclo di vita di tutti i server e offre un'interfaccia
    unificata per la scoperta e l'invocazione dei tool.
    """

    def __init__(self) -> None:
        self._connections: dict[str, MCPServerConnection] = {}

    async def connect_all(
        self,
        servers: list[ServerConfig],
        timeout: float = 30.0,
    ) -> dict[str, Exception]:
        """
        Connette tutti i server in parallelo.
        Restituisce un dict con i server che hanno fallito.
        """
        errors: dict[str, Exception] = {}

        async def _connect_one(cfg: ServerConfig) -> None:
            conn = MCPServerConnection(cfg)
            try:
                await conn.connect(timeout=timeout)
                self._connections[cfg.name] = conn
                logger.info("[%s] Connesso ✓", cfg.name)
            except Exception as exc:
                logger.error(
                    "[%s] Connessione fallita: %s", cfg.name, exc, exc_info=True
                )
                errors[cfg.name] = exc

        await asyncio.gather(*[_connect_one(cfg) for cfg in servers])

        if not self._connections:
            logger.warning(
                "Nessun server MCP connesso! Errori: %s",
                {k: str(v) for k, v in errors.items()},
            )
        else:
            logger.info(
                "%d/%d server connessi: %s",
                len(self._connections),
                len(servers),
                list(self._connections.keys()),
            )

        return errors

    async def disconnect_all(self) -> None:
        """Disconnette tutti i server."""
        await asyncio.gather(
            *[conn.disconnect() for conn in self._connections.values()],
            return_exceptions=True,
        )
        self._connections.clear()

    @asynccontextmanager
    async def session(self, servers: list[ServerConfig], timeout: float = 30.0):
        errors = await self.connect_all(servers, timeout=timeout)
        try:
            yield errors
        finally:
            await self.disconnect_all()

    def get_connections(self) -> dict[str, MCPServerConnection]:
        return dict(self._connections)

    def get_all_tools(self) -> list[tuple[str, Tool]]:
        result = []
        for server_name, conn in self._connections.items():
            for tool in conn.tools:
                result.append((server_name, tool))
        return result

    def find_server_for_tool(self, tool_name: str) -> MCPServerConnection | None:
        for conn in self._connections.values():
            if any(t.name == tool_name for t in conn.tools):
                return conn
        return None

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        conn = self.find_server_for_tool(tool_name)
        if conn is None:
            raise ValueError(
                f"Nessun server connesso possiede il tool '{tool_name}'"
            )
        return await conn.call_tool(tool_name, arguments)