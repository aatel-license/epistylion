"""
epistylion.cli
~~~~~~~~~~~~~~
Interfaccia a riga di comando per esplorare i tool MCP e
avviare sessioni di chat interattive.

Uso::

    python -m epistylion                           # chat interattiva
    python -m epistylion --list-tools              # lista tool
    python -m epistylion --config my_servers.json  # config personalizzata
    python -m epistylion --run "query singola"     # modalità one-shot
    python -m epistylion --serve-mcp               # server MCP HTTP/SSE (porta 9000)
    python -m epistylion --serve-openai            # server OpenAI-compat (porta 8081)
    python -m epistylion --serve-mcp --serve-openai  # entrambi in parallelo
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

from .config import load_config
from agent import AgentMessage          # import assoluto (non relativo)
from epistylion import MCPBridge

logging.basicConfig(
    level=logging.WARNING,
    format="%(message)s",
    handlers=[RichHandler(rich_tracebacks=True)],
)

console = Console()


async def cmd_list_tools(args: argparse.Namespace) -> None:
    """Lista tutti i tool disponibili senza avviare l'LLM."""
    async with MCPBridge.from_config(args.config, args.env) as bridge:
        bridge.print_tools()

        if args.json:
            tools = bridge.get_openai_tools(use_qualified_names=args.qualified)
            console.print_json(json.dumps(tools, ensure_ascii=False, indent=2))


async def cmd_run(args: argparse.Namespace) -> None:
    """Esegue una singola query e stampa il risultato."""
    async with MCPBridge.from_config(args.config, args.env) as bridge:
        if args.list_tools:
            bridge.print_tools()

        def on_step(step: int, tool: str, result: str) -> None:
            console.print(
                f"  [dim]Step {step} → [cyan]{tool}[/cyan]: "
                f"{result[:100]}{'...' if len(result) > 100 else ''}[/dim]"
            )

        # Il bridge è già connesso via __aenter__: basta agganciare la callback
        bridge.agent._on_step = on_step  # type: ignore

        console.print(Panel(f"[bold]Query:[/bold] {args.run}", border_style="blue"))
        response = await bridge.agent.run(args.run)

        console.print(Panel(
            Markdown(response.final_message),
            title=f"✓ Risposta ({response.steps} step, {len(response.tool_calls_made)} tool call)",
            border_style="green",
        ))


async def cmd_chat(args: argparse.Namespace) -> None:
    """Loop di chat interattiva."""
    async with MCPBridge.from_config(args.config, args.env) as bridge:
        bridge.print_tools()

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

        bridge.agent._on_step = on_step  # type: ignore

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
                bridge.print_tools()
                continue

            if user_input.strip() == "/clear":
                history.clear()
                console.print("[yellow]Conversazione resettata.[/yellow]")
                continue

            if not user_input.strip():
                continue

            with console.status("[dim]Elaborazione...[/dim]"):
                response = await bridge.agent.run(user_input, history)

            # Aggiorna history
            history.append(AgentMessage(role="user", content=user_input))
            history.append(AgentMessage(role="assistant", content=response.final_message))

            console.print(
                Panel(
                    Markdown(response.final_message),
                    title="[bold green]Assistente[/bold green]",
                    border_style="green",
                )
            )


async def cmd_serve_mcp(args: argparse.Namespace) -> None:
    """Avvia il server MCP HTTP/SSE proxy."""
    from .server_mcp import MCPProxyServer
    server = MCPProxyServer.from_config(
        args.config, args.env,
        use_qualified_names=not args.no_qualified,
    )
    console.print(
        Panel(
            f"[bold green]MCP Proxy Server[/bold green]\n"
            f"In ascolto su [cyan]http://{args.host}:{args.mcp_port}/sse[/cyan]\n"
            f"Health: [cyan]http://{args.host}:{args.mcp_port}/health[/cyan]\n\n"
            f"Aggiungi al Claude Desktop config:\n"
            f'[dim]{{"mcpServers": {{"bridge": {{"url": '
            f'"http://localhost:{args.mcp_port}/sse"}}}}}}[/dim]',
            border_style="green",
        )
    )
    await server.run(host=args.host, port=args.mcp_port)


