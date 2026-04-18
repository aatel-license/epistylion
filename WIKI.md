# 📚 Wiki di Epistylion

## Indice

1. [Introduzione](#introduzione)
2. [Architettura](#architettura)
3. [Installazione](#installazione)
4. [Configurazione](#configurazione)
5. [Utilizzo Rapido](#utilizzo-rapido)
6. [Riferimento API](#riferimento-api)
7. [FAQ](#faq)

---

## Introduzione

**Epistylion** è una libreria Python che funge da ponte tra LLM e server MCP (Model Context Protocol). Permette di esporre le funzionalità dei server MCP come tool utilizzabili dagli agenti AI, con un'interfaccia unificata e semplice.

Il nome deriva dalla combinazione di *episteme* (conoscenza) e *stylion* (riferimento alla struttura/colonna), a simboleggiare il supporto strutturato per l'accesso alla conoscenza tramite tool.

---

## Architettura

```
┌─────────────────┐     ┌───────────────┐     ┌──────────────┐
│  Configurazione │────▶│   MCPBridge   │────▶│ Server MCP   │
│      & LLM      │     │ (Facade)      │     │ (stdio)      │
└─────────────────┘     └───────────────┘     └──────────────┘
                               ▲
                               │
                       ┌───────┴───────┐
                       │ Tool Registry │
                       └───────────────┘
```

### Componenti Principali

| Componente | Responsabilità | File |
|------------|----------------|--------|
| `MCPBridge` | Facade principale, gestisce l'intero ciclo di vita | `epistylion.py` |
| `MCPAgent` | Loop ReAct dell'agente, esecuzione tool | `agent.py` |
| `MCPClient` | Trasporto stdio per server MCP | `client.py` |
| `ToolRegistry` | Registrazione e lookup dei tool | `registry.py` |
| `BridgeConfig` | Caricamento configurazione | `config.py` |

---

## Installazione

```bash
# Clona il repository
git clone https://github.com/aatel-license/epistylion.git
cd epistylion

# Installa in modalità editabile
pip install -e .

# Oppure installa le dipendenze minime
pip install rich pydantic python-dotenv
```

---

## Configurazione

### File `mcp_servers.json`

Definisce i server MCP da connettere:

```json
{
  "mcpServers": {
    "blender": {
      "command": "uvx",
      "args": ["blender-mcp"]
    },
    "web-search": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-fetch"]
    }
  }
}
```

### Variabili d'Ambiente (`.env`)

Crea un file `.env` (usa `.env.example` come template):

```ini
LLM_BASE_URL=http://localhost:1234/v1
LLM_API_KEY=none
LLM_MODEL=qwen2.5-coder-7b-instruct
MCP_CONFIG_PATH=./mcp_servers.json
```

---

## Utilizzo Rapido

### Esempio Base

```python
from epistylion import MCPEpistylion, LLMConfig

# Configurazione LLM (opzionale, usa .env altrimenti)
llm = LLMConfig(base_url="http://localhost:11434/v1", model="qwen2.5:7b")

async with MCPEpistylion.from_config("mcp_servers.json") as epistylion:
    # L'agente può usare tutti i tool dei server connessi
    response = await epistylion.agent.run(
        "Crea un cubo in Blender e cerca notizie recenti sull'AI",
        max_steps=10
    )
    print(response.final_message)
```

### Visualizzare i Tool Disponibili

```python
async with MCPEpistylion.from_config("mcp_servers.json") as epistylion:
    epistylion.print_tools()
```

---

## Riferimento API

### `MCPBridge` (in `epistylion.py`)

**Metodi di Classe:**

- `MCPEpistylion.from_config(mcp_config_path=None, env_path=None)`  
  Crea un'istanza caricando la configurazione.

- `MCPEpistylion.from_llm_config(llm: LLMConfig, ...)`  
  Crea un'istanza con una configurazione LLM personalizzata.

**Metodi d'Istanza:**

| Metodo | Descrizione |
|--------|------------|
| `await connect()` | Connette ai server MCP e inizializza l'agente |
| `await disconnect()` | Chiude tutte le connessioni |
| `agent` (property) | Ritorna l'istanza di `MCPAgent` |
| `registry` (property) | Ritorna il `ToolRegistry` |
| `print_tools()` | Stampa una tabella con tutti i tool disponibili |
| `get_openai_tools(use_qualified_names=False)` | Esporta tool in formato OpenAI |

### `MCPAgent` (in `agent.py`)

**Metodo Principale:**

```python
await agent.run(query: str, max_steps: int = 20) -> AgentResponse
```

Ritorna un oggetto `AgentResponse` con:
- `final_message`: risposta finale dell'LLM
- `history`: lista di tutti i messaggi dello scambio
- `steps`: numero di iterazioni effettuate
- `tool_calls`: tool effettivamente chiamati

---

## FAQ

**Cosa succede se un server MCP non si connette?**  
L'agente continua a funzionare con gli altri server. I server falliti vengono stampati come avviso ma non bloccano l'esecuzione.

**Posso usare nomi qualificati per i tool?**  
Sì, imposta `use_qualified_names=True` in `connect()` o `get_openai_tools()`. I tool verranno nominati `server__tool` invece di `tool`, evitando conflitti tra server diversi.

---

© 2026 Epistylion Project — Licenza AATEL
