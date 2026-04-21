"""
epistylion.server_openai
~~~~~~~~~~~~~~~~~~~~~~~~
Server HTTP **OpenAI-compatible** con tool MCP integrati, logging strutturato
e observability completa.

Flusso::

    LAN client ──POST /v1/chat/completions──► questo server
                                              ├──► LLM locale
                                              └──► tool MCP (stdio)
               ◄── risposta finale ───────────┘

Endpoint
--------
POST /v1/chat/completions   Completions con tool MCP (stream opzionale)
GET  /v1/models             Modello configurato
GET  /v1/tools              Tool MCP disponibili in formato OpenAI
GET  /v1/status             Stato runtime (uptime, contatori, server connessi)
GET  /metrics               Metriche JSON (Prometheus-friendly labels)
GET  /health                Health check (200 = ok, 503 = degraded)
GET  /v1/skills             Returns the list of available skills in the Epistylion system

Variabili d'ambiente
--------------------
EPISTYLION_API_KEY       Se impostata, richiede Authorization: Bearer <key>
                         oppure X-Api-Key: <key>
EPISTYLION_LOG_LEVEL     DEBUG / INFO / WARNING / ERROR  (default: INFO)
EPISTYLION_LOG_FORMAT    json / text                     (default: json)
EPISTYLION_CORS_ORIGINS  Origini CORS separate da virgola (default: *)
EPISTYLION_RATE_LIMIT    Richieste/minuto per IP, 0=disabilitato (default: 0)
"""

from __future__ import annotations

import collections
import json
import logging
import math
import os
import statistics
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from agent import AgentMessage
from epistylion import MCPBridge
from config import BridgeConfig, load_config

# ── logging strutturato ────────────────────────────────────────────────────────

_LOG_FORMAT = os.getenv("EPISTYLION_LOG_FORMAT", "json").lower()
_LOG_LEVEL  = os.getenv("EPISTYLION_LOG_LEVEL", "INFO").upper()


class _JsonFormatter(logging.Formatter):
    """Serializza ogni LogRecord come riga JSON (Loki / ELK / Datadog ready)."""

    KEEP = {"name", "levelname", "message", "exc_info"}

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        doc: dict[str, Any] = {
            "ts":      self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level":   record.levelname,
            "logger":  record.name,
            "msg":     record.message,
        }
        # Campi extra aggiunti con logger.info(..., extra={...})
        for k, v in record.__dict__.items():
            if k not in logging.LogRecord.__dict__ and not k.startswith("_"):
                doc[k] = v
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, ensure_ascii=False, default=str)


def _setup_logging() -> None:
    handler = logging.StreamHandler()
    if _LOG_FORMAT == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s  %(message)s"
        ))
    root = logging.getLogger()
    root.setLevel(getattr(logging, _LOG_LEVEL, logging.INFO))
    root.handlers = [handler]


_setup_logging()
logger = logging.getLogger("epistylion.server")


# ── metriche in memoria ────────────────────────────────────────────────────────

