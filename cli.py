"""
epistylion.cli
~~~~~~~~~~~~~~

Interfaccia a riga di comando per esplorare i tool MCP e
avviare sessioni di chat interattive.

Uso::

    python -m epistylion                        # chat interattiva
    python -m epistylion --list-tools           # lista tool
    python -m epistylion --list-skills          # lista skill disponibili
    python -m epistylion --skill code           # chat con skill "code" attiva
    python -m epistylion --run "query" --skill summarize
    python -m epistylion --config my_servers.json
    python -m epistylion --serve-mcp            # server MCP HTTP/SSE (porta 9000)
    python -m epistylion --serve-openai         # server OpenAI-compat (porta 8081)
    python -m epistylion --serve-mcp --serve-openai
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from rich.console import Console
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from config import load_config
from agent import AgentMessage
from epistylion import MCPBridge
from skills import SkillRegistry

logging.basicConfig(
    level=logging.WARNING,
    format="%(message)s",
    handlers=[RichHandler(rich_tracebacks=True)],
)

console = Console()


# ── helpers ────────────────────────────────────────────────────────────────────

def _print_skills(registry: SkillRegistry, active: str | None = None) -> None:
    """Stampa una tabella con tutte le skill disponibili."""
    skills = registry.list()
    if not skills:
        console.print(f"[yellow]Nessuna skill trovata in '{registry.skills_dir}'.[/yellow]")
        return

    table = Table(title="✦ Skill disponibili", show_lines=True)
    table.add_column("Nome",        style="cyan",  no_wrap=True)
    table.add_column("Descrizione", style="white")
    table.add_column("File",        style="dim")

    for name in skills:
        skill = registry.get(name)
        if not skill:
            continue
        label = name + (" [green](attiva)[/green]" if name == active else "")
        table.add_row(label, skill.description, str(skill.path))

    console.print(table)


def _on_step_callback(step: int, tool: str, result: str) -> None:
    console.print(
        f"  [dim]🔧 Step {step} → [cyan]{tool}[/cyan]: "
        f"{result[:120]}{'...' if len(result) > 120 else ''}[/dim]"
    )


# ── comandi ────────────────────────────────────────────────────────────────────

async def cmd_list_tools(args: argparse.Namespace) -> None:
    """Lista tutti i tool disponibili senza avviare l'LLM."""
    async with MCPBridge.from_config(args.config, args.env) as bridge:
        await bridge.connect(skills_dir=args.skills_dir)
        bridge.print_tools()
        if args.json:
            tools = bridge.get_openai_tools(use_qualified_names=args.qualified)
            console.print_json(json.dumps(tools, ensure_ascii=False, indent=2))


async def cmd_list_skills(args: argparse.Namespace) -> None:
    """Lista le skill disponibili senza connettersi ai server MCP."""
    registry = SkillRegistry(args.skills_dir or "skills")
    _print_skills(registry, active=args.skill)


async def cmd_run(args: argparse.Namespace) -> None:
    """Esegue una singola query e stampa il risultato."""
    async with MCPBridge.from_config(args.config, args.env) as bridge:
        await bridge.connect(
            skill=args.skill or None,
            skills_dir=args.skills_dir,
        )

        if args.list_tools:
            bridge.print_tools()

        if args.skill:
            console.print(f"[cyan]✦ Skill attiva:[/cyan] {args.skill}")

        bridge.agent._on_step = _on_step_callback  # type: ignore

        console.print(Panel(f"[bold]Query:[/bold] {args.run}", border_style="blue"))

        response = await bridge.agent.run(args.run)

        skill_label = f" | skill: {response.skill_used}" if response.skill_used else ""
        console.print(Panel(
            Markdown(response.final_message),
            title=(
                f"✓ Risposta "
                f"({response.steps} step, "
                f"{len(response.tool_calls_made)} tool call"
                f"{skill_label})"
            ),
            border_style="green",
        ))


