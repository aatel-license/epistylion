"""
epistylion
~~~~~~~~~

Facade di alto livello: combina config, client, registry, agent e skill
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
from skills import SkillRegistry

logger  = logging.getLogger(__name__)
console = Console()


class MCPBridge:
    """
    Facade principale di Epistylion.

    Gestisce l'intero ciclo di vita:
    - caricamento config
    - connessione ai server MCP
    - costruzione del registry dei tool
    - caricamento delle skill (opzionale)
    - creazione dell'agente LLM

    Uso base (senza skill)::

        async with MCPBridge.from_config("mcp_servers.json") as bridge:
            bridge.print_tools()
            result = await bridge.agent.run("Cosa puoi fare?")
            print(result.final_message)

    Uso con skill fissa per tutta la sessione::

        async with MCPBridge.from_config("mcp_servers.json") as bridge:
            await bridge.connect(skill="code")
            result = await bridge.agent.run("Scrivi un parser JSON")

    Uso con skill diversa per ogni chiamata::

        async with MCPBridge.from_config("mcp_servers.json") as bridge:
            await bridge.connect()   # nessuna skill di default
            r1 = await bridge.agent.run("Scrivi un parser", skill="code")
            r2 = await bridge.agent.run("Traduci in francese", skill="translate")
            r3 = await bridge.agent.run("Cosa sai fare?")   # senza skill
    """

    def __init__(self, config: BridgeConfig) -> None:
        self._config   = config
        self._client   = MCPClient()
        self._registry = ToolRegistry()
        self._agent:          MCPAgent      | None = None
        self._skill_registry: SkillRegistry | None = None
        self._connected = False

    # ── factory methods ───────────────────────────────────────────────────────

    @classmethod
    def from_config(
        cls,
        mcp_config_path: str | Path | None = None,
        env_path:        str | Path | None = None,
    ) -> "MCPBridge":
        """Crea un MCPBridge caricando la config da file e .env."""
        config = load_config(mcp_config_path, env_path)
        return cls(config)

    @classmethod
    def from_llm_config(
        cls,
        llm:             LLMConfig,
        mcp_config_path: str | Path | None = None,
        env_path:        str | Path | None = None,
    ) -> "MCPBridge":
        """Crea un MCPBridge con una LLMConfig personalizzata."""
        config     = load_config(mcp_config_path, env_path)
        config.llm = llm
        return cls(config)

    # ── ciclo di vita ─────────────────────────────────────────────────────────

    async def connect(
        self,
        system_prompt:       str            = "Sei un assistente utile con accesso a vari tool MCP.",
        max_steps:           int            = 20,
        use_qualified_names: bool           = False,
        on_step              = None,
        skill:               str | None     = None,
        skills_dir:          str | Path | None = None,
    ) -> dict[str, Exception]:
        """
        Connette tutti i server MCP e inizializza l'agente.

        Parameters
        ----------
        system_prompt : str
            System prompt base dell'agente (senza skill).
        max_steps : int
            Limite massimo di iterazioni tool-call.
        use_qualified_names : bool
            Se True, i tool usano il formato 'server__tool'.
        on_step : Callable, optional
            Callback (step_num, tool_name, result) dopo ogni step.
        skill : str | None
            Nome (es. "code") o percorso a un SKILL.md da iniettare di
            default in ogni run(). Può essere sovrascritta per singola
            chiamata passando ``skill=`` direttamente a agent.run().
            Passa None per non usare nessuna skill di default.
        skills_dir : str | Path | None
            Cartella delle skill. Default: variabile d'ambiente
            EPISTYLION_SKILLS_DIR, oppure ./skills.

        Returns
        -------
        dict[str, Exception]
            Server che hanno fallito la connessione.
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

        # ── inizializza skill registry ────────────────────────────────────────
        self._skill_registry = SkillRegistry(skills_dir or "skills")
        available_skills     = self._skill_registry.list()

        if available_skills:
            console.print(
                f"[cyan]✦ Skill disponibili:[/cyan] {', '.join(available_skills)}"
            )
        else:
            console.print("[dim]  (nessuna skill trovata in 'skills/')[/dim]")

        if skill:
            if skill in available_skills or Path(skill).exists():
                console.print(f"[cyan]✦ Skill attiva (default):[/cyan] {skill}")
            else:
                console.print(f"[yellow]⚠ Skill '{skill}' non trovata — ignorata.[/yellow]")
                skill = None

        # ── crea l'agente con il registry delle skill ─────────────────────────
        self._agent = MCPAgent(
            llm_config=self._config.llm,
            mcp_client=self._client,
            registry=self._registry,
            system_prompt=system_prompt,
            max_steps=max_steps,
            use_qualified_names=use_qualified_names,
            on_step=on_step,
            skill_registry=self._skill_registry,
            default_skill=skill,
        )

        self._connected = True

        connected_count = len(self._client.get_connections())
        tool_count      = len(self._registry.all_entries())
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
    def skill_registry(self) -> SkillRegistry | None:
        return self._skill_registry

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

        for server_name in summary:
            table = Table(
                title=f"🔧 Server: [bold cyan]{server_name}[/bold cyan]",
                show_lines=True,
            )
            table.add_column("Tool",        style="green")
            table.add_column("Descrizione", style="white")

            for entry in self._registry.all_entries():
                if entry.server_name != server_name:
                    continue
                desc = entry.tool.description or "—"
                table.add_row(entry.tool.name, desc[:120])

            console.print(table)

    def print_skills(self) -> None:
        """Stampa una tabella con tutte le skill disponibili."""
        if self._skill_registry is None:
            console.print("[yellow]Skill registry non inizializzato.[/yellow]")
            return

        skills = self._skill_registry.list()
        if not skills:
            console.print("[yellow]Nessuna skill trovata.[/yellow]")
            return

        table = Table(title="✦ Skill disponibili", show_lines=True)
        table.add_column("Nome",        style="cyan")
        table.add_column("Descrizione", style="white")
        table.add_column("File",        style="dim")

        for name in skills:
            skill = self._skill_registry.get(name)
            if skill:
                active = " [green](default)[/green]" if name == self._agent.default_skill else ""
                table.add_row(name + active, skill.description, str(skill.path))

        console.print(table)

    def get_openai_tools(self, use_qualified_names: bool = False) -> list[dict[str, Any]]:
        """Restituisce i tool nel formato OpenAI function-calling."""
        return self._registry.to_openai_tools(use_qualified_names)

    def are_servers_ready(self) -> bool:
        connections = self._client.get_connections()
        if not connections:
            return False
        return len(self._registry.all_entries()) > 0

    def servers_are_up(self) -> bool:
        return bool(self._client.get_connections())

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Chiama direttamente un tool MCP senza passare dall'LLM."""
        from registry import mcp_result_to_string
        raw = await self._client.call_tool(tool_name, arguments)
        return mcp_result_to_string(raw)