class _Metrics:
    """
    Contatori e istogrammi leggeri, thread-safe tramite asyncio single-thread.
    Tutti i valori sono resettabili; esportati come JSON da /metrics.
    """

    def __init__(self) -> None:
        self.started_at: float = time.time()

        # Contatori
        self.requests_total:       int = 0
        self.requests_ok:          int = 0
        self.requests_error:       int = 0
        self.requests_auth_fail:   int = 0
        self.requests_rate_limit:  int = 0
        self.tool_calls_total:     int = 0
        self.tool_calls_error:     int = 0
        self.stream_requests:      int = 0

        # Latenze (ms) — ultime 1000 osservazioni per calcolo percentili
        self._latencies:    collections.deque[float] = collections.deque(maxlen=1000)
        self._tool_latencies: collections.deque[float] = collections.deque(maxlen=1000)

        # Contatori per path
        self.by_path: dict[str, int] = collections.defaultdict(int)
        # Contatori per tool
        self.by_tool: dict[str, int] = collections.defaultdict(int)

    # ── registrazione ──────────────────────────────────────────────────────────

    def record_request(self, path: str, status: int, latency_ms: float) -> None:
        self.requests_total += 1
        self.by_path[path]  += 1
        self._latencies.append(latency_ms)
        if status < 400:
            self.requests_ok += 1
        else:
            self.requests_error += 1

    def record_tool(self, tool_name: str, latency_ms: float, error: bool = False) -> None:
        self.tool_calls_total  += 1
        self.by_tool[tool_name] += 1
        self._tool_latencies.append(latency_ms)
        if error:
            self.tool_calls_error += 1

    # ── export ─────────────────────────────────────────────────────────────────

    def _percentiles(self, data: collections.deque) -> dict[str, float]:
        if not data:
            return {"p50": 0, "p95": 0, "p99": 0, "min": 0, "max": 0, "mean": 0}
        s = sorted(data)
        def p(pct): return s[min(int(len(s) * pct / 100), len(s) - 1)]
        return {
            "p50":  round(p(50),  2),
            "p95":  round(p(95),  2),
            "p99":  round(p(99),  2),
            "min":  round(s[0],   2),
            "max":  round(s[-1],  2),
            "mean": round(statistics.mean(s), 2),
        }

    def snapshot(self) -> dict[str, Any]:
        uptime = time.time() - self.started_at
        return {
            "uptime_s":            round(uptime, 1),
            "requests_total":      self.requests_total,
            "requests_ok":         self.requests_ok,
            "requests_error":      self.requests_error,
            "requests_auth_fail":  self.requests_auth_fail,
            "requests_rate_limit": self.requests_rate_limit,
            "stream_requests":     self.stream_requests,
            "tool_calls_total":    self.tool_calls_total,
            "tool_calls_error":    self.tool_calls_error,
            "latency_ms":          self._percentiles(self._latencies),
            "tool_latency_ms":     self._percentiles(self._tool_latencies),
            "by_path":             dict(self.by_path),
            "by_tool":             dict(self.by_tool),
        }


_metrics = _Metrics()


# ── middleware ─────────────────────────────────────────────────────────────────

_API_KEY      = os.getenv("EPISTYLION_API_KEY", "")
_CORS_ORIGINS = [o.strip() for o in os.getenv("EPISTYLION_CORS_ORIGINS", "*").split(",")]
_RATE_LIMIT   = int(os.getenv("EPISTYLION_RATE_LIMIT", "0"))   # req/min per IP, 0=off

# sliding window per rate limit: ip → deque di timestamps
_rate_windows: dict[str, collections.deque] = collections.defaultdict(
    lambda: collections.deque()
)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Aggiunge a ogni richiesta:
      - X-Request-Id header (correlation ID)
      - log strutturato di ingresso (request_in)
      - log strutturato di uscita  (request_out) con status e latency
    """

    async def dispatch(self, request: Request, call_next):
        req_id  = str(uuid.uuid4())
        started = time.perf_counter()
        client_ip = (request.headers.get("X-Forwarded-For") or
                     (request.client.host if request.client else "unknown"))

        request.state.request_id = req_id
        request.state.started    = started
        request.state.client_ip  = client_ip

        logger.info(
            "request_in",
            extra={
                "req_id":    req_id,
                "method":    request.method,
                "path":      request.url.path,
                "client_ip": client_ip,
                "ua":        request.headers.get("user-agent", ""),
            },
        )

        response = await call_next(request)

        latency_ms = (time.perf_counter() - started) * 1000
        _metrics.record_request(request.url.path, response.status_code, latency_ms)

        logger.info(
            "request_out",
            extra={
                "req_id":     req_id,
                "method":     request.method,
                "path":       request.url.path,
                "status":     response.status_code,
                "latency_ms": round(latency_ms, 2),
                "client_ip":  client_ip,
            },
        )

        response.headers["X-Request-Id"] = req_id
        return response


class AuthMiddleware(BaseHTTPMiddleware):
    """Valida API key se EPISTYLION_API_KEY è impostata. Bypassa /health e /metrics."""

    SKIP = {"/health", "/metrics"}

    async def dispatch(self, request: Request, call_next):
        if not _API_KEY or request.url.path in self.SKIP:
            return await call_next(request)

        auth   = request.headers.get("Authorization", "")
        x_key  = request.headers.get("X-Api-Key", "")
        token  = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        key_ok = (token == _API_KEY) or (x_key == _API_KEY)

        if not key_ok:
            _metrics.requests_auth_fail += 1
            req_id = getattr(request.state, "request_id", "-")
            logger.warning(
                "auth_fail",
                extra={"req_id": req_id, "path": request.url.path,
                       "client_ip": getattr(request.state, "client_ip", "?")},
            )
            return JSONResponse(
                {"error": {"message": "Unauthorized", "type": "auth_error", "code": 401}},
                status_code=401,
            )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding window rate limit per IP. Disabilitato se EPISTYLION_RATE_LIMIT=0."""

    SKIP = {"/health", "/metrics"}

    async def dispatch(self, request: Request, call_next):
        if not _RATE_LIMIT or request.url.path in self.SKIP:
            return await call_next(request)

        ip  = getattr(request.state, "client_ip", request.client.host if request.client else "?")
        now = time.time()
        win = _rate_windows[ip]

        # rimuovi eventi più vecchi di 60s
        while win and now - win[0] > 60:
            win.popleft()

        if len(win) >= _RATE_LIMIT:
            _metrics.requests_rate_limit += 1
            retry_after = math.ceil(60 - (now - win[0]))
            logger.warning(
                "rate_limit",
                extra={"client_ip": ip, "path": request.url.path,
                       "req_id": getattr(request.state, "request_id", "-")},
            )
            return JSONResponse(
                {"error": {"message": "Too Many Requests", "type": "rate_limit_error", "code": 429}},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        win.append(now)
        return await call_next(request)


class CORSMiddleware(BaseHTTPMiddleware):
    """CORS minimale con origini configurabili via EPISTYLION_CORS_ORIGINS."""

    async def dispatch(self, request: Request, call_next):
        origin  = request.headers.get("origin", "")
        allowed = "*" in _CORS_ORIGINS or origin in _CORS_ORIGINS
        origin_hdr = origin if allowed and origin else "*"

        if request.method == "OPTIONS":
            return Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin":  origin_hdr,
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Api-Key",
                    "Access-Control-Max-Age":       "86400",
                },
            )

        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = origin_hdr
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Api-Key"
        return response


