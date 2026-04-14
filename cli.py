"""
epistylion.cli
~~~~~~~~~~~~~~
Interfaccia a riga di comando per esplorare i tool MCP e
avviare sessioni di chat interattive.

Uso::

    python cli.py                           # chat interattiva
    python cli.py --list-tools              # lista tool
    python cli.py --config my_servers.json  # config personalizzata
    python cli.py --run "query singola"     # modalità one-shot
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

from agent import AgentMessage  # import assoluto (non relativo)
from epistylion import MCPBridge

console = Console()


def setup_logging(verbose: bool = False) -> None:
    """Configura il logging con RichHandler per output leggibile."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, markup=True)],
    )
    # Silenzia librerie troppo verbose
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.INFO)


async def cmd_list_tools(args: argparse.Namespace) -> None:
    """Lista tutti i tool disponibili senza avviare l'LLM."""
    async with MCPBridge.from_config(args.config, args.env) as epistylion:
        epistylion.print_tools()

        if args.json:
            tools = epistylion.get_openai_tools(use_qualified_names=args.qualified)
            console.print_json(json.dumps(tools, ensure_ascii=False, indent=2))


async def cmd_run(args: argparse.Namespace) -> None:
    """Esegue una singola query e stampa il risultato."""
    async with MCPBridge.from_config(args.config, args.env) as epistylion:
        if args.list_tools:
            epistylion.print_tools()

        def on_step(step: int, tool: str, result: str) -> None:
            console.print(
                f"  [dim]Step {step} → [cyan]{tool}[/cyan]: "
                f"{result[:100]}{'...' if len(result) > 100 else ''}[/dim]"
            )

        epistylion.agent._on_step = on_step  # type: ignore

        console.print(Panel(f"[bold]Query:[/bold] {args.run}", border_style="blue"))
        response = await epistylion.agent.run(args.run)

        console.print(Panel(
            Markdown(response.final_message),
            title=f"✓ Risposta ({response.steps} step, {len(response.tool_calls_made)} tool call)",
            border_style="green",
        ))


async def cmd_chat(args: argparse.Namespace) -> None:
    """Loop di chat interattiva."""
    async with MCPBridge.from_config(args.config, args.env) as epistylion:
        epistylion.print_tools()

        history: list[AgentMessage] = []

        console.print(
            Panel(
                "[bold green]Chat MCP avviata[/bold green]\n"
                "Digita [bold]exit[/bold] o [bold]quit[/bold] per uscire.\n"
                "Digita [bold]/tools[/bold] per vedere i tool disponibili.\n"
                "Digita [bold]/clear[/bold] per resettare la conversazione.",
                border_style="green",
            )
        )

        def on_step(step: int, tool: str, result: str) -> None:
            console.print(
                f"  [dim]🔧 {tool}: {result[:120]}{'...' if len(result) > 120 else ''}[/dim]"
            )

        epistylion.agent._on_step = on_step  # type: ignore

        while True:
            try:
                user_input = Prompt.ask("\n[bold blue]Tu[/bold blue]")
            except (EOFError, KeyboardInterrupt):
                console.print("\n[yellow]Arrivederci![/yellow]")
                break

            if user_input.strip().lower() in ("exit", "quit", "esci"):
                console.print("[yellow]Arrivederci![/yellow]")
                break

            if user_input.strip() == "/tools":
                epistylion.print_tools()
                continue

            if user_input.strip() == "/clear":
                history.clear()
                console.print("[yellow]Conversazione resettata.[/yellow]")
                continue

            if not user_input.strip():
                continue

            with console.status("[dim]Elaborazione...[/dim]"):
                response = await epistylion.agent.run(user_input, history)

            history.append(AgentMessage(role="user", content=user_input))
            history.append(AgentMessage(role="assistant", content=response.final_message))

            console.print(
                Panel(
                    Markdown(response.final_message),
                    title="[bold green]Assistente[/bold green]",
                    border_style="green",
                )
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Epistylion — Client MCP per LLM OpenAI-compatible",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        metavar="PATH",
        help="Percorso al file mcp_servers.json (default: $MCP_CONFIG_PATH o ./mcp_servers.json)",
    )
    parser.add_argument(
        "--env", "-e",
        default=None,
        metavar="PATH",
        help="Percorso al file .env (default: ./.env)",
    )
    parser.add_argument(
        "--list-tools", "-l",
        action="store_true",
        help="Lista i tool disponibili ed esci",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output dei tool in formato JSON OpenAI (con --list-tools)",
    )
    parser.add_argument(
        "--qualified",
        action="store_true",
        help="Usa nomi qualificati 'server__tool' (con --list-tools --json)",
    )
    parser.add_argument(
        "--run", "-r",
        default=None,
        metavar="QUERY",
        help="Esegui una singola query e stampa il risultato",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Abilita logging DEBUG (mostra errori di connessione dettagliati)",
    )

    args = parser.parse_args()
    setup_logging(verbose=args.verbose)

    if args.list_tools:
        asyncio.run(cmd_list_tools(args))
    elif args.run:
        asyncio.run(cmd_run(args))
    else:
        asyncio.run(cmd_chat(args))


if __name__ == "__main__":
    main()