async def cmd_chat(args: argparse.Namespace) -> None:
    """Loop di chat interattiva con supporto skill."""
    async with MCPBridge.from_config(args.config, args.env) as bridge:
        await bridge.connect(
            skill=args.skill or None,
            skills_dir=args.skills_dir,
        )

        bridge.print_tools()

        active_skill = args.skill or None
        history: list[AgentMessage] = []

        # ── pannello di benvenuto ──────────────────────────────────────────────
        skill_line = (
            f"Skill attiva: [cyan]{active_skill}[/cyan]\n"
            if active_skill else
            "Nessuna skill attiva (digita [bold]/skill <nome>[/bold] per attivarla)\n"
        )
        console.print(Panel(
            "[bold green]Chat Epistylion avviata[/bold green]\n"
            + skill_line +
            "\nComandi:\n"
            "  [bold]/tools[/bold]          → lista tool MCP\n"
            "  [bold]/skills[/bold]         → lista skill disponibili\n"
            "  [bold]/skill <nome>[/bold]   → attiva una skill per i prossimi messaggi\n"
            "  [bold]/skill off[/bold]      → disattiva la skill corrente\n"
            "  [bold]/clear[/bold]          → resetta la conversazione\n"
            "  [bold]exit[/bold]            → esci",
            border_style="green",
        ))

        bridge.agent._on_step = _on_step_callback  # type: ignore

        while True:
            try:
                user_input = Prompt.ask("\n[bold blue]Tu[/bold blue]")
            except (EOFError, KeyboardInterrupt):
                console.print("\n[yellow]Arrivederci![/yellow]")
                break

            stripped = user_input.strip()

            # ── comandi speciali ───────────────────────────────────────────────
            if stripped.lower() in ("exit", "quit", "esci"):
                console.print("[yellow]Arrivederci![/yellow]")
                break

            if stripped == "/tools":
                bridge.print_tools()
                continue

            if stripped == "/skills":
                _print_skills(bridge.skill_registry, active=active_skill)
                continue

            if stripped.startswith("/skill "):
                arg = stripped[7:].strip()
                if arg == "off":
                    active_skill = None
                    bridge.agent.default_skill = None
                    console.print("[yellow]Skill disattivata.[/yellow]")
                else:
                    # verifica che esista
                    if bridge.skill_registry and bridge.skill_registry.get(arg):
                        active_skill = arg
                        bridge.agent.default_skill = arg
                        console.print(f"[cyan]✦ Skill attivata:[/cyan] {arg}")
                    else:
                        available = ", ".join(bridge.skill_registry.list()) if bridge.skill_registry else "—"
                        console.print(
                            f"[red]Skill '{arg}' non trovata.[/red] "
                            f"Disponibili: {available}"
                        )
                continue

            if stripped == "/clear":
                history.clear()
                console.print("[yellow]Conversazione resettata.[/yellow]")
                continue

            if not stripped:
                continue

            # ── chiamata all'agente ────────────────────────────────────────────
            skill_indicator = f" [dim](skill: {active_skill})[/dim]" if active_skill else ""
            with console.status(f"[dim]Elaborazione...{skill_indicator}[/dim]"):
                response = await bridge.agent.run(user_input, history)

            history.append(AgentMessage(role="user",      content=user_input))
            history.append(AgentMessage(role="assistant", content=response.final_message))

            title = "[bold green]Assistente[/bold green]"
            if response.skill_used:
                title += f" [dim](skill: {response.skill_used})[/dim]"

            console.print(Panel(
                Markdown(response.final_message),
                title=title,
                border_style="green",
            ))


async def cmd_serve_mcp(args: argparse.Namespace) -> None:
    from server_mcp import MCPProxyServer
    server = MCPProxyServer.from_config(
        args.config, args.env,
        use_qualified_names=not args.no_qualified,
    )
    console.print(Panel(
        f"[bold green]MCP Proxy Server[/bold green]\n"
        f"In ascolto su [cyan]http://{args.host}:{args.mcp_port}/sse[/cyan]\n"
        f"Health: [cyan]http://{args.host}:{args.mcp_port}/health[/cyan]",
        border_style="green",
    ))
    await server.run(host=args.host, port=args.mcp_port)


