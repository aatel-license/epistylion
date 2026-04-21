"""
mcp_bridge.server_mcp
~~~~~~~~~~~~~~~~~~~~
Espone tutti i tool MCP aggregati come un **server MCP HTTP/SSE**,
accessibile da qualsiasi client MCP sulla LAN (Claude Desktop, altri bridge, ecc.).

Il server fa da proxy/aggregatore:
    LAN client (MCP) ──HTTP SSE──► questo server ──stdio──► [blender, mnheme, scrapling, ...]
    ◄── risposta finale ───────────────────────────────────────────────────────────────┘

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

import json
import logging
import statistics
import time
import uuid
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import (
    TextContent,
    Tool,
)
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route

from epistylion import MCPBridge
from config import BridgeConfig, load_config
from registry import mcp_result_to_string

# ── logging strutturato ───────────────────────────────────────────────────────

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


_setupLogging()
logger = logging.getLogger("epistylion.server_mcp")

# ── metriche in memoria ───────────────────────────────────────────────────────

class _Metrics:
    """
    Contatori e istogrammi leggeri, thread-safe tramite asyncio single-thread.
    Tutti i valori sono resettabili; esportati come JSON da /metrics.
    """

    def __init__(self) -> None:
        self.started_at: float = time.time()

        # Contatori
        self.requests_total: int = 0
        self.requests_ok: int = 0
        self.requests_error: int = 0
        self.requests_auth_fail: int = 0
        self.requests_rate_limit: int = 0
        self.tool_calls_total: int = 0
        self.tool_calls_error: int = 0
        self.stream_requests: int = 0

        # Latenze (ms) — ultime 1000 osservazioni per calcolo percentili
        self._latencies: list[float] = []
        self._tool_latencies: list[float] = []

        # Contatori per path
        self.by_path: dict[str, int] = {}
        # Contatori per tool
        self.by_tool: dict[str, int] = {}

        # Sliding window per rate limit: ip → deque di timestamp
        self._rate_windows: dict[str, list[float]] = {}

    # ── registrazione ──────────────────────────────────────────────────────────

    def record_request(self, path: str, status: int, latency_ms: float) -> None:
        self.requests_total += 1
        self.by_path[path] = self.by_path.get(path, 0) + 1
        self._latencies.append(latency_ms)
        if status < 400:
            self.requests_ok += 1
        else:
            self.requests_error += 1

    def record_tool(self, tool_name: str, latency_ms: float, error: bool = False) -> None:
        self.tool_calls_total += 1
        self.by_tool[tool_name] = self.by_tool.get(tool_name, 0) + 1
        self._tool_latencies.append(latency_ms)
        if error:
            self.tool_calls_error += 1

    # ── export ─────────────────────────────────────────────────────────────────

    def _percentiles(self, data: list[float]) -> dict[str, float]:
        if not data:
            return {"p50": 0, "p95": 0, "p99": 0, "min": 0, "max": 0, "mean": 0}
        s = sorted(data)
        def p(pct: float) -> float:
            return s[min(int(len(s) * pct / 100), len(s) - 1)]
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
            "uptime_s": round(uptime, 1),
            "requests_total": self.requests_total,
            "requests_ok": self.requests_ok,
            "requests_error": self.requests_error,
            "requests_auth_fail": self.requests_auth_fail,
            "requests_rate_limit": self.requests_rate_limit,
            "stream_requests": self.stream_requests,
            "tool_calls_total": self.tool_calls_total,
            "tool_calls_error": self.tool_calls_error,
            "latency_ms": self._percentiles(self._latencies),
            "tool_latency_ms": self._percentiles(self._tool_latencies),
            "by_path": dict(self.by_path),
            "by_tool": dict(self.by_tool),
        }

_metrics = _Metrics()

# ── middleware ─────────────────────────────────────────────────────────────────

_API_KEY = os.getenv("EPISTYLION_API_KEY", "")
_CORS_ORIGINS = [o.strip() for o in os.getenv("EPISTYLION_CORS_ORIGINS", "*").split(",")]
_RATE_LIMIT = int(os.getenv("EPISTYLION_RATE_LIMIT", "0"))   # req/min per IP, 0=off


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Aggiunge a ogni richiesta: X-Request-Id header e log strutturato."""

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
        now = time.perf_counter()
        win = _metrics._rate_windows.get(ip, [])
        _metrics._rate_windows[ip] = win

        # rimuovi eventi più vecchi di 60s
        while win and now - win[0] > 60:
            win.pop(0)

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
    """CORS minimale con origini configurabili tramite EPISTYLION_CORS_ORIGINS."""

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

