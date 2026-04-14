
"""
Esempi d'uso di epistylion.

Copia questo file nella tua directory di lavoro e personalizzalo.
"""

import asyncio
import json
import httpx

from epistylion import MCPBridge, LLMConfig


async def retry_with_backoff(coro_func, *args, max_attempts=3, base_delay=1.0, **kwargs):
    """Esegue una coroutine con retry in caso di errori di connessione HTTPX."""
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_func(*args, **kwargs)
        except httpx.ConnectError as e:
            if attempt == max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            print(f"Connection error: {e}. Retrying in {delay}s... (attempt {attempt}/{max_attempts})")
            await asyncio.sleep(delay)
        except Exception as e:
            # Altre eccezioni non vengono ritentate
            raise


# ────────────────────────────────────────────────────────────────────────────

async def example_basic():
    """Connetti, lista tool, esegui una query."""

    async with MCPBridge.from_config("mcp_servers.json") as epistylion:
        # Stampa tutti i tool disponibili (tabella rich)
        epistylion.print_tools()

        # Esegui una query
        result = await retry_with_backoff(
            epistylion.agent.run,
            "Crea un cubo di 2 metri in Blender al centro della scena"
        )
        print(result.final_message)
        print(f"\nStep eseguiti: {result.steps}")
        print(f"Tool chiamati: {[tc['tool'] for tc in result.tool_calls_made]}")


# ────────────────────────────────────────────────────────────────────────────
# ESEMPIO 2: LLMConfig personalizzata a runtime
# ────────────────────────────────────────────────────────────────────────────

async def example_custom_llm():
    """Usa Ollama con llama3.2 invece dei default da .env."""

    llm = LLMConfig(
        base_url="http://localhost:1234/v1",
        api_key="",
        model="",
        temperature=0.1,
        max_tokens=8192,
    )

    async with MCPBridge.from_llm_config(llm, "mcp_servers.json") as epistylion:
        result = await retry_with_backoff(
            epistylion.agent.run,
            "Scrapa il titolo della homepage di example.com"
        )
        print(result.final_message)


# ────────────────────────────────────────────────────────────────────────────
# ESEMPIO 3: Conversazione multi-turn con history
# ────────────────────────────────────────────────────────────────────────────

async def example_conversation():
    """Loop conversazionale che mantiene la history."""

    from agent import AgentMessage

    async with MCPBridge.from_config("mcp_servers.json") as epistylion:
        history: list[AgentMessage] = []

        turns = [
            "Quanti oggetti ci sono nella scena Blender attuale?",
            "Aggiungi una sfera rossa accanto al cubo",
            "Qual è il totale degli oggetti ora?",
        ]

        for user_msg in turns:
            print(f"\n[USER] {user_msg}")
            response = await retry_with_backoff(
                epistylion.agent.run,
                user_msg,
                history
            )
            print(f"[ASSISTANT] {response.final_message}")

            # Aggiorna history per il turno successivo
            history.append(AgentMessage(role="user", content=user_msg))
            history.append(AgentMessage(role="assistant", content=response.final_message))


# ────────────────────────────────────────────────────────────────────────────
# ESEMPIO 4: Chiamata diretta a un tool MCP (senza LLM)
# ────────────────────────────────────────────────────────────────────────

async def example_direct_tool_call():
    """Chiama un tool MCP direttamente, senza passare dall'LLM."""

    async with MCPBridge.from_config("mcp_servers.json") as epistylion:
        # Stampa tutti i tool disponibili per verificare il nome corretto
        epistylion.print_tools()
        # Verifica che lo strumento sia registrato nel registry
        tool_entry = epistylion.registry.resolve("remember")
        if not tool_entry:
            print("[yellow]Tool 'remember' non disponibile.[/yellow]")
            return
        # Chiamata diretta usando il nome qualificato (server__tool)
        result = await epistylion.call_tool(
            tool_name="remember",    # nome del tool nel server blender
            arguments={"concept":"chitarra","feeling":"gioia", "content":"Quel giorno in cui riuscii a fare l' assolo di chitarra di confortably numb. "},
        )
        print("Risultato diretto:", result)
        tool_entry = epistylion.registry.resolve("brain_introspect")
        if not tool_entry:
            print("[yellow]Tool 'brain_introspect' non disponibile.[/yellow]")
            return
        # Chiamata diretta usando il nome qualificato (server__tool)
        result = await epistylion.call_tool(
            tool_name="brain_introspect",    # nome del tool nel server blender
            arguments={"concept":"chitarra"},
        )
        print("Risultato diretto:", result)

# ────────────────────────────────────────────────────────────────────────────
# ESEMPIO 5: Esporta tool in formato OpenAI (per integrazione con altri sistemi)
# ────────────────────────────────────────────────────────────────────────

async def example_export_tools():
    """Ottieni la lista tool in formato OpenAI function-calling."""

    async with MCPBridge.from_config("mcp_servers.json") as epistylion:
        tools = epistylion.get_openai_tools()
        print(json.dumps(tools, indent=2, ensure_ascii=False))


# ────────────────────────────────────────────────────────────────────────────
# ESEMPIO 6: Callback di monitoraggio per ogni step
# ────────────────────────────────────────────────────────────────────────

async def example_with_monitoring():
    """Monitora ogni tool call con una callback."""

    def on_step(step: int, tool_name: str, result: str) -> None:
        print(f"  → Step {step}: {tool_name}({result[:80]}...)")

    _epistylion = MCPBridge.from_config("mcp_servers.json")
    await _epistylion.connect(on_step=on_step)

    try:
        result = await retry_with_backoff(
            _epistylion.agent.run,
            "Cerca le ultime notizie su LLM locali"
        )
        print(result.final_message)
    finally:
        await _epistylion.disconnect()


# ────────────────────────────────────────────────────────────────────────────
# ESEMPIO 7: Configurazione programmatica (senza file JSON)
# ────────────────────────────────────────────────────────────────────────

async def example_programmatic_config():
    """Crea la configurazione direttamente nel codice."""

    from epistylion import BridgeConfig
    from config import ServerConfig

    config = BridgeConfig(
        servers=[
            ServerConfig(
                name="<YOUR_MCP_SERVER>",
                command="~/<YOUR_MCP_SERVER>",
                args=["<MCP_ARGS>"],
                env={"<YOUR_MCP_SERVER>_TIMEOUT": "30"},  # env specifico del server
            ),
        ],
        llm=LLMConfig(
            base_url="http://localhost:8080/v1",
            api_key="none",
            model="local-model",
            temperature=0.2,
            max_tokens=4096,
        ),
    )

    from epistylion import MCPBridge
    epistylion = MCPBridge(config)
    async with epistylion:
        epistylion.print_tools()
        result = await retry_with_backoff(
            epistylion.agent.run,
            "Apri Blender e descrivi la scena attuale"
        )
        print(result.final_message)


if __name__ == "__main__":
    import sys

    examples = {
        "1": ("Uso base", example_basic),
        "2": ("LLM personalizzato", example_custom_llm),
        "3": ("Conversazione multi-turn", example_conversation),
        "4": ("Chiamata diretta tool", example_direct_tool_call),
        "5": ("Esporta tool OpenAI format", example_export_tools),
        "6": ("Monitoraggio step", example_with_monitoring),
        "7": ("Config programmatica", example_programmatic_config),
    }

    print("Esempi disponibili:")
    for k, (desc, _) in examples.items():
        print(f"  {k}) {desc}")

    choice = input("\nScegli esempio (1-7): ").strip()
    if choice in examples:
        asyncio.run(examples[choice][1]())
    else:
        print("Scelta non valida")
        sys.exit(1)