# ── server principale ──────────────────────────────────────────────────────────

class OpenAIProxyServer:
    """
    Server HTTP OpenAI-compatible con observability completa.

    Parameters
    ----------
    config : BridgeConfig
    system_prompt : str
        System prompt di default (sovrascrivibile dal client via role=system).
    max_steps : int
        Limite iterazioni tool-call per richiesta.
    expose_tool_calls : bool
        Aggiunge ``_mcp_tool_calls`` e ``_mcp_steps`` nella risposta JSON.
    """

    def __init__(
        self,
        config: BridgeConfig,
        system_prompt: str = "Sei un assistente utile con accesso a vari tool MCP.",
        max_steps: int = 20,
        expose_tool_calls: bool = False,
    ) -> None:
        self._config           = config
        self._system_prompt    = system_prompt
        self._max_steps        = max_steps
        self._expose_tool_calls = expose_tool_calls
        self._bridge: MCPBridge | None = None
        # Runtime-mutable model: updated via PATCH /v1/config or per-request body
        self._current_model: str = config.llm.model
        # Server-side disabled tool set: kept in sync via PATCH /v1/tools/state
        self._disabled_tools: set[str] = set()

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

    async def run(self, host: str = "0.0.0.0", port: int = 8081) -> None:
        """Connette i server MCP e avvia il server HTTP."""
        import uvicorn

        logger.info(
            "server_starting",
            extra={
                "host": host, "port": port,
                "log_format": _LOG_FORMAT, "log_level": _LOG_LEVEL,
                "auth_enabled": bool(_API_KEY),
                "rate_limit": _RATE_LIMIT,
                "cors_origins": _CORS_ORIGINS,
            },
        )

        self._bridge = MCPBridge(self._config)
        errors = await self._bridge.connect(
            system_prompt=self._system_prompt,
            max_steps=self._max_steps,
        )

        tool_count = len(self._bridge.registry.all_entries())
        logger.info(
            "bridge_ready",
            extra={
                "connected_servers": list(self._bridge.client.get_connections().keys()),
                "failed_servers":    list(errors.keys()),
                "total_tools":       tool_count,
            },
        )

        # Aggancia callback tool-call per logging + metriche
        self._bridge.agent._on_step = self._on_tool_step  # type: ignore

        app = self._build_app()
        cfg = uvicorn.Config(
            app, host=host, port=port,
            log_config=None,   # disabilitiamo il logger uvicorn, usiamo il nostro
            access_log=False,
            reload=True,
        )
        server = uvicorn.Server(cfg)
        try:
            await server.serve()
        finally:
            logger.info("server_stopping")
            if self._bridge:
                await self._bridge.disconnect()

    # ── Starlette app ─────────────────────────────────────────────────────────

    def _build_app(self) -> Starlette:
        return Starlette(
            middleware=[
                Middleware(CORSMiddleware),
                Middleware(RequestLoggingMiddleware),
                Middleware(AuthMiddleware),
                Middleware(RateLimitMiddleware),
            ],
            routes=[
                Route("/v1/chat/completions", self._handle_completions, methods=["POST", "OPTIONS"]),
                Route("/v1/models",           self._handle_models,      methods=["GET"]),
                Route("/v1/skills",           self._handle_skills,      methods=["GET"]),
                Route("/v1/tools",            self._handle_tools,       methods=["GET"]),
                Route("/v1/tools/state",      self._handle_tools_state, methods=["GET", "PATCH", "OPTIONS"]),
                Route("/v1/config",           self._handle_config,      methods=["PATCH", "OPTIONS"]),
                Route("/v1/status",           self._handle_status,      methods=["GET"]),
                Route("/metrics",             self._handle_metrics,     methods=["GET"]),
                Route("/health",              self._handle_health,      methods=["GET"]),
            ],
        )

    # ── handler: /v1/chat/completions ─────────────────────────────────────────

    async def _handle_completions(self, request: Request) -> Response:
        req_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            logger.warning("bad_json", extra={"req_id": req_id})
            return _err(400, "Body JSON non valido", "invalid_request_error")

        messages: list[dict[str, Any]] = body.get("messages", [])
        stream:   bool                 = bool(body.get("stream", False))
        model     = body.get("model") or self._current_model
        skill     = body.get("skill")  # skill da iniettare nel system prompt
        # Per-request disabled tools merged with server-side set
        req_disabled: set[str] = set(body.get("disabled_tools") or [])
        effective_disabled = self._disabled_tools | req_disabled

        # Persist model override so subsequent requests and /v1/status reflect it
        if model and model != self._current_model:
            self._current_model = model
            self._config.llm.model = model
            if self._bridge:
                self._bridge._config.llm.model = model

        history, user_message = self._parse_messages(messages, req_id)

        if not user_message:
            return _err(400, "Nessun messaggio utente trovato", "invalid_request_error")

        logger.info(
            "completion_start",
            extra={
                "req_id":  req_id,
                "model":   model,
                "stream":  stream,
                "history_turns": len(history),
                "user_msg_len":  len(user_message),
                "skill":    skill,
            },
        )

        if stream:
            _metrics.stream_requests += 1
            return StreamingResponse(
                self._stream_completion(user_message, history, body, req_id, skill, effective_disabled),
                media_type="text/event-stream",
                headers={
                    "Cache-Control":     "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        return await self._blocking_completion(user_message, history, body, req_id, skill, effective_disabled)

    # ── completions blocking ──────────────────────────────────────────────────

    async def _blocking_completion(
        self,
        user_message: str,
        history:      list[AgentMessage],
        body:         dict[str, Any],
        req_id:       str,
        skill:        str | None = None,
        disabled_tools: set[str] | None = None,
    ) -> JSONResponse:
        assert self._bridge is not None
        t0 = time.perf_counter()

        try:
            # TODO: pass disabled_tools to agent.run() once agent.py supports the parameter
            # e.g.: response = await self._bridge.agent.run(user_message, history, skill=skill, disabled_tools=disabled_tools)
            response = await self._bridge.agent.run(user_message, history, skill=skill)
        except Exception as exc:
            logger.error(
                "agent_error",
                extra={"req_id": req_id, "error": str(exc),
                       "traceback": traceback.format_exc()},
            )
            return _err(500, str(exc), "internal_error")

        latency_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "completion_done",
            extra={
                "req_id":        req_id,
                "steps":         response.steps,
                "tool_calls":    len(response.tool_calls_made),
                "reply_len":     len(response.final_message),
                "latency_ms":    round(latency_ms, 2),
            },
        )

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        result: dict[str, Any] = {
            "id":      completion_id,
            "object":  "chat.completion",
            "created": int(time.time()),
            "model":   body.get("model", self._config.llm.model),
            "choices": [{
                "index":   0,
                "message": {"role": "assistant", "content": response.final_message},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens":     -1,
                "completion_tokens": -1,
                "total_tokens":      -1,
            },
        }
        if self._expose_tool_calls:
            result["_mcp_tool_calls"] = response.tool_calls_made
            result["_mcp_steps"]      = response.steps
            result["_latency_ms"]     = round(latency_ms, 2)

        return JSONResponse(result)

    # ── completions streaming ─────────────────────────────────────────────────

    async def _stream_completion(
        self,
        user_message: str,
        history:      list[AgentMessage],
        body:         dict[str, Any],
        req_id:       str,
        skill:        str | None = None,
        disabled_tools: set[str] | None = None,
    ) -> AsyncIterator[str]:
        assert self._bridge is not None

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        model   = body.get("model", self._config.llm.model)
        created = int(time.time())
        t0      = time.perf_counter()
        chunks  = 0

        def _chunk(content: str = "", finish_reason: str | None = None) -> str:
            delta: dict[str, Any] = {}
            if content:
                delta["content"] = content
            if finish_reason:
                delta["finish_reason"] = finish_reason
            return "data: " + json.dumps({
                "id":      completion_id,
                "object":  "chat.completion.chunk",
                "created": created,
                "model":   model,
                "choices": [{"index": 0, "delta": delta}],
            }) + "\n\n"

        try:
            async for text in self._bridge.agent.stream(user_message, history, skill=skill):
                chunks += 1
                yield _chunk(text)
        except Exception as exc:
            logger.error(
                "stream_error",
                extra={"req_id": req_id, "error": str(exc),
                       "traceback": traceback.format_exc()},
            )
            yield _chunk(f"\n[ERRORE] {exc}", finish_reason="stop")
            yield "data: [DONE]\n\n"
            return

        latency_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "stream_done",
            extra={
                "req_id":     req_id,
                "chunks":     chunks,
                "latency_ms": round(latency_ms, 2),
            },
        )
        yield _chunk(finish_reason="stop")
        yield "data: [DONE]\n\n"

    # ── handler: /v1/models ───────────────────────────────────────────────────

    async def _handle_models(self, request: Request) -> JSONResponse:
        assert self._bridge is not None
        return JSONResponse({
            "object": "list",
            "data": [{
                "id":          self._config.llm.model,
                "object":      "model",
                "created":     int(time.time()),
                "owned_by":    "epistylion",
                "description": f"LLM locale con {len(self._bridge.registry.all_entries())} tool MCP",
            }],
        })

    # ── handler: /v1/tools ────────────────────────────────────────────────────

    async def _handle_tools(self, request: Request) -> JSONResponse:
        """Lista tutti i tool MCP nel formato OpenAI function-calling."""
        assert self._bridge is not None
        qualified = request.query_params.get("qualified", "false").lower() == "true"
        tools  = self._bridge.get_openai_tools(use_qualified_names=qualified)
        summary = self._bridge.registry.summary()
        return JSONResponse({
            "object":     "list",
            "count":      len(tools),
            "by_server":  summary,
            "tools":      tools,
        })

    # ── handler: /v1/skills ─────────────────────────────────────────────────

    async def _handle_skills(self, request: Request) -> JSONResponse:
        """Restituisce la lista delle skills disponibili."""
        assert self._bridge is not None
        skills = self._bridge._skill_registry.list()
        return JSONResponse({
            "skills": skills,
        })

    # ── handler: PATCH /v1/config ────────────────────────────────────────────

    async def _handle_config(self, request: Request) -> JSONResponse:
        """
        Aggiorna la configurazione runtime senza riavvio.

        Body JSON accettato::

            { "model": "new-model-id" }

        Restituisce la configurazione aggiornata.
        """
        if request.method == "OPTIONS":
            return Response(status_code=204)
        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            return _err(400, "Body JSON non valido", "invalid_request_error")

        updated: dict[str, Any] = {}

        if "model" in body:
            new_model = str(body["model"]).strip()
            if new_model:
                self._current_model = new_model
                self._config.llm.model = new_model
                if self._bridge:
                    self._bridge._config.llm.model = new_model
                updated["model"] = new_model
                logger.info("config_model_updated", extra={"model": new_model})

        return JSONResponse({"updated": updated, "current_model": self._current_model})

    # ── handler: GET + PATCH /v1/tools/state ────────────────────────────────

    async def _handle_tools_state(self, request: Request) -> JSONResponse:
        """
        GET  → restituisce la lista dei tool disabilitati lato server.
        PATCH → aggiorna la lista; body: ``{"disabled": ["tool_a", "tool_b"]}``

        Il client può sincronizzare il proprio stato localStorage con questo endpoint.
        """
        if request.method == "OPTIONS":
            return Response(status_code=204)

        if request.method == "PATCH":
            try:
                body: dict[str, Any] = await request.json()
            except Exception:
                return _err(400, "Body JSON non valido", "invalid_request_error")
            disabled = body.get("disabled", [])
            if not isinstance(disabled, list):
                return _err(400, "'disabled' deve essere una lista di stringhe", "invalid_request_error")
            self._disabled_tools = set(str(t) for t in disabled)
            logger.info("tools_state_updated", extra={"disabled_count": len(self._disabled_tools)})

        return JSONResponse({
            "disabled": sorted(self._disabled_tools),
            "disabled_count": len(self._disabled_tools),
        })

    # ── handler: /v1/status ─────────────────────────────────────────────────

    async def _handle_status(self, request: Request) -> JSONResponse:
        assert self._bridge is not None
        connections = self._bridge.client.get_connections()
        servers_detail = {}
        for name, conn in connections.items():
            servers_detail[name] = {
                "connected": conn.is_connected,
                "tools":     [t.name for t in conn.tools],
                "tool_count": len(conn.tools),
            }

        return JSONResponse({
            "status":          "ok",
            "uptime_s":        round(time.time() - _metrics.started_at, 1),
            "llm_backend":     self._config.llm.base_url,
            "llm_model":       self._current_model,
            "max_steps":       self._max_steps,
            "auth_enabled":    bool(_API_KEY),
            "rate_limit_rpm":  _RATE_LIMIT,
            "cors_origins":    _CORS_ORIGINS,
            "log_format":      _LOG_FORMAT,
            "log_level":       _LOG_LEVEL,
            "servers":         servers_detail,
            "total_tools":     len(self._bridge.registry.all_entries()),
        })

    # ── handler: /metrics ─────────────────────────────────────────────────────

    async def _handle_metrics(self, request: Request) -> JSONResponse:
        """
        Snapshot JSON delle metriche interne.
        Compatibile con qualsiasi scraper (Prometheus via json_exporter,
        Grafana, Datadog, ecc.).
        """
        return JSONResponse(_metrics.snapshot())

    # ── handler: /health ──────────────────────────────────────────────────────

    async def _handle_health(self, request: Request) -> JSONResponse:
        """
        200 → tutti i server MCP connessi (o nessuno configurato)
        503 → almeno un server ha perso la connessione
        """
        if not self._bridge:
            return JSONResponse({"status": "starting"}, status_code=503)

        connections = self._bridge.client.get_connections()
        total       = len(self._config.servers)
        connected   = sum(1 for c in connections.values() if c.is_connected)
        degraded    = total > 0 and connected < total
        status_code = 503 if degraded else 200

        return JSONResponse(
            {
                "status":           "degraded" if degraded else "ok",
                "servers_total":    total,
                "servers_connected": connected,
                "total_tools":      len(self._bridge.registry.all_entries()),
                "uptime_s":         round(time.time() - _metrics.started_at, 1),
            },
            status_code=status_code,
        )

    # ── callback tool step ────────────────────────────────────────────────────

    def _on_tool_step(self, step: int, tool_name: str, result: str) -> None:
        """
        Chiamata da MCPAgent dopo ogni tool call.
        Logga un evento strutturato e aggiorna le metriche.
        Nota: la latenza reale viene misurata in agent.py; qui stimiamo
        dall'esterno usando il timestamp.
        """
        _metrics.record_tool(tool_name, latency_ms=0)   # latency calcolata in agent.py
        logger.info(
            "tool_call",
            extra={
                "step":          step,
                "tool":          tool_name,
                "result_len":    len(result),
                "result_excerpt": result[:200],
            },
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _parse_messages(
        self,
        messages: list[dict[str, Any]],
        req_id:   str = "-",
    ) -> tuple[list[AgentMessage], str]:
        """Separa history e ultimo messaggio utente. Logga il system prompt se presente."""
        history: list[AgentMessage] = []
        user_message = ""

        for msg in messages:
            role    = msg.get("role", "")
            content = msg.get("content", "") or ""

            if role == "system":
                logger.debug(
                    "system_prompt_override",
                    extra={"req_id": req_id, "length": len(content)},
                )
                if self._bridge and self._bridge._agent:
                    self._bridge._agent._base_system_prompt = content
                continue

            if role == "user":
                user_message = content
                history.append(AgentMessage(role="user", content=content))
            elif role == "assistant":
                history.append(AgentMessage(role="assistant", content=content))

        if history and history[-1].role == "user":
            history = history[:-1]

        return history, user_message


# ── helpers privati ────────────────────────────────────────────────────────────

def _err(status: int, message: str, err_type: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": err_type, "code": status}},
        status_code=status,
    )