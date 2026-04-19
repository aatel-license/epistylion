"""
epistylion.agent
~~~~~~~~~~~~~~~~

Loop agente che orchestra LLM + tool MCP.

Compatibile con qualsiasi server OpenAI-compatible:
- llama-cpp-python (python -m llama_cpp.server ...)
- Ollama (http://localhost:11434/v1)
- LM Studio (http://localhost:1234/v1)
- vLLM, text-generation-webui, ecc.

FIX v2:
- System prompt più robusto che forza l'uso continuo dei tool
- Warning esplicito se il LLM risponde senza usare nessun tool al primo step
- Logging migliorato per debug del loop ReAct
- stream(): aggiunto tracking degli step e warning a fine loop
- _call_llm(): nessuna modifica a tool_choice (rimane "auto" per compatibilità
  con modelli locali che non supportano "required")
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

# ── System prompt di default ───────────────────────────────────────────────────
# FIX: istruisce esplicitamente il modello a usare i tool per tutti i passi
# necessari prima di rispondere all'utente. Cruciale per modelli locali piccoli.
DEFAULT_SYSTEM_PROMPT = """\
Sei un agente AI con accesso a tool MCP (Model Context Protocol).

REGOLE FONDAMENTALI:
1. Per completare un task che richiede operazioni su sistemi esterni (es. Blender,
   filesystem, web), DEVI usare i tool appropriati — non puoi inventare risultati.
2. Usa i tool TUTTE LE VOLTE CHE SERVONO: se un task richiede più passi, chiama
   un tool per ogni passo, uno alla volta.
3. NON rispondere all'utente finché non hai eseguito TUTTI i passi richiesti con
   i tool. Dopo ogni tool result, valuta se ne servono altri prima di concludere.
4. Se un tool fallisce, segnalalo chiaramente e, se possibile, prova un approccio
   alternativo con un altro tool.
5. Quando hai completato tutti i passi, riassumi cosa è stato fatto in modo chiaro.
"""


# ── strutture dati ────────────────────────────────────────────────────────────

@dataclass
class AgentMessage:
    role: str  # "user" | "assistant" | "tool" | "system"
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None


@dataclass
class AgentResponse:
    """Risultato finale di un ciclo agent.run()."""
    final_message: str
    steps: int
    tool_calls_made: list[dict[str, Any]] = field(default_factory=list)
    # FIX: aggiunto campo per segnalare se il limite step è stato raggiunto
    max_steps_reached: bool = False


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
        System prompt iniziale. Di default usa DEFAULT_SYSTEM_PROMPT che
        forza il modello a usare i tool per tutti i passi necessari.
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
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
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
            IMPORTANTE: in una webapp, passa sempre la history accumulata
            altrimenti il modello non ricorderà i tool call precedenti.

        Returns
        -------
        AgentResponse
        """
        messages = self._build_messages(user_message, history or [])
        tool_calls_made: list[dict[str, Any]] = []
        steps = 0

        logger.debug("Agent.run() avviato | max_steps=%d | tool disponibili=%d",
                     self._max_steps, len(self._openai_tools))

        while steps < self._max_steps:
            logger.debug("Step %d/%d — chiamata LLM...", steps + 1, self._max_steps)
            response = await self._call_llm(messages)
            choice = response.choices[0]
            msg = choice.message

            # Aggiungi la risposta dell'assistente alla history
            messages.append(self._assistant_msg_to_param(msg))

            # Nessun tool call → risposta finale
            if not msg.tool_calls:
                # FIX: warning se il LLM non ha usato nessun tool al primo step.
                # Spesso indica che il modello ha "capito male" il task o che
                # il system prompt non è abbastanza chiaro.
                if steps == 0 and self._openai_tools:
                    logger.warning(
                        "LLM ha risposto senza usare nessun tool al primo step! "
                        "Potrebbe indicare un problema di system prompt o un modello "
                        "che non gestisce bene il tool calling. "
                        "Risposta: %s",
                        (msg.content or "")[:300],
                    )
                else:
                    logger.debug("Step %d — nessun tool call, risposta finale.", steps)

                return AgentResponse(
                    final_message=msg.content or "",
                    steps=steps,
                    tool_calls_made=tool_calls_made,
                )

            logger.debug(
                "Step %d — %d tool call(s): %s",
                steps + 1,
                len(msg.tool_calls),
                [tc.function.name for tc in msg.tool_calls],
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

        # FIX: log più chiaro quando si raggiunge il limite
        logger.warning(
            "Raggiunto limite massimo di step (%d). Tool usati: %s",
            self._max_steps,
            [r["tool"] for r in tool_calls_made],
        )
        return AgentResponse(
            final_message="[LIMITE MASSIMO DI STEP RAGGIUNTO]",
            steps=steps,
            tool_calls_made=tool_calls_made,
            max_steps_reached=True,
        )

    async def stream(
        self,
        user_message: str,
        history: list[AgentMessage] | None = None,
    ) -> AsyncIterator[str]:
        """
        Versione streaming: yielda chunk di testo man mano che l'LLM risponde.
        I tool call vengono eseguiti silenziosamente (non in streaming).

        FIX: aggiunto tracking degli step e warning se il loop raggiunge il limite.
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
                # FIX: warning anche nello stream se step==0 senza tool call
                if steps == 0 and self._openai_tools:
                    logger.warning(
                        "stream(): LLM ha risposto senza usare nessun tool al primo step!"
                    )
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

        # FIX: yielda un messaggio di errore invece di uscire silenziosamente
        logger.warning("stream(): raggiunto limite massimo di step (%d)", self._max_steps)
        yield "\n⚠️ **[LIMITE MASSIMO DI STEP RAGGIUNTO]**\n"

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
            # FIX NOTE: "auto" lascia libertà al modello di non usare tool.
            # Se il modello ignora i tool, il log a step==0 ti avviserà.
            # NON usiamo "required" perché creerebbe un loop infinito con modelli
            # che non sanno quando fermarsi (e non è supportato da tutti i backend).
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