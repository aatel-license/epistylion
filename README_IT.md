# epistylion

Una libreria Python per collegare server MCP (Model Context Protocol) a LLM e gestire tool in modo asincrono.

## Uso rapido
```python
from epistylion import MCPBridge, LLMConfig

async def main():
    llm = LLMConfig(
        base_url="http://localhost:11434/v1",
        api_key="ollama",
        model="qwen2.5:14b"
    )
    async with MCPBridge.from_llm_config(llm, "mcp_servers.json") as epistylion:
        result = await epistylion.agent.run("Crea un cubo in Blender")
        print(result.final_message)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

## Esempi completi
Vedi il file [`example_usage.py`](./example_usage.py) per sette esempi d'uso, inclusi:
- Configurazione da file `.env` o `mcp_servers.json`
- LLM personalizzato a runtime
- Conversazioni multi‑turno con history
- Chiamata diretta a tool MCP
- Export dei tool in formato OpenAI function‑calling
- Monitoraggio step con callback
- Configurazione programmatica senza file JSON

## Contributi
Apri una *issue* o un *pull request* su GitHub per suggerimenti, bugfix o nuove funzionalità. Segui le linee guida di `README.md` per mantenere lo stile del progetto.
# epistylion
