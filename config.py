"""
epistylion.config
~~~~~~~~~~~~~~~~~
Carica la configurazione MCP (mcp_servers.json) e le variabili d'ambiente (.env).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


# Carica .env dalla directory corrente (o da percorso esplicito).
def load_env(env_path: str | Path | None = None) -> None:
    """Carica le variabili d'ambiente dal file .env."""
    if env_path is not None:
        load_dotenv(dotenv_path=env_path, override=True)
    else:
        load_dotenv(override=True)


@dataclass
class ServerConfig:
    """Configurazione di un singolo server MCP."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    def full_env(self) -> dict[str, str]:
        """Restituisce l'env del processo: os.environ + env specifico del server."""
        merged = dict(os.environ)
        merged.update(self.env)
        return merged


@dataclass
class LLMConfig:
    """Configurazione del backend LLM OpenAI-compatible."""

    base_url: str
    api_key: str
    model: str
    temperature: float
    max_tokens: int = 0


@dataclass
class BridgeConfig:
    """Configurazione completa del bridge epistylion MCP."""

    servers: list[ServerConfig]
    llm: LLMConfig
    init_timeout: int = 30


def load_mcp_servers(path: str | Path) -> list[ServerConfig]:
    """Parsa il file JSON in stile Claude Desktop."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"MCP config non trovato: {path}")

    with path.open() as f:
        raw: dict[str, Any] = json.load(f)

    servers_raw: dict[str, Any] = raw.get("mcpServers", raw)
    servers: list[ServerConfig] = []

    for name, cfg in servers_raw.items():
        if not isinstance(cfg, dict):
            continue
        srv = ServerConfig(
            name=name,
            command=cfg["command"],
            args=cfg.get("args", []),
            env=cfg.get("env", {}),
        )
        servers.append(srv)

    return servers


def load_config(
    mcp_config_path: str | Path | None = None,
    env_path: str | Path | None = None,
) -> BridgeConfig:
    """
    Carica la configurazione completa.

    Ordine di priorità:
      1. argomenti espliciti
      2. variabili d'ambiente (anche da .env)
      3. valori di default
    """
    load_env(env_path)

    config_path = mcp_config_path or os.getenv("MCP_CONFIG_PATH", "./mcp_servers.json")
    servers = load_mcp_servers(config_path)

    llm = LLMConfig(
        base_url=os.getenv("LLM_BASE_URL", "http://localhost:8080/v1"),
        api_key=os.getenv("LLM_API_KEY", "none"),
        model=os.getenv("LLM_MODEL", "local-model"),
        temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
        max_tokens=int(os.getenv("LLM_MAX_TOKENS", "4096")),
    )

    return BridgeConfig(
        servers=servers,
        llm=llm,
        init_timeout=int(os.getenv("MCP_INIT_TIMEOUT", "30")),
    )