async def cmd_serve_openai(args: argparse.Namespace) -> None:
    from server_openai import OpenAIProxyServer
    server = OpenAIProxyServer.from_config(
        args.config, args.env,
        expose_tool_calls=args.expose_tool_calls,
    )
    console.print(Panel(
        f"[bold green]OpenAI-compatible Server[/bold green]\n"
        f"Endpoint: [cyan]http://{args.host}:{args.openai_port}/v1/chat/completions[/cyan]\n"
        f"Modelli:  [cyan]http://{args.host}:{args.openai_port}/v1/models[/cyan]",
        border_style="green",
    ))
    await server.run(host=args.host, port=args.openai_port)


# ── argparse ───────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="epistylion – Client MCP per LLM OpenAI-compatible",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  python -m epistylion                          # chat interattiva
  python -m epistylion --list-skills            # lista skill disponibili
  python -m epistylion --skill code             # chat con skill "code"
  python -m epistylion --run "crea un cubo"     # query singola
  python -m epistylion --run "..." --skill summarize
  python -m epistylion --serve-mcp              # server MCP proxy (porta 9000)
  python -m epistylion --serve-openai           # server OpenAI-compat (porta 8081)
""",
    )

    # ── opzioni comuni ────────────────────────────────────────────────────────
    parser.add_argument("--config",     "-c", default=None, metavar="PATH",
                        help="Percorso mcp_servers.json")
    parser.add_argument("--env",        "-e", default=None, metavar="PATH",
                        help="Percorso .env")
    parser.add_argument("--host",             default="0.0.0.0", metavar="HOST",
                        help="Indirizzo di bind per i server (default: 0.0.0.0)")

    # ── skill ─────────────────────────────────────────────────────────────────
    parser.add_argument("--skill",      "-s", default=None, metavar="NOME",
                        help="Nome della skill da iniettare (es. code, translate, summarize)")
    parser.add_argument("--skills-dir",       default="skills", metavar="DIR",
                        help="Cartella delle skill (default: ./skills)")
    parser.add_argument("--list-skills",      action="store_true",
                        help="Lista le skill disponibili ed esci")

    # ── modalità client ───────────────────────────────────────────────────────
    parser.add_argument("--list-tools", "-l", action="store_true",
                        help="Lista i tool disponibili")
    parser.add_argument("--json",             action="store_true",
                        help="Output tool in formato JSON OpenAI (con --list-tools)")
    parser.add_argument("--qualified",        action="store_true",
                        help="Usa nomi qualificati server__tool")
    parser.add_argument("--run",        "-r", default=None, metavar="QUERY",
                        help="Esegui una singola query e stampa il risultato")

    # ── server MCP ────────────────────────────────────────────────────────────
    parser.add_argument("--serve-mcp",        action="store_true",
                        help="Avvia server MCP HTTP/SSE proxy")
    parser.add_argument("--mcp-port",         type=int, default=9000, metavar="PORT",
                        help="Porta server MCP (default: 9000)")
    parser.add_argument("--no-qualified",     action="store_true",
                        help="Usa nomi originali dei tool (senza prefisso server__)")

    # ── server OpenAI ─────────────────────────────────────────────────────────
    parser.add_argument("--serve-openai",     action="store_true",
                        help="Avvia server OpenAI-compatible con tool MCP integrati")
    parser.add_argument("--openai-port",      type=int, default=8081, metavar="PORT",
                        help="Porta server OpenAI (default: 8081)")
    parser.add_argument("--expose-tool-calls", action="store_true",
                        help="Includi dettaglio tool call nella risposta")

    args = parser.parse_args()

    # ── dispatch ──────────────────────────────────────────────────────────────
    if args.serve_mcp or args.serve_openai:
        async def run_servers():
            tasks = []
            if args.serve_mcp:    tasks.append(cmd_serve_mcp(args))
            if args.serve_openai: tasks.append(cmd_serve_openai(args))
            await asyncio.gather(*tasks)
        asyncio.run(run_servers())

    elif args.list_skills:
        asyncio.run(cmd_list_skills(args))

    elif args.list_tools:
        asyncio.run(cmd_list_tools(args))

    elif args.run:
        asyncio.run(cmd_run(args))

    else:
        asyncio.run(cmd_chat(args))


if __name__ == "__main__":
    main()