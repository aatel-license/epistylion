"""
mcp_bridge.server_openai
~~~~~~~~~~~~~~~~~~~~~~~~
Espone un endpoint **OpenAI-compatible** (``/v1/chat/completions``) sulla LAN,
con i tool MCP già integrati nel loop agente.

Il client remoto fa una normale chiamata OpenAI e riceve la risposta finale
dopo che il bridge ha già eseguito tutti i tool call necessari.

Flusso::

    LAN client ──POST /v1/chat/completions──► questo server
                                              ├──► LLM locale (llama-cpp-python/Ollama/…)
                                              └──► tool MCP (blender, mnheme, scrapling…)
                ◄── risposta finale ──────────┘

Avvio rapido::

    python -m mcp_bridge --serve-openai
    python -m mcp_bridge --serve-openai --host 0.0.0.0 --openai-port 8081

Oppure da codice::

    from mcp_bridge.server_openai import OpenAIProxyServer
    server = OpenAIProxyServer.from_config("mcp_servers.json")
    await server.run(host="0.0.0.0", port=8081)

Endpoint esposti
----------------
POST /v1/chat/completions   Completions con tool MCP integrati (streaming opzionale)
GET  /v1/models             Lista modello configurato
GET  /health                Stato server e server MCP connessi
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .agent import AgentMessage
from .bridge import MCPBridge
from .config import BridgeConfig, load_config

logger = logging.getLogger(__name__)


class OpenAIProxyServer:
    """
    Server HTTP OpenAI-compatible che orchestra LLM + tool MCP.

    Il client remoto invia una richiesta ``/v1/chat/completions`` standard;
    il server esegue il loop agente (LLM ↔ tool MCP) e restituisce la risposta
    finale come se fosse una normale completion — senza che il client debba
    sapere nulla di MCP.

    Parameters
    ----------
    config : BridgeConfig
    system_prompt : str
        System prompt iniettato in ogni richiesta (può essere sovrascritto
        dal client inserendo un messaggio con role="system").
    max_steps : int
        Limite massimo di iterazioni tool-call per richiesta.
    expose_tool_calls : bool
        Se True, la risposta include un campo extra ``_tool_calls`` con il
        dettaglio di ogni tool eseguito (utile per debug).
    """

    def __init__(
        self,
        config: BridgeConfig,
        system_prompt: str = "Sei un assistente utile con accesso a vari tool MCP.",
        max_steps: int = 20,
        expose_tool_calls: bool = False,
    ) -> None:
        self._config = config
        self._system_prompt = system_prompt
        self._max_steps = max_steps
        self._expose_tool_calls = expose_tool_calls
        self._bridge: MCPBridge | None = None

    # ── factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_config(
        cls,
        mcp_config_path: str | Path | None = None,
        env_path: str | Path | None = None,
        **kwargs,
    ) -> "OpenAIProxyServer":
        config = load_config(mcp_config_path, env_path)
        return cls(config, **kwargs)

    # ── avvio ─────────────────────────────────────────────────────────────────

    async def run(
        self,
        host: str = "0.0.0.0",
        port: int = 8081,
    ) -> None:
        """Connette i server MCP locali e avvia il server HTTP."""
        import uvicorn

        self._bridge = MCPBridge(self._config)
        await self._bridge.connect(
            system_prompt=self._system_prompt,
            max_steps=self._max_steps,
        )

        app = self._build_starlette_app()
        logger.info(
            "OpenAI-compatible server in ascolto su http://%s:%d/v1/chat/completions",
            host, port,
        )

        cfg = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(cfg)
        try:
            await server.serve()
        finally:
            if self._bridge:
                await self._bridge.disconnect()

    # ── Starlette app ─────────────────────────────────────────────────────────

    def _build_starlette_app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/v1/chat/completions", self._handle_completions, methods=["POST"]),
                Route("/v1/models", self._handle_models, methods=["GET"]),
                Route("/health", self._handle_health, methods=["GET"]),
            ]
        )

    # ── handler /v1/chat/completions ──────────────────────────────────────────

    async def _handle_completions(self, request: Request) -> Response:
        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            return JSONResponse(
                {"error": {"message": "Body JSON non valido", "type": "invalid_request_error"}},
                status_code=400,
            )

        messages: list[dict[str, Any]] = body.get("messages", [])
        stream: bool = body.get("stream", False)

        # Estrai history e ultimo messaggio utente
        history, user_message = self._parse_messages(messages)

        if not user_message:
            return JSONResponse(
                {"error": {"message": "Nessun messaggio utente trovato", "type": "invalid_request_error"}},
                status_code=400,
            )

        if stream:
            return StreamingResponse(
                self._stream_completion(user_message, history, body),
                media_type="text/event-stream",
            )
        else:
            return await self._blocking_completion(user_message, history, body)

    # ── completions blocking ──────────────────────────────────────────────────

    async def _blocking_completion(
        self,
        user_message: str,
        history: list[AgentMessage],
        body: dict[str, Any],
    ) -> JSONResponse:
        assert self._bridge is not None

        try:
            response = await self._bridge.agent.run(user_message, history)
        except Exception as exc:
            logger.error("Errore agent.run: %s", exc)
            return JSONResponse(
                {"error": {"message": str(exc), "type": "internal_error"}},
                status_code=500,
            )

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        model = body.get("model", self._config.llm.model)
        created = int(time.time())

        result: dict[str, Any] = {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": response.final_message,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                # Non abbiamo token count reali dall'agente, usiamo placeholder
                "prompt_tokens": -1,
                "completion_tokens": -1,
                "total_tokens": -1,
            },
        }

        if self._expose_tool_calls:
            result["_mcp_tool_calls"] = response.tool_calls_made
            result["_mcp_steps"] = response.steps

        return JSONResponse(result)

    # ── completions streaming ─────────────────────────────────────────────────

    async def _stream_completion(
        self,
        user_message: str,
        history: list[AgentMessage],
        body: dict[str, Any],
    ) -> AsyncIterator[str]:
        assert self._bridge is not None

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        model = body.get("model", self._config.llm.model)
        created = int(time.time())

        def make_chunk(content: str, finish_reason: str | None = None) -> str:
            delta: dict[str, Any] = {}
            if content:
                delta["content"] = content
            if finish_reason:
                delta["finish_reason"] = finish_reason
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta}],
            }
            return f"data: {json.dumps(chunk)}\n\n"

        try:
            async for chunk_text in self._bridge.agent.stream(user_message, history):
                yield make_chunk(chunk_text)
        except Exception as exc:
            logger.error("Errore streaming: %s", exc)
            yield make_chunk(f"\n[ERRORE] {exc}", finish_reason="stop")
            yield "data: [DONE]\n\n"
            return

        yield make_chunk("", finish_reason="stop")
        yield "data: [DONE]\n\n"

    # ── handler /v1/models ────────────────────────────────────────────────────

    async def _handle_models(self, request: Request) -> JSONResponse:
        assert self._bridge is not None
        model_id = self._config.llm.model
        return JSONResponse({
            "object": "list",
            "data": [
                {
                    "id": model_id,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "mcp-bridge",
                    "description": (
                        f"Modello locale con {len(self._bridge.registry.all_entries())} tool MCP"
                    ),
                }
            ],
        })

    # ── handler /health ───────────────────────────────────────────────────────

    async def _handle_health(self, request: Request) -> JSONResponse:
        connected: list[str] = []
        tool_summary: dict[str, list[str]] = {}
        total_tools = 0

        if self._bridge:
            connected = list(self._bridge.client.get_connections().keys())
            tool_summary = self._bridge.registry.summary()
            total_tools = len(self._bridge.registry.all_entries())

        return JSONResponse({
            "status": "ok",
            "llm_backend": self._config.llm.base_url,
            "llm_model": self._config.llm.model,
            "connected_servers": connected,
            "tools_by_server": tool_summary,
            "total_tools": total_tools,
        })

    # ── helpers ───────────────────────────────────────────────────────────────

    def _parse_messages(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[AgentMessage], str]:
        """
        Divide la lista messaggi in (history, ultimo_messaggio_utente).

        Il system prompt nel body del client ha precedenza su quello
        configurato nel server; se assente, usa quello del server.
        """
        history: list[AgentMessage] = []
        user_message = ""

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "") or ""

            if role == "system":
                # Il system prompt del client sovrascrive quello del server
                # Lo impostiamo sull'agente a runtime
                if self._bridge and self._bridge._agent:
                    self._bridge._agent._system_prompt = content
                continue

            if role == "user":
                user_message = content  # ultimo vince
                history.append(AgentMessage(role="user", content=content))
            elif role == "assistant":
                history.append(AgentMessage(role="assistant", content=content))

        # Rimuovi l'ultimo messaggio utente dalla history
        # (verrà passato come user_message separato ad agent.run)
        if history and history[-1].role == "user":
            history = history[:-1]

        return history, user_message
