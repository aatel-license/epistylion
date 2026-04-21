# 📚 Epistylion

![Alt text](https://raw.githubusercontent.com/aatel-license/epistylion/refs/heads/main/epistylion.png "Epistylion")

> Bridging LLMs with MCP (Model Context Protocol) Servers

**Epistylion** is a Python library that acts as a bridge between LLMs and MCP (Model Context Protocol) servers. It connects to any number of local MCP servers via stdio, aggregates their tools, and exposes them to AI agents through a unified interface — with optional network exposure over HTTP.

The name combines *episteme* (knowledge) and *stylion* (referring to structure/column), symbolizing structured support for knowledge access through tools.

---

## 📖 Table of Contents

1. [Introduction](#introduction)
2. [Architecture](#architecture)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Quick Start](#quick-start)
6. [Skills System](#skills-system)
7. [CLI Reference](#cli-reference)
8. [API Reference](#api-reference)
9. [Network Servers](#network-servers)
10. [Web Console](#web-console)
11. [FAQ](#faq)

---

## 🚀 Introduction

### Key Features

- ✅ **Unified Interface**: Simple connection to one or more MCP servers
- ✅ **Automatic Tool Discovery**: Automatic detection of available tools
- ✅ **Graceful Error Handling**: Connection failures don't block execution
- ✅ **Qualified Tool Names**: Avoids conflicts between tools with the same name from different servers
- ✅ **Skill System**: Inject SKILL.md files into the agent's system prompt to specialize behavior
- ✅ **MCP Proxy Server**: Re-exposes aggregated tools as an HTTP/SSE MCP server on the LAN
- ✅ **OpenAI-compatible Server**: Exposes a `/v1/chat/completions` endpoint with MCP tools integrated
- ✅ **Streaming Support**: SSE streaming for the OpenAI-compatible endpoint
- ✅ **Web Console**: Browser-based dashboard, chat, and tool explorer with skill selector
- ✅ **Rich Output**: Uses the `rich` library for readable and colored output

---

## 🏗️ Architecture

### Local agent mode

```
┌─────────────────┐     ┌───────────────┐     ┌──────────────────┐
│  Configuration  │────▶│  MCPBridge    │────▶│ MCP Server stdio │
│  .env / JSON    │     │  (Facade)     │     │ blender, mnheme… │
└─────────────────┘     └───────┬───────┘     └──────────────────┘
                                │
                    ┌───────────┴────────────┐
                    │      MCPAgent          │
                    │  skill → system prompt │
                    │  LLM ↔ tool call loop  │
                    └────────────────────────┘
```

### Network server modes

```
LAN client (MCP)    ──HTTP/SSE──▶ MCPProxyServer    ──stdio──▶ MCP servers
LAN client (OpenAI) ──HTTP     ──▶ OpenAIProxyServer ──▶ LLM + MCP tools
Browser             ──HTTP     ──▶ Web Console       ──▶ Epistylion API
```

### Main Components

| Component | Responsibility | File |
|-----------|----------------|------|
| `MCPBridge` | Main facade, manages the entire lifecycle | `epistylion.py` |
| `MCPAgent` | ReAct agent loop, tool execution, skill injection | `agent.py` |
| `SkillRegistry` | Skill loading, indexing, system prompt injection | `skills.py` |
| `MCPClient` | Stdio transport for MCP servers | `client.py` |
| `ToolRegistry` | Tool registration, lookup, OpenAI conversion | `registry.py` |
| `BridgeConfig` | Configuration loading (.env + JSON) | `config.py` |
| `MCPProxyServer` | HTTP/SSE MCP proxy server for LAN | `server_mcp.py` |
| `OpenAIProxyServer` | OpenAI-compatible HTTP server for LAN | `server_openai.py` |
| CLI | Interactive chat and server launcher | `cli.py` |
| Web Console | Browser dashboard, chat, skill selector | `webapp/` |

---

## 📦 Installation

### Prerequisites

- Python 3.10+
- pip or uv

```bash
git clone https://github.com/aatel-license/epistylion.git
cd epistylion
pip install -e .

# Or install dependencies manually
pip install mcp openai python-dotenv anyio rich uvicorn starlette
```

### Dependencies

| Package | Minimum Version | Purpose |
|---------|-----------------|---------|
| `mcp` | 1.0+ | MCP protocol client/server |
| `openai` | 1.0+ | LLM API calls |
| `python-dotenv` | 1.0+ | `.env` loading |
| `anyio` | 4.0+ | Async I/O |
| `rich` | 13.0+ | Terminal output |
| `uvicorn` | 0.20+ | HTTP server (network modes) |
| `starlette` | 0.30+ | HTTP routing (network modes) |

---

## ⚙️ Configuration

### `mcp_servers.json`

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

### `.env`

```ini
LLM_BASE_URL=http://localhost:1234/v1
LLM_API_KEY=none
LLM_MODEL=qwen2.5-coder-7b-instruct
LLM_TEMPERATURE=0.2
LLM_MAX_TOKENS=4096

MCP_CONFIG_PATH=./mcp_servers.json
MCP_INIT_TIMEOUT=30

# Skills directory (default: ./skills)
EPISTYLION_SKILLS_DIR=skills
```

### Compatible LLM Backends

| Backend | `LLM_BASE_URL` | `LLM_API_KEY` |
|---------|----------------|---------------|
| LM Studio | `http://localhost:1234/v1` | `none` |
| Ollama | `http://localhost:11434/v1` | `ollama` |
| llama-cpp-python | `http://localhost:8080/v1` | `none` |
| vLLM | `http://localhost:8000/v1` | `none` |
| OpenAI | `https://api.openai.com/v1` | your key |

---

## 🎯 Quick Start

```python
import asyncio
from epistylion import MCPBridge

async def main():
    async with MCPBridge.from_config("mcp_servers.json") as bridge:
        bridge.print_tools()
        response = await bridge.agent.run(
            "Create a cube in Blender and search for recent AI news"
        )
        print(response.final_message)

asyncio.run(main())
```

### Custom LLM Config

```python
from epistylion import MCPBridge, LLMConfig

llm = LLMConfig(
    base_url="http://localhost:11434/v1",
    api_key="ollama",
    model="qwen2.5:14b",
    temperature=0.1,
    max_tokens=8192,
)

async with MCPBridge.from_llm_config(llm, "mcp_servers.json") as bridge:
    response = await bridge.agent.run("Describe the current Blender scene")
    print(response.final_message)
```

### Multi-turn Conversation

```python
from epistylion import MCPBridge, AgentMessage

async with MCPBridge.from_config("mcp_servers.json") as bridge:
    history: list[AgentMessage] = []

    for user_msg in ["How many objects in the scene?", "Add a red sphere"]:
        response = await bridge.agent.run(user_msg, history)
        print(response.final_message)
        history.append(AgentMessage(role="user",      content=user_msg))
        history.append(AgentMessage(role="assistant", content=response.final_message))
```

### Direct Tool Call (without LLM)

```python
async with MCPBridge.from_config("mcp_servers.json") as bridge:
    result = await bridge.call_tool("get_scene_info", {})
    print(result)
```

---

## ✦ Skills System

A **skill** is a `SKILL.md` file that contains natural-language instructions injected into the agent's system prompt before the ReAct loop starts. The skill shapes how the model reasons and responds — it is not a tool and does not appear in the tool list.

### Why skills, not tool-calling?

Injecting a skill into the system prompt means the LLM that is already orchestrating the task also carries the skill's instructions. Calling a second LLM with the skill would add latency, double the token cost, and achieve nothing extra. Skills belong in the agent, not in a downstream service.

### Directory structure

```
skills/
    code/SKILL.md         → writes clean Python with type hints and docstrings
    translate/SKILL.md    → professional multilingual translator
    summarize/SKILL.md    → concise summaries preserving key facts
    json_output/SKILL.md  → responds only with valid JSON
    italian/SKILL.md      → always responds in Italian
    my_skill/SKILL.md     → any custom skill you add
```

Each `SKILL.md` is plain markdown with natural-language instructions. New files are discovered automatically at startup — no code changes needed.

### Usage — Python API

```python
# Skill fixed for the entire session
async with MCPBridge.from_config("mcp_servers.json") as bridge:
    await bridge.connect(skill="code")

    r = await bridge.agent.run("Write a CSV parser")
    print(r.final_message)
    print(r.skill_used)   # → "code"
```

```python
# Different skill per call
async with MCPBridge.from_config("mcp_servers.json") as bridge:
    await bridge.connect()

    r1 = await bridge.agent.run("Write a JSON parser",       skill="code")
    r2 = await bridge.agent.run("Translate to French: ...",  skill="translate")
    r3 = await bridge.agent.run("Summarize this article:",   skill="summarize")
    r4 = await bridge.agent.run("What tools do you have?")   # no skill
```

```python
# Default skill + per-call override
async with MCPBridge.from_config("mcp_servers.json") as bridge:
    await bridge.connect(skill="italian")   # default: always respond in Italian

    r1 = await bridge.agent.run("What is RAG?")              # → Italian
    r2 = await bridge.agent.run("Write code", skill="code")  # → overrides default
    r3 = await bridge.agent.run("Ciao!",      skill="")      # → "" disables default
```

```python
# Inspect available skills
async with MCPBridge.from_config("mcp_servers.json") as bridge:
    await bridge.connect(skill="code")
    bridge.print_skills()   # rich table: name, description, file, (default) marker
```

### Creating a custom skill

Create `skills/my_skill/SKILL.md`:

```markdown
# My Skill

You are an expert in Italian cuisine.
- Always structure recipes with Ingredients and Procedure sections.
- State preparation and cooking times.
- Suggest regional Italian variations when relevant.
```

At the next `connect()` call it is available as `skill="my_skill"` — no code changes needed.

### `AgentResponse` — skill tracking

```python
response = await bridge.agent.run("Write a parser", skill="code")

print(response.final_message)    # final LLM answer
print(response.skill_used)       # "code"
print(response.steps)            # number of tool-call rounds
print(response.tool_calls_made)  # [{tool, args, result}, ...]
```

### Skills directory environment variable

```ini
# .env
EPISTYLION_SKILLS_DIR=/absolute/path/to/skills
```

---

## 💻 CLI Reference

```bash
# Interactive chat (no skill)
python -m epistylion

# Interactive chat with skill active
python -m epistylion --skill code
python -m epistylion --skill italian

# List skills without starting the agent
python -m epistylion --list-skills

# List skills from a custom directory
python -m epistylion --list-skills --skills-dir /path/to/skills

# One-shot query with skill
python -m epistylion --run "Write a CSV parser" --skill code
python -m epistylion --run "Summarise this: ..." --skill summarize

# Skill from a direct file path
python -m epistylion --skill /path/to/SKILL.md

# List all available MCP tools
python -m epistylion --list-tools

# List tools in OpenAI JSON format
python -m epistylion --list-tools --json --qualified

# Custom config paths
python -m epistylion --config /path/to/servers.json --env /path/to/.env

# Network servers
python -m epistylion --serve-mcp
python -m epistylion --serve-openai
python -m epistylion --serve-mcp --serve-openai
python -m epistylion --serve-openai --expose-tool-calls
```

### Chat session commands

Once inside the interactive chat, the following slash commands are available:

| Command | Effect |
|---------|--------|
| `/tools` | Show available MCP tools |
| `/skills` | Show available skills (active one highlighted) |
| `/skill <name>` | Activate a skill for all subsequent messages |
| `/skill off` | Deactivate the current skill |
| `/clear` | Reset conversation history (skill stays active) |
| `exit` | Quit |

The response panel always shows which skill was active:
```
╭─ Assistente (skill: code) ────────────────────────╮
│ ...                                               │
╰───────────────────────────────────────────────────╯
```

---

## 📚 API Reference

### `MCPBridge` (in `epistylion.py`)

#### Class Methods

```python
MCPBridge.from_config(mcp_config_path=None, env_path=None) -> MCPBridge
MCPBridge.from_llm_config(llm: LLMConfig, mcp_config_path=None, env_path=None) -> MCPBridge
```

#### `connect()`

```python
await bridge.connect(
    system_prompt       = "Sei un assistente utile con accesso a vari tool MCP.",
    max_steps           = 20,
    use_qualified_names = False,
    on_step             = None,          # Callable(step, tool_name, result)
    skill               = None,          # default skill for all agent.run() calls
    skills_dir          = None,          # overrides EPISTYLION_SKILLS_DIR
)
```

#### Instance Methods

| Method | Description |
|--------|-------------|
| `await disconnect()` | Closes all connections |
| `async with bridge:` | Context manager (connect + disconnect) |
| `agent` *(property)* | Returns the `MCPAgent` instance |
| `registry` *(property)* | Returns the `ToolRegistry` |
| `skill_registry` *(property)* | Returns the `SkillRegistry` |
| `print_tools()` | Rich table of all available MCP tools |
| `print_skills()` | Rich table of all available skills |
| `get_openai_tools(use_qualified_names=False)` | Tools in OpenAI function-calling format |
| `await call_tool(tool_name, arguments)` | Calls a tool directly, bypassing the LLM |

### `MCPAgent` (in `agent.py`)

```python
await agent.run(
    user_message: str,
    history:      list[AgentMessage] | None = None,
    skill:        str | None = None,    # overrides default_skill for this call
                                        # skill="" disables default_skill
) -> AgentResponse
```

```python
async for chunk in agent.stream(user_message, history, skill="code"):
    print(chunk, end="")
```

`AgentResponse` fields:

| Field | Type | Description |
|-------|------|-------------|
| `final_message` | `str` | Final LLM response |
| `steps` | `int` | Number of tool-call iterations |
| `tool_calls_made` | `list[dict]` | Each tool call: `{tool, args, result}` |
| `skill_used` | `str \| None` | Name of the skill injected, or `None` |

### `SkillRegistry` (in `skills.py`)

```python
registry = SkillRegistry("skills/")

registry.list()                       # ["code", "italian", "summarize", ...]
registry.get("code")                  # Skill(name, path, description, content)
registry.load_path("/path/SKILL.md")  # load from explicit path

# Returns: <skill content> --- <base_system>
system = registry.apply("code", base_system="You are a helpful assistant.")
```

### `LLMConfig`

```python
@dataclass
class LLMConfig:
    base_url:    str
    api_key:     str
    model:       str
    temperature: float = 0.2
    max_tokens:  int   = 4096
```

### `ToolRegistry` (in `registry.py`)

| Method | Description |
|--------|-------------|
| `register_server_tools(server_name, tools)` | Registers tools from a server |
| `to_openai_tools(use_qualified_names=False)` | Returns all tools in OpenAI format |
| `resolve(name)` | Resolves a tool name to a `ToolEntry` |
| `summary()` | Returns `{server_name: [tool_names]}` |
| `all_entries()` | Returns all `ToolEntry` objects |

---

## 🌐 Network Servers

### A) MCP Proxy Server (`server_mcp.py`)

Aggregates all local MCP tools and re-exposes them as a single HTTP/SSE MCP server on the LAN.

```python
from epistylion.server_mcp import MCPProxyServer

server = MCPProxyServer.from_config("mcp_servers.json", use_qualified_names=True)
await server.run(host="0.0.0.0", port=9000)
```

**Endpoints:**

| Endpoint | Description |
|----------|-------------|
| `GET /sse` | MCP SSE transport |
| `POST /messages/` | MCP message handler |
| `GET /health` | JSON: connected servers, tool count |

**Claude Desktop integration:**
```json
{
  "mcpServers": {
    "epistylion": { "url": "http://192.168.1.x:9000/sse" }
  }
}
```

---

### B) OpenAI-compatible Server (`server_openai.py`)

Exposes a standard `/v1/chat/completions` endpoint. The client sends a normal chat request; the bridge handles all MCP tool calls internally. Accepts an optional `skill` field in the request body.

```python
from epistylion.server_openai import OpenAIProxyServer

server = OpenAIProxyServer.from_config("mcp_servers.json", expose_tool_calls=True)
await server.run(host="0.0.0.0", port=8081)
```

**Endpoints:**

| Endpoint | Description |
|----------|-------------|
| `POST /v1/chat/completions` | Chat completions — supports `stream`, `skill` |
| `GET /v1/models` | Lists the configured model |
| `GET /v1/skills` | Returns `{"skills": ["code", "translate", ...]}` |
| `GET /v1/status` | Full status: LLM, servers, tools, skills |
| `GET /health` | JSON health check |
| `GET /metrics` | Request and tool call counters and latencies |

**Request body — extra fields supported:**

```json
{
  "model": "local-model",
  "messages": [...],
  "stream": false,
  "skill": "code"
}
```

**Client usage:**
```python
from openai import OpenAI

client = OpenAI(base_url="http://192.168.1.x:8081/v1", api_key="none")
response = client.chat.completions.create(
    model="local-model",
    messages=[{"role": "user", "content": "Write a CSV parser"}],
    extra_body={"skill": "code"},
)
print(response.choices[0].message.content)
```

---

## 🖥️ Web Console

A browser-based console for the Epistylion OpenAI server. Vanilla JS, no framework, no build step — open `webapp/index.html` directly or serve it statically.

### Tabs

| Tab | Description |
|-----|-------------|
| **Dashboard** | Server health, uptime, LLM backend info, connected servers, available skills |
| **Chat** | Multi-turn chat with system prompt override and skill selector |
| **Tools** | MCP tool explorer with per-server tabs, search, parameter view, JSON copy |
| **Metrics** | Request counters, latency charts, tool call statistics |
| **Settings** | Server URL, API key, model, auto-refresh interval |

### Skill selector in Chat

The chat bar contains a skill dropdown populated automatically via `GET /v1/skills`:

```
System → [override system prompt...] | Skill → [ code ▼ ]
```

- Selecting a skill turns the dropdown amber and adds the skill to every outgoing request
- Each assistant response shows a `⬡ code` badge next to the role label
- Selecting `none` sends requests without a skill
- The dashboard "LLM Backend" panel shows all available skills as tags

### First-time setup

1. Open `webapp/index.html` in a browser (or serve it)
2. You will be redirected to Settings — enter the server URL (e.g. `http://localhost:8081`)
3. Click **Save & Connect**
4. Switch to Chat and start typing

---

## ❓ FAQ

**What happens if an MCP server fails to connect?**  
The bridge continues functioning with the remaining servers. Failed servers are printed as warnings but don't block execution.

**Can I use qualified names for tools?**  
Yes, pass `use_qualified_names=True` to `connect()` or `get_openai_tools()`, or use `--qualified` in the CLI. Tools will be named `server__tool`, avoiding conflicts across servers.

**Can I run both network servers simultaneously?**  
Yes: `python -m epistylion --serve-mcp --serve-openai`. They run in parallel via `asyncio.gather`.

**Can I use the OpenAI server from Open WebUI or other frontends?**  
Yes. Set the base URL to `http://<host>:8081/v1` and the API key to `none`.

**Does it support streaming?**  
Yes. The OpenAI-compatible server supports `"stream": true`. Skills work transparently with streaming — the skill is injected before the first token.

**How do I add a skill?**  
Create `skills/<name>/SKILL.md` with natural-language instructions. No code changes needed — it is discovered automatically at the next `connect()`.

**Why are skills injected in the system prompt instead of being tools?**  
A skill shapes *how the model reasons*, not *what it can do*. Injecting it into the system prompt means the single LLM that is already orchestrating the task carries the skill's instructions from the first token. Routing through a second LLM with the skill would double latency and token cost with no benefit.

**Can I use a skill from a file path instead of the skills directory?**  
Yes: `skill="/absolute/path/to/SKILL.md"` works in both the Python API and the CLI `--skill` flag.

---

## 📄 License

© 2026 Epistylion Project — [AATEL License](https://aatel.org)