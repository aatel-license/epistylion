"""
epistylion
~~~~~~~~~~~~~~~~~

Facade di alto livello: combina config, client, registry e agent
in un unico context manager facile da usare.

FIX v2:
- connect() usa DEFAULT_SYSTEM_PROMPT da agent.py (robusto per modelli locali)
- system_prompt è ora facilmente sovrascrivibile da chi costruisce la webapp
- Aggiunto helper history_to_agent_messages() per gestire correttamente
  la history in contesti webapp (ogni chiamata agent.run deve passare la history!)
- Docstring aggiornate con note sul bug "tool chiamato una sola volta"
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from agent import MCPAgent, AgentMessage, DEFAULT_SYSTEM_PROMPT
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

        async with MCPBridge.from_config("mcp_servers.json") as bridge:
            bridge.print_tools()
            result = await bridge.agent.run("Crea un cubo in Blender")
            print(result.final_message)

    Uso in webapp (multi-turn con history)::

        bridge = await MCPBridge.from_config("mcp_servers.json").connect()
        conversation_history: list[AgentMessage] = []

        # Per ogni messaggio utente:
        result = await bridge.agent.run(
            user_message=user_input,
            history=conversation_history,   # <-- FONDAMENTALE in webapp!
        )
        # Aggiorna la history dopo ogni risposta
        conversation_history = bridge.update_history(
            conversation_history, user_input, result
        )

    NOTA sul bug "tool chiamato una sola volta":
    Se il modello usa un tool e poi risponde senza continuare il loop,
    il problema è quasi sempre uno di questi:
    1. System prompt non esplicito → usa DEFAULT_SYSTEM_PROMPT (default)
    2. History non passata in webapp → ogni run() ricomincia da zero
    3. Modello troppo piccolo (7B) → prova 14B+ per task multi-step
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
        # FIX: default cambiato a DEFAULT_SYSTEM_PROMPT invece della stringa
        # generica originale. Forza il modello a usare i tool per tutti i passi.
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_steps: int = 20,
        use_qualified_names: bool = False,
        on_step=None,
    ) -> dict[str, Exception]:
        """
        Connette tutti i server MCP e inizializza l'agente.

        Parameters
        ----------
        system_prompt : str
            System prompt per l'agente. Di default usa DEFAULT_SYSTEM_PROMPT
            che istruisce il modello a usare i tool per tutti i passi necessari.
            Sovrascrivilo solo se sai cosa stai facendo.
        max_steps : int
            Numero massimo di iterazioni tool-call. Aumenta per task complessi.
        use_qualified_names : bool
            Se True, i nomi dei tool usano il formato 'server__tool'.
        on_step : Callable, optional
            Callback (step_num, tool_name, result) chiamata dopo ogni tool call.

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

        # Crea l'agente con il system prompt robusto
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
            raise RuntimeError(
                "MCPBridge non connesso. Chiama connect() o usa il context manager."
            )
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
        return len(self._registry.all_entries()) > 0

    def servers_are_up(self) -> bool:
        """Verifica se almeno un server MCP è connesso e pronto per le chiamate tool."""
        connections = self._client.get_connections()
        if not connections:
            return False
        return True

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Chiama direttamente un tool MCP senza passare dall'LLM."""
        from registry import mcp_result_to_string
        raw = await self._client.call_tool(tool_name, arguments)
        return mcp_result_to_string(raw)

    # ── FIX: helper per gestione history in webapp ─────────────────────────────

    def update_history(
        self,
        history: list[AgentMessage],
        user_message: str,
        result: Any,  # AgentResponse
    ) -> list[AgentMessage]:
        """
        Aggiorna la history della conversazione dopo una chiamata agent.run().

        Uso tipico in webapp::

            history: list[AgentMessage] = []

            # Per ogni turno:
            result = await bridge.agent.run(user_input, history=history)
            history = bridge.update_history(history, user_input, result)

        Il mancato aggiornamento della history è una delle cause principali
        del bug "tool chiamato una sola volta" nelle webapp: senza history,
        ogni chiamata run() riparte da zero e il modello non ricorda i
        tool call precedenti.

        Parameters
        ----------
        history : list[AgentMessage]
            History corrente della conversazione.
        user_message : str
            Messaggio dell'utente inviato in questo turno.
        result : AgentResponse
            Risposta ottenuta da agent.run().

        Returns
        -------
        list[AgentMessage]
            History aggiornata con il turno appena completato.
        """
        updated = list(history)
        updated.append(AgentMessage(role="user", content=user_message))

        # Aggiungi i tool call intermedi alla history
        for tc_record in result.tool_calls_made:
            updated.append(AgentMessage(
                role="tool",
                content=tc_record.get("result", ""),
                tool_name=tc_record.get("tool"),
            ))

        # Aggiungi la risposta finale
        updated.append(AgentMessage(role="assistant", content=result.final_message))
        return updated