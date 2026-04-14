"""
epistylion.agent
~~~~~~~~~~~~~~~~
Loop agente che orchestra LLM + tool MCP.
Compatibile con qualsiasi server OpenAI-compatible:
  - llama-cpp-python  (python -m llama_cpp.server ...)
  - Ollama            (http://localhost:11434/v1)
  - LM Studio         (http://localhost:1234/v1)
  - vLLM, text-generation-webui, ecc.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from client import MCPClient
from config import LLMConfig
from registry import ToolRegistry, mcp_result_to_string

logger = logging.getLogger(__name__)


# ── strutture dati ────────────────────────────────────────────────────────────

@dataclass
class AgentMessage:
    role: str          # "user" | "assistant" | "tool" | "system"
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None


@dataclass
class AgentResponse:
    """Risultato finale di un ciclo agent.run()."""
    final_message: str
    steps: int
    tool_calls_made: list[dict[str, Any]] = field(default_factory=list)


# ── agent ─────────────────────────────────────────────────────────────────────

class MCPAgent:
    """
    Agente che usa un LLM OpenAI-compatible + tool MCP.

    Il loop di esecuzione:
      1. Invia la conversazione all'LLM con la lista dei tool
      2. Se l'LLM risponde con tool_calls → esegue ogni tool via MCP
      3. Aggiunge i risultati alla conversazione e ripete
      4. Termina quando l'LLM risponde senza tool_calls (risposta finale)

    Parameters
    ----------
    llm_config : LLMConfig
    mcp_client : MCPClient
        Client MCP già connesso (con i server attivi).
    registry : ToolRegistry
        Registry dei tool MCP → formato OpenAI.
    system_prompt : str, optional
        System prompt iniziale.
    max_steps : int
        Limite massimo di iterazioni tool-call per evitare loop infiniti.
    use_qualified_names : bool
        Se True, i nomi dei tool usano il formato 'server__tool'.
    on_step : Callable, optional
        Callback chiamata dopo ogni step con (step_num, tool_name, result).
    """

    def __init__(
        self,
        llm_config: LLMConfig,
        mcp_client: MCPClient,
        registry: ToolRegistry,
        system_prompt: str = "Sei un assistente utile con accesso a vari tool.",
        max_steps: int = 20,
        use_qualified_names: bool = False,
        on_step: Callable[[int, str, str], None] | None = None,
    ) -> None:
        self._llm_config = llm_config
        self._mcp = mcp_client
        self._registry = registry
        self._system_prompt = system_prompt
        self._max_steps = max_steps
        self._use_qualified_names = use_qualified_names
        self._on_step = on_step

        self._openai = AsyncOpenAI(
            base_url=llm_config.base_url,
            api_key=llm_config.api_key,
        )

        # Strumenti nel formato OpenAI (calcolati una volta sola)
        self._openai_tools = registry.to_openai_tools(use_qualified_names)

    # ── API pubblica ──────────────────────────────────────────────────────────

    async def run(
        self,
        user_message: str,
        history: list[AgentMessage] | None = None,
    ) -> AgentResponse:
        """
        Esegue un ciclo completo (user → tool calls → risposta finale).

        Parameters
        ----------
        user_message : str
            Messaggio dell'utente.
        history : list[AgentMessage], optional
            Storico della conversazione precedente.

        Returns
        -------
        AgentResponse
        """
        messages = self._build_messages(user_message, history or [])
        tool_calls_made: list[dict[str, Any]] = []
        steps = 0

        while steps < self._max_steps:
            response = await self._call_llm(messages)
            choice = response.choices[0]
            msg = choice.message

            # Aggiungi la risposta dell'assistente alla history
            messages.append(self._assistant_msg_to_param(msg))

            # Nessun tool call → risposta finale
            if not msg.tool_calls:
                return AgentResponse(
                    final_message=msg.content or "",
                    steps=steps,
                    tool_calls_made=tool_calls_made,
                )

            # Esegui tutti i tool call in parallelo
            import asyncio
            tool_results = await asyncio.gather(
                *[self._execute_tool_call(tc) for tc in msg.tool_calls],
                return_exceptions=True,
            )

            for tc, result in zip(msg.tool_calls, tool_results):
                if isinstance(result, Exception):
                    result_str = f"[ERRORE] {result}"
                else:
                    result_str = result

                record = {
                    "tool": tc.function.name,
                    "args": tc.function.arguments,
                    "result": result_str,
                }
                tool_calls_made.append(record)
                logger.info("Tool '%s' → %s", tc.function.name, result_str[:200])

                if self._on_step:
                    self._on_step(steps + 1, tc.function.name, result_str)

                # Tool result message
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_str,
                })

            steps += 1

        logger.warning("Raggiunto limite massimo di step (%d)", self._max_steps)
        return AgentResponse(
            final_message="[LIMITE MASSIMO DI STEP RAGGIUNTO]",
            steps=steps,
            tool_calls_made=tool_calls_made,
        )

    async def stream(
        self,
        user_message: str,
        history: list[AgentMessage] | None = None,
    ) -> AsyncIterator[str]:
        """
        Versione streaming: yielda chunk di testo man mano che l'LLM risponde.
        I tool call vengono eseguiti silenziosamente (non in streaming).
        """
        messages = self._build_messages(user_message, history or [])
        steps = 0

        while steps < self._max_steps:
            # Prima chiamata non-streaming per verificare se ci sono tool call
            response = await self._call_llm(messages)
            choice = response.choices[0]
            msg = choice.message

            messages.append(self._assistant_msg_to_param(msg))

            if not msg.tool_calls:
                # Risposta finale: stream il testo
                if msg.content:
                    yield msg.content
                return

            # Tool calls: esegui e continua il loop
            import asyncio
            tool_results = await asyncio.gather(
                *[self._execute_tool_call(tc) for tc in msg.tool_calls],
                return_exceptions=True,
            )

            for tc, result in zip(msg.tool_calls, tool_results):
                result_str = f"[ERRORE] {result}" if isinstance(result, Exception) else result
                yield f"\n🔧 **{tc.function.name}** → `{result_str[:120]}...`\n"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_str,
                })

            steps += 1

    # ── helpers privati ────────────────────────────────────────────────────────

    def _build_messages(
        self,
        user_message: str,
        history: list[AgentMessage],
    ) -> list[ChatCompletionMessageParam]:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": self._system_prompt}
        ]
        for h in history:
            if h.role == "tool":
                messages.append({
                    "role": "tool",
                    "tool_call_id": h.tool_call_id or "",
                    "content": h.content,
                })
            else:
                messages.append({"role": h.role, "content": h.content})  # type: ignore[arg-type]

        messages.append({"role": "user", "content": user_message})
        return messages

    async def _call_llm(
        self, messages: list[ChatCompletionMessageParam]
    ):
        kwargs: dict[str, Any] = {
            "model": self._llm_config.model,
            "messages": messages,
            "temperature": self._llm_config.temperature,
            "max_tokens": self._llm_config.max_tokens,
        }
        if self._openai_tools:
            kwargs["tools"] = self._openai_tools
            kwargs["tool_choice"] = "auto"

        return await self._openai.chat.completions.create(**kwargs)

    async def _execute_tool_call(self, tc) -> str:
        """Esegue un singolo tool call e restituisce il risultato come stringa."""
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}

        # Risolvi il tool nel registry (supporta sia nomi originali che qualified)
        entry = self._registry.resolve(tc.function.name)
        if entry is None:
            return f"[ERRORE] Tool '{tc.function.name}' non trovato nel registry"

        # Chiama il server MCP corretto
        conn = self._mcp.find_server_for_tool(entry.tool.name)
        if conn is None:
            return f"[ERRORE] Nessun server connesso per il tool '{entry.tool.name}'"

        raw = await conn.call_tool(entry.tool.name, args)
        return mcp_result_to_string(raw)

    @staticmethod
    def _assistant_msg_to_param(msg) -> dict[str, Any]:
        """Converte un ChatCompletionMessage in un dict per la history."""
        d: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        return d