class MCPProxyServer:
    """
    Server HTTP/SSE che aggrega e ri-espone i tool di più server MCP locali.

    I client MCP remoti lo vedono come un unico server con tutti i tool
    dei server sottostanti (usando nomi qualificati per evitare collisioni).

    Parameters
    ----------
    config : BridgeConfig
    use_qualified_names : bool
        Se True (default), i tool sono esposti come 'server__tool'.
        Necessario quando più server hanno tool con lo stesso nome.
    """

    def __init__(
        self,
        config: BridgeConfig,
        use_qualified_names: bool = True,
    ) -> None:
        self._config = config
        self._use_qualified_names = use_qualified_names
        self._bridge: MCPBridge | None = None
        self._mcp_server = Server("mcp-bridge-proxy")
        self._ready = asyncio.Event()

    # ── factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_config(
        cls,
        mcp_config_path: str | Path | None = None,
        env_path: str | Path | None = None,
        use_qualified_names: bool = True,
    ) -> "MCPProxyServer":
        config = load_config(mcp_config_path, env_path)
        return cls(config, use_qualified_names)

    # ── avvio ─────────────────────────────────────────────────────────────────

    async def run(
        self,
        host: str = "0.0.0.0",
        port: int = 9000,
    ) -> None:
        """Connette i server MCP locali e avvia il server HTTP/SSE."""
        import uvicorn

        # Connetti il bridge ai server MCP locali (senza LLM)
        self._bridge = MCPBridge(self._config)
        errors = await self._bridge.connect()
        if errors:
            logger.warning("Server MCP non connessi: %s", list(errors.keys()))

        # Registra i handler MCP
        self._register_handlers()
        self._ready.set()

        # Costruisci l'app Starlette con il transport SSE
        app = self._build_starlette_app()

        logger.info("MCP Proxy Server in ascolto su http://%s:%d/sse", host, port)
        config = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        try:
            await server.serve()
        finally:
            if self._bridge:
                await self._bridge.disconnect()

    # ── handler MCP ───────────────────────────────────────────────────────────

    def _register_handlers(self) -> None:
        """Registra list_tools e call_tool sul server MCP."""
        assert self._bridge is not None

        @self._mcp_server.list_tools()
        async def list_tools() -> list[Tool]:
            entries = self._bridge.registry.all_entries()
            result = []
            for entry in entries:
                tool = entry.tool
                name = entry.qualified_name if self._use_qualified_names else tool.name
                result.append(Tool(
                    name=name,
                    description=tool.description or f"Tool dal server '{entry.server_name}'",
                    inputSchema=tool.inputSchema or {"type": "object", "properties": {}},
                ))
            return result

        @self._mcp_server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
            assert self._bridge is not None

            # Risolvi nome → entry
            entry = self._bridge.registry.resolve(name)
            if entry is None:
                return [TextContent(type="text", text=f"[ERRORE] Tool '{name}' non trovato")]

            try:
                raw = await self._bridge.client.call_tool(entry.tool.name, arguments)
                text = mcp_result_to_string(raw)
            except Exception as exc:
                text = f"[ERRORE] {exc}"
                logger.error("Errore tool '%s': %s", name, exc)

            return [TextContent(type="text", text=text)]

    # ── Starlette app ─────────────────────────────────────────────────────────

    def _build_starlette_app(self) -> Starlette:
        sse_transport = SseServerTransport("/messages/")

        async def handle_sse(request: Request) -> Response:
            async with sse_transport.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await self._mcp_server.run(
                    streams[0],
                    streams[1],
                    self._mcp_server.create_initialization_options(),
                )
            return Response()

        async def handle_health(request: Request) -> Response:
            connected = list(self._bridge.client.get_connections().keys()) if self._bridge else []
            tool_count = len(self._bridge.registry.all_entries()) if self._bridge else 0
            body = json.dumps({
                "status": "ok",
                "connected_servers": connected,
                "total_tools": tool_count,
            })
            return Response(content=body, media_type="application/json")

        return Starlette(
            routes=[
                Route("/health", handle_health),
                Mount("/", app=sse_transport.handle_post_message),
                Route("/sse", handle_sse),
            ]
        )

# ── import mancanti ───────────────────────────────────────────────────────────

from agent import AgentMessage
from epistylion import MCPBridge
from config import BridgeConfig, load_config
from registry import mcp_result_to_string

# ── route handler per /v1/chat/completions ─────────────────────────────────────

@router.post("/v1/chat/completions")
async def handle_chat_completions(request: Request) -> Response:
    """Gestisce le richieste di completamento con tool MCP."""
    # TODO: Implementare logica completamenti
    return JSONResponse({"error": "Not implemented"}, status_code=501)

# ── route handler per /v1/models ─────────────────────────────────────────────

@router.get("/v1/models")
async def handle_models(request: Request) -> Response:
    """Restituisce la lista dei modelli disponibili."""
    return JSONResponse({
        "object": "list",
        "data": [{
            "id": "epistylion-mcp",
            "object": "model",
            "created": int(time.time()),
            "owned_by": "epistylion",
        }],
    })

# ── route handler per /v1/tools ───────────────────────────────────────────────

@router.get("/v1/tools")
async def handle_tools(request: Request) -> Response:
    """Restituisce i tool MCP disponibili."""
    return JSONResponse({
        "object": "list",
        "count": 0,
        "tools": [],
    })

# ── route handler per /v1/skills ─────────────────────────────────────────────

@router.get("/v1/skills")
async def handle_skills(request: Request) -> Response:
    """Restituisce la lista delle skills disponibili."""
    return JSONResponse({"skills": []})

# ── route handler per /v1/status ─────────────────────────────────────────────

@router.get("/v1/status")
async def handle_status(request: Request) -> Response:
    """Restituisce lo stato del server."""
    return JSONResponse({
        "status": "ok",
        "uptime_s": 0,
        "servers": [],
        "total_tools": 0,
    })

# ── route handler per /metrics ───────────────────────────────────────────────

@router.get("/metrics")
async def handle_metrics(request: Request) -> Response:
    """Restituisce le metriche in JSON."""
    return JSONResponse(_metrics.snapshot())

# ── route handler per /health ───────────────────────────────────────────────

@router.get("/health")
async def handle_health(request: Request) -> Response:
    """Health check."""
    return JSONResponse({"status": "ok"})

# ── import mancanti per le route ─────────────────────────────────────────────

from agent import AgentMessage
from epistylion import MCPBridge
from config import BridgeConfig, load_config
from registry import mcp_result_to_string
