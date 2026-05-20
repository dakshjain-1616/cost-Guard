"""FastAPI proxy server with OpenAI-compatible endpoints."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from costguard.alerts import get_alert_manager, get_structured_logger
from costguard.circuit_breaker import CircuitBreakerError, get_circuit_breaker_manager
from costguard.database import get_database
from costguard.models import (
    CostEstimateRequest,
    Provider,
    ProxyRequest,
    SpendRecord,
)
from costguard.pricing import get_pricing_manager
from costguard.safe_mode import SafeModeManager


class ProxyServer:
    """FastAPI proxy server for CostGuard."""

    def __init__(
        self,
        openai_api_key: str | None = None,
        anthropic_api_key: str | None = None,
        openrouter_api_key: str | None = None,
        base_url: str = "http://localhost:8000",
    ) -> None:
        """Initialize proxy server.

        Args:
            openai_api_key: OpenAI API key.
            anthropic_api_key: Anthropic API key.
            openrouter_api_key: OpenRouter API key.
            base_url: Base URL for the proxy server.
        """
        self.openai_api_key = openai_api_key
        self.anthropic_api_key = anthropic_api_key
        self.openrouter_api_key = openrouter_api_key
        self.base_url = base_url

        # Initialize managers
        self.pricing_manager = get_pricing_manager()
        self.circuit_breaker_manager = get_circuit_breaker_manager()
        self.safe_mode_manager = SafeModeManager()
        self.alert_manager = get_alert_manager()
        self.logger = get_structured_logger()

        # HTTP client for forwarding requests
        self._client: httpx.AsyncClient | None = None

        # WebSocket connections for dashboard
        self._websocket_connections: dict[str, WebSocket] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    @asynccontextmanager
    async def lifespan(self, app: FastAPI) -> AsyncGenerator[None, None]:
        """Application lifespan handler."""
        # Startup
        await get_database().initialize_schema()
        yield
        # Shutdown
        if self._client:
            await self._client.aclose()
        await self.alert_manager.close()

    def create_app(self) -> FastAPI:
        """Create FastAPI application."""
        app = FastAPI(
            title="CostGuard",
            description="Real-Time AI Spend Circuit Breaker",
            version="1.0.0",
            lifespan=self.lifespan,
        )

        # Register routes
        self._register_routes(app)

        return app

    def _register_routes(self, app: FastAPI) -> None:
        """Register API routes."""

        @app.get("/health")
        async def health_check() -> dict[str, str]:
            """Health check endpoint."""
            return {"status": "healthy", "service": "costguard"}

        @app.get("/v1/models")
        async def list_models(
            authorization: str | None = Header(None),
        ) -> dict[str, Any]:
            """List available models with pricing."""
            models = self.pricing_manager.list_available_models()
            return {
                "object": "list",
                "data": [
                    {
                        "id": m.model_id,
                        "object": "model",
                        "created": int(m.effective_date.timestamp()),
                        "owned_by": m.provider.value,
                        "pricing": {
                            "input": str(m.input_price_per_mtok),
                            "output": str(m.output_price_per_mtok),
                        },
                    }
                    for m in models
                ],
            }

        @app.post("/v1/chat/completions")
        async def chat_completions(
            request: Request,
            body: dict[str, Any],
        ) -> JSONResponse | StreamingResponse:
            """OpenAI-compatible chat completions endpoint."""
            request_id = str(uuid.uuid4())
            session_id = request.headers.get("x-session-id", "default")
            project_id = request.headers.get("x-project-id", "default")

            # Parse request
            try:
                proxy_request = ProxyRequest(**body)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid request: {e}") from e

            # Get circuit breaker
            breaker = await self.circuit_breaker_manager.get_breaker(
                session_id=session_id,
                project_id=project_id,
            )

            # Estimate cost
            cost_estimate = self.pricing_manager.estimate_cost(
                CostEstimateRequest(
                    provider=self._detect_provider(proxy_request.model),
                    model_id=proxy_request.model,
                    messages=proxy_request.messages,
                    estimated_output_tokens=proxy_request.max_tokens or 1000,
                )
            )

            if cost_estimate is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown model: {proxy_request.model}",
                )

            # Check circuit breaker
            try:
                await breaker.check_limits(
                    estimated_cost=cost_estimate.estimated_cost,
                    request_id=request_id,
                )
            except CircuitBreakerError as e:
                self.logger.log_request_blocked(
                    request_id=request_id,
                    session_id=session_id,
                    model_id=proxy_request.model,
                    reason=e.error.message,
                    exceeded_limits=[limit.value for limit in e.error.exceeded_limits],
                )
                return JSONResponse(
                    status_code=429,
                    content=e.error.model_dump(),
                )

            # Check safe mode
            safe_mode_result = await self.safe_mode_manager.check_request(
                request_id=request_id,
                session_id=session_id,
                estimated_cost=cost_estimate.estimated_cost,
            )

            if safe_mode_result.requires_confirmation and not safe_mode_result.confirmed:
                # Return safe mode response requiring confirmation
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "type": "safe_mode_triggered",
                            "message": f"Request requires confirmation: estimated cost ${cost_estimate.estimated_cost}",
                            "estimated_cost": str(cost_estimate.estimated_cost),
                            "threshold": str(safe_mode_result.threshold),
                            "request_id": request_id,
                        }
                    },
                )

            # Create spend record
            spend_record = SpendRecord(
                request_id=request_id,
                session_id=session_id,
                project_id=project_id,
                provider=self._detect_provider(proxy_request.model),
                model_id=proxy_request.model,
                input_tokens=cost_estimate.estimated_input_tokens,
                output_tokens=cost_estimate.estimated_output_tokens,
                estimated_cost=cost_estimate.estimated_cost,
            )

            self.logger.log_request_start(
                request_id=request_id,
                session_id=session_id,
                model_id=proxy_request.model,
                estimated_cost=str(cost_estimate.estimated_cost),
            )

            # Forward request to provider
            start_time = time.time()

            try:
                response = await self._forward_request(
                    proxy_request=proxy_request,
                    request_id=request_id,
                )

                duration_ms = (time.time() - start_time) * 1000

                # Extract actual usage if available
                actual_cost = None
                if response.get("usage"):
                    usage = response["usage"]
                    actual_cost = self.pricing_manager.calculate_actual_cost(
                        model_id=proxy_request.model,
                        input_tokens=usage.get("prompt_tokens", 0),
                        output_tokens=usage.get("completion_tokens", 0),
                    )

                # Record spend
                await breaker.record_spend(spend_record, actual_cost)

                self.logger.log_request_complete(
                    request_id=request_id,
                    session_id=session_id,
                    model_id=proxy_request.model,
                    actual_cost=str(actual_cost) if actual_cost else str(cost_estimate.estimated_cost),
                    duration_ms=duration_ms,
                )

                # Add CostGuard metadata
                response["costguard_metadata"] = {
                    "request_id": request_id,
                    "estimated_cost": str(cost_estimate.estimated_cost),
                    "actual_cost": str(actual_cost) if actual_cost else None,
                    "session_id": session_id,
                    "project_id": project_id,
                }

                return JSONResponse(content=response)

            except Exception as e:
                await breaker.record_failed(spend_record, str(e))
                self.logger.log_request_failed(
                    request_id=request_id,
                    session_id=session_id,
                    model_id=proxy_request.model,
                    error=str(e),
                )
                raise HTTPException(status_code=502, detail=f"Provider error: {e}") from e

        @app.post("/v1/estimate")
        async def estimate_cost(
            request: Request,
            body: dict[str, Any],
        ) -> JSONResponse:
            """Estimate cost for a request without making the call."""
            try:
                estimate_request = CostEstimateRequest(**body)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid request: {e}") from e

            estimate = self.pricing_manager.estimate_cost(estimate_request)

            if estimate is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown model: {estimate_request.model_id}",
                )

            return JSONResponse(content=estimate.model_dump())

        @app.get("/v1/status/{session_id}")
        async def get_status(
            session_id: str,
            project_id: str = "default",
        ) -> JSONResponse:
            """Get circuit breaker status for a session."""
            breaker = await self.circuit_breaker_manager.get_breaker(
                session_id=session_id,
                project_id=project_id,
            )
            state = breaker.get_state()

            if state is None:
                raise HTTPException(status_code=404, detail="Session not found")

            return JSONResponse(content=state.model_dump())

        @app.websocket("/v1/dashboard/ws")
        async def dashboard_websocket(websocket: WebSocket) -> None:
            """WebSocket endpoint for real-time dashboard."""
            await websocket.accept()

            session_id = websocket.query_params.get("session_id", "default")
            project_id = websocket.query_params.get("project_id", "default")
            client_id = str(uuid.uuid4())

            self._websocket_connections[client_id] = websocket
            self.logger.log_dashboard_connect(session_id, client_id)

            try:
                while True:
                    # Get current status
                    breaker = await self.circuit_breaker_manager.get_breaker(
                        session_id=session_id,
                        project_id=project_id,
                    )
                    state = breaker.get_state()

                    if state:
                        await websocket.send_json(state.model_dump())

                    # Wait for next update or message
                    try:
                        message = await asyncio.wait_for(
                            websocket.receive_text(),
                            timeout=5.0,
                        )
                        # Handle client messages
                        if message == "ping":
                            await websocket.send_text("pong")
                    except asyncio.TimeoutError:
                        # Normal timeout, continue loop
                        pass

            except WebSocketDisconnect:
                pass
            finally:
                del self._websocket_connections[client_id]
                self.logger.log_dashboard_disconnect(session_id, client_id)

        @app.post("/v1/safe-mode/confirm")
        async def confirm_safe_mode(
            request: Request,
            body: dict[str, Any],
        ) -> JSONResponse:
            """Confirm a safe mode request."""
            request_id = body.get("request_id")
            session_id = body.get("session_id", "default")
            confirmed = body.get("confirmed", False)

            if not request_id:
                raise HTTPException(status_code=400, detail="request_id required")

            result = await self.safe_mode_manager.confirm_request(
                request_id=request_id,
                session_id=session_id,
                confirmed=confirmed,
            )

            return JSONResponse(content=result.to_dict())

    def _detect_provider(self, model_id: str) -> Provider:
        """Detect provider from model ID."""
        if model_id.startswith("claude-") or "anthropic" in model_id.lower():
            return Provider.ANTHROPIC
        elif "openrouter" in model_id.lower() or "/" in model_id:
            return Provider.OPENROUTER
        else:
            return Provider.OPENAI

    async def _forward_request(
        self,
        proxy_request: ProxyRequest,
        request_id: str,
    ) -> dict[str, Any]:
        """Forward request to appropriate provider."""
        provider = self._detect_provider(proxy_request.model)
        client = await self._get_client()

        if provider == Provider.OPENAI:
            return await self._forward_to_openai(client, proxy_request, request_id)
        elif provider == Provider.ANTHROPIC:
            return await self._forward_to_anthropic(client, proxy_request, request_id)
        elif provider == Provider.OPENROUTER:
            return await self._forward_to_openrouter(client, proxy_request, request_id)
        else:
            raise ValueError(f"Unsupported provider: {provider}")

    async def _forward_to_openai(
        self,
        client: httpx.AsyncClient,
        request: ProxyRequest,
        request_id: str,
    ) -> dict[str, Any]:
        """Forward request to OpenAI."""
        if not self.openai_api_key:
            raise ValueError("OpenAI API key not configured")

        headers = {
            "Authorization": f"Bearer {self.openai_api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": request.model,
            "messages": request.messages,
            "stream": False,
        }

        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.user:
            payload["user"] = request.user

        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
        )

        response.raise_for_status()
        return dict(response.json())

    async def _forward_to_anthropic(
        self,
        client: httpx.AsyncClient,
        request: ProxyRequest,
        request_id: str,
    ) -> dict[str, Any]:
        """Forward request to Anthropic."""
        if not self.anthropic_api_key:
            raise ValueError("Anthropic API key not configured")

        headers = {
            "x-api-key": self.anthropic_api_key,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }

        # Convert OpenAI format to Anthropic format
        system_message = None
        messages = []
        for msg in request.messages:
            if msg.get("role") == "system":
                system_message = msg.get("content", "")
            else:
                messages.append(msg)

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens or 1000,
        }

        if system_message:
            payload["system"] = system_message
        if request.temperature is not None:
            payload["temperature"] = request.temperature

        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
        )

        response.raise_for_status()
        anthropic_response = response.json()

        # Convert Anthropic response to OpenAI format
        return {
            "id": anthropic_response.get("id", request_id),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": anthropic_response.get("content", [{}])[0].get("text", ""),
                    },
                    "finish_reason": anthropic_response.get("stop_reason", "stop"),
                }
            ],
            "usage": {
                "prompt_tokens": anthropic_response.get("usage", {}).get("input_tokens", 0),
                "completion_tokens": anthropic_response.get("usage", {}).get("output_tokens", 0),
                "total_tokens": (
                    anthropic_response.get("usage", {}).get("input_tokens", 0) +
                    anthropic_response.get("usage", {}).get("output_tokens", 0)
                ),
            },
        }

    async def _forward_to_openrouter(
        self,
        client: httpx.AsyncClient,
        request: ProxyRequest,
        request_id: str,
    ) -> dict[str, Any]:
        """Forward request to OpenRouter."""
        if not self.openrouter_api_key:
            raise ValueError("OpenRouter API key not configured")

        headers = {
            "Authorization": f"Bearer {self.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.base_url,
            "X-Title": "CostGuard",
        }

        payload = {
            "model": request.model,
            "messages": request.messages,
            "stream": False,
        }

        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p

        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
        )

        response.raise_for_status()
        return dict(response.json())


# Create FastAPI app
def create_app(
    openai_api_key: str | None = None,
    anthropic_api_key: str | None = None,
    openrouter_api_key: str | None = None,
) -> FastAPI:
    """Create FastAPI application."""
    server = ProxyServer(
        openai_api_key=openai_api_key,
        anthropic_api_key=anthropic_api_key,
        openrouter_api_key=openrouter_api_key,
    )
    return server.create_app()
