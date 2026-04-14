"""
epistylion.registry
~~~~~~~~~~~~~~~~~~~~
Converte i tool MCP nel formato OpenAI function-calling e gestisce
il registro centralizzato dei tool disponibili.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from mcp.types import Tool

logger = logging.getLogger(__name__)


@dataclass
class ToolEntry:
    """Coppia (server_name, Tool MCP)."""
    server_name: str
    tool: Tool

    @property
    def qualified_name(self) -> str:
        """Nome univoco nel formato 'server__tool' per evitare collisioni."""
        return f"{self.server_name}__{self.tool.name}"


class ToolRegistry:
    """
    Raccoglie i tool da tutti i server MCP connessi e li converte
    nel formato atteso dalle API OpenAI-compatible.
    """

    def __init__(self) -> None:
        # qualified_name → ToolEntry
        self._tools: dict[str, ToolEntry] = {}
        # nome originale → qualified_name  (per lookup veloce)
        self._name_index: dict[str, str] = {}

    def register_server_tools(
        self, server_name: str, tools: list[Tool]
    ) -> None:
        """Registra tutti i tool di un server."""
        for tool in tools:
            entry = ToolEntry(server_name=server_name, tool=tool)
            self._tools[entry.qualified_name] = entry
            # Se il nome originale è già usato da un altro server logghiamo
            if tool.name in self._name_index:
                existing = self._name_index[tool.name]
                logger.warning(
                    "Conflitto tool '%s': '%s' sovrascrive '%s'. "
                    "Usa qualified_name per disambiguare.",
                    tool.name, entry.qualified_name, existing,
                )
            self._name_index[tool.name] = entry.qualified_name
        logger.debug("[%s] Registrati %d tool", server_name, len(tools))

    # ── lookup ───────────────────────────────────────────────────────────────

    def resolve(self, name: str) -> ToolEntry | None:
        """
        Risolve un nome tool (originale o qualified) in un ToolEntry.
        Accetta sia 'tool_name' che 'server__tool_name'.
        """
        if name in self._tools:
            return self._tools[name]
        qname = self._name_index.get(name)
        if qname:
            return self._tools[qname]
        return None

    def all_entries(self) -> list[ToolEntry]:
        return list(self._tools.values())

    def summary(self) -> dict[str, list[str]]:
        """Dizionario server → lista nomi tool (per debug/stampa)."""
        result: dict[str, list[str]] = {}
        for entry in self._tools.values():
            result.setdefault(entry.server_name, []).append(entry.tool.name)
        return result

    # ── conversione OpenAI ────────────────────────────────────────────────────

    def to_openai_tools(
        self,
        use_qualified_names: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Converte tutti i tool registrati nel formato OpenAI function-calling.

        Parameters
        ----------
        use_qualified_names:
            Se True, il nome della funzione sarà 'server__tool' invece di 'tool'.
            Utile quando ci sono conflitti di nome tra server diversi.
        """
        return [
            _mcp_tool_to_openai(entry, use_qualified_names)
            for entry in self._tools.values()
        ]

    def to_openai_tools_for_server(
        self, server_name: str, use_qualified_names: bool = False
    ) -> list[dict[str, Any]]:
        """Tool OpenAI solo per un server specifico."""
        return [
            _mcp_tool_to_openai(entry, use_qualified_names)
            for entry in self._tools.values()
            if entry.server_name == server_name
        ]


# ── helpers privati ────────────────────────────────────────────────────────────

def _mcp_tool_to_openai(
    entry: ToolEntry, use_qualified_names: bool = False
) -> dict[str, Any]:
    """
    Converte un singolo ToolEntry MCP nel dict OpenAI:

    {
      "type": "function",
      "function": {
        "name": "...",
        "description": "...",
        "parameters": { ... }  # JSON Schema
      }
    }
    """
    tool = entry.tool
    func_name = entry.qualified_name if use_qualified_names else tool.name

    # inputSchema è già un dict JSON Schema (o None)
    parameters: dict[str, Any]
    if tool.inputSchema:
        parameters = _clean_schema(dict(tool.inputSchema))
    else:
        parameters = {"type": "object", "properties": {}}

    return {
        "type": "function",
        "function": {
            "name": func_name,
            "description": tool.description or f"Tool '{tool.name}' dal server '{entry.server_name}'",
            "parameters": parameters,
        },
    }


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """
    Pulizia leggera dello schema per massimizzare la compatibilità
    con modelli OpenAI-compatible che non supportano tutte le keywords JSON Schema.
    """
    # Rimuove $schema se presente (alcuni modelli si confondono)
    schema.pop("$schema", None)
    schema.pop("title", None)
    # Assicura che ci sia sempre "type"
    if "type" not in schema:
        schema["type"] = "object"
    return schema


# ── serializzazione risultati MCP ─────────────────────────────────────────────

def mcp_result_to_string(content: Any) -> str:
    """
    Converte il risultato di una chiamata MCP (lista di ContentBlock)
    in una stringa da passare come tool_result all'LLM.
    """
    if content is None:
        return ""

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if hasattr(block, "text"):
                parts.append(block.text)
            elif hasattr(block, "data"):
                # ImageContent o blob → placeholder
                parts.append(f"[binary data: {type(block).__name__}]")
            elif isinstance(block, dict):
                text = block.get("text") or block.get("data", "")
                parts.append(str(text))
            else:
                parts.append(str(block))
        return "\n".join(parts)

    # fallback
    try:
        return json.dumps(content, ensure_ascii=False, indent=2)
    except Exception:
        return str(content)