async def cmd_serve_openai(args: argparse.Namespace) -> None:
    """Avvia il server OpenAI-compatible con tool MCP integrati."""
    from .server_openai import OpenAIProxyServer
    server = OpenAIProxyServer.from_config(
        args.config, args.env,
        expose_tool_calls=args.expose_tool_calls,
    )
    console.print(
        Panel(
            f"[bold green]OpenAI-compatible Server[/bold green]\n"
            f"Endpoint: [cyan]http://{args.host}:{args.openai_port}/v1/chat/completions[/cyan]\n"
            f"Modelli:  [cyan]http://{args.host}:{args.openai_port}/v1/models[/cyan]\n"
            f"Health:   [cyan]http://{args.host}:{args.openai_port}/health[/cyan]\n\n"
            f"Usa con qualsiasi client OpenAI:\n"
            f'[dim]openai.base_url = "http://localhost:{args.openai_port}/v1"[/dim]',
            border_style="green",
        )
    )
    await server.run(host=args.host, port=args.openai_port)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="epistylion – Client MCP per LLM OpenAI-compatible",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  python -m epistylion                             # chat interattiva
  python -m epistylion --list-tools                # lista tool
  python -m epistylion --run "crea un cubo"        # query singola
  python -m epistylion --serve-mcp                 # server MCP HTTP/SSE (porta 9000)
  python -m epistylion --serve-openai              # server OpenAI-compat (porta 8081)
  python -m epistylion --serve-mcp --serve-openai  # entrambi in parallelo
        """,
    )

    # ── opzioni comuni ────────────────────────────────────────────────────────
    parser.add_argument("--config", "-c", default=None, metavar="PATH",
                        help="Percorso mcp_servers.json")
    parser.add_argument("--env", "-e", default=None, metavar="PATH",
                        help="Percorso .env")
    parser.add_argument("--host", default="0.0.0.0", metavar="HOST",
                        help="Indirizzo di bind per i server (default: 0.0.0.0)")

    # ── modalità client ───────────────────────────────────────────────────────
    parser.add_argument("--list-tools", "-l", action="store_true",
                        help="Lista i tool disponibili ed esci")
    parser.add_argument("--json", action="store_true",
                        help="Output tool in formato JSON OpenAI (con --list-tools)")
    parser.add_argument("--qualified", action="store_true",
                        help="Usa nomi qualificati server__tool (con --list-tools --json)")
    parser.add_argument("--run", "-r", default=None, metavar="QUERY",
                        help="Esegui una singola query e stampa il risultato")

    # ── modalità server MCP ───────────────────────────────────────────────────
    parser.add_argument("--serve-mcp", action="store_true",
                        help="Avvia server MCP HTTP/SSE proxy")
    parser.add_argument("--mcp-port", type=int, default=9000, metavar="PORT",
                        help="Porta server MCP (default: 9000)")
    parser.add_argument("--no-qualified", action="store_true",
                        help="Usa nomi originali dei tool (senza prefisso server__)")

    # ── modalità server OpenAI ────────────────────────────────────────────────
    parser.add_argument("--serve-openai", action="store_true",
                        help="Avvia server OpenAI-compatible con tool MCP integrati")
    parser.add_argument("--openai-port", type=int, default=8081, metavar="PORT",
                        help="Porta server OpenAI (default: 8081)")
    parser.add_argument("--expose-tool-calls", action="store_true",
                        help="Includi dettaglio tool call nella risposta (_mcp_tool_calls)")

    args = parser.parse_args()

    # Modalità server (una o entrambe in parallelo)
    if args.serve_mcp or args.serve_openai:
        async def run_servers():
            tasks = []
            if args.serve_mcp:
                tasks.append(cmd_serve_mcp(args))
            if args.serve_openai:
                tasks.append(cmd_serve_openai(args))
            await asyncio.gather(*tasks)
        asyncio.run(run_servers())

    # Modalità client
    elif args.list_tools:
        asyncio.run(cmd_list_tools(args))
    elif args.run:
        asyncio.run(cmd_run(args))
    else:
        asyncio.run(cmd_chat(args))


if __name__ == "__main__":
    main()