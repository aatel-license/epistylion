"""
epistylion
~~~~~~~~~~~~~~~~~
Facade di alto livello: combina config, client, registry e agent
in un unico context manager facile da usare.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from agent import MCPAgent
from client import MCPClient
from config import BridgeConfig, LLMConfig, load_config
from registry import ToolRegistry

logger = logging.getLogger(__name__)
console = Console()


class MCPBridge:
    """
    Facade principale di epistylion.

    Gestisce l'intero ciclo di vita:
      - caricamento config
      - connessione ai server MCP
      - costruzione del registry dei tool
      - creazione dell'agente LLM

    Uso come context manager (raccomandato)::

        async with MCPBridge.from_config("mcp_servers.json") as epistylion:
            epistylion.print_tools()
            result = await epistylion.agent.run("Cosa puoi fare?")
            print(result.final_message)

    Uso manuale::

        epistylion = MCPBridge.from_config("mcp_servers.json")
        await epistylion.connect()
        try:
            ...
        finally:
            await epistylion.disconnect()
    """

    def __init__(self, config: BridgeConfig) -> None:
        self._config = config
        self._client = MCPClient()
        self._registry = ToolRegistry()
        self._agent: MCPAgent | None = None
        self._connected = False

    # ── factory methods ───────────────────────────────────────────────────────

    @classmethod
    def from_config(
        cls,
        mcp_config_path: str | Path | None = None,
        env_path: str | Path | None = None,
    ) -> "MCPBridge":
        """Crea un MCPBridge caricando la config da file e .env."""
        config = load_config(mcp_config_path, env_path)
        return cls(config)

    @classmethod
    def from_llm_config(
        cls,
        llm: LLMConfig,
        mcp_config_path: str | Path | None = None,
        env_path: str | Path | None = None,
    ) -> "MCPBridge":
        """Crea un MCPBridge con una LLMConfig personalizzata."""
        config = load_config(mcp_config_path, env_path)
        config.llm = llm
        return cls(config)

    # ── ciclo di vita ──────────────────────────────────────────────────────────

    async def connect(
        self,
        system_prompt: str = "Sei un assistente utile con accesso a vari tool MCP.",
        max_steps: int = 20,
        use_qualified_names: bool = False,
        on_step=None,
    ) -> dict[str, Exception]:
        """
        Connette tutti i server MCP e inizializza l'agente.

        Returns
        -------
        Dizionario dei server che hanno fallito la connessione.
        """
        errors = await self._client.connect_all(
            self._config.servers,
            timeout=self._config.init_timeout,
        )

        if errors:
            console.print(
                f"[yellow]⚠ {len(errors)} server non connessi:[/yellow] "
                + ", ".join(errors.keys())
            )

        # Registra i tool di tutti i server connessi
        for server_name, conn in self._client.get_connections().items():
            self._registry.register_server_tools(server_name, conn.tools)

        # Crea l'agente
        self._agent = MCPAgent(
            llm_config=self._config.llm,
            mcp_client=self._client,
            registry=self._registry,
            system_prompt=system_prompt,
            max_steps=max_steps,
            use_qualified_names=use_qualified_names,
            on_step=on_step,
        )

        self._connected = True
        connected_count = len(self._client.get_connections())
        tool_count = len(self._registry.all_entries())
        console.print(
            f"[green]✓ Connesso a {connected_count} server MCP "
            f"({tool_count} tool totali)[/green]"
        )

        return errors

    async def disconnect(self) -> None:
        """Disconnette tutti i server MCP."""
        await self._client.disconnect_all()
        self._connected = False

    # ── context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> "MCPBridge":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.disconnect()

    # ── proprietà ─────────────────────────────────────────────────────────────

    @property
    def agent(self) -> MCPAgent:
        if self._agent is None:
            raise RuntimeError("MCPBridge non connesso. Chiama connect() o usa il context manager.")
        return self._agent

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def client(self) -> MCPClient:
        return self._client

    @property
    def config(self) -> BridgeConfig:
        return self._config

    # ── utility ───────────────────────────────────────────────────────────────

    def print_tools(self) -> None:
        """Stampa una tabella ricca con tutti i tool disponibili."""
        summary = self._registry.summary()
        if not summary:
            console.print("[yellow]Nessun tool disponibile.[/yellow]")
            return

        for server_name, tool_names in summary.items():
            table = Table(
                title=f"🔧 Server: [bold cyan]{server_name}[/bold cyan]",
                show_lines=True,
            )
            table.add_column("Tool", style="green")
            table.add_column("Descrizione", style="white")

            for entry in self._registry.all_entries():
                if entry.server_name != server_name:
                    continue
                desc = entry.tool.description or "—"
                table.add_row(entry.tool.name, desc[:120])

            console.print(table)

    def get_openai_tools(self, use_qualified_names: bool = False) -> list[dict[str, Any]]:
        """Restituisce i tool nel formato OpenAI function-calling."""
        return self._registry.to_openai_tools(use_qualified_names)

    def are_servers_ready(self) -> bool:
        """Check if at least one MCP server is connected and its tools are registered."""
        connections = self._client.get_connections()
        if not connections:
            return False
        # Ensure registry has entries for any connected server
        return len(self._registry.all_entries()) > 0

    def servers_are_up(self) -> bool:
        """Verifica se almeno un server MCP è connesso e pronto per le chiamate tool."""
        connections = self._client.get_connections()
        if not connections:
            return False
        # Se ci sono connessioni, controlliamo che non ci siano errori recenti (opzionale)
        return True

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Chiama direttamente un tool MCP senza passare dall'LLM."""
        from registry import mcp_result_to_string
        raw = await self._client.call_tool(tool_name, arguments)
        return mcp_result_to_string(raw)
