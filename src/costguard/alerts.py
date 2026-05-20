"""Alert and logging system with structured output."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Protocol

import httpx
import structlog
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from costguard.models import AlertChannel, AlertEvent, BudgetConfig, BudgetWindow

# Configure structlog for structured logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.dict_tracebacks,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO level
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

logger = structlog.get_logger("costguard")


class AlertHandler(Protocol):
    """Protocol for alert handlers."""

    async def send_alert(self, event: AlertEvent, config: BudgetConfig) -> bool:
        """Send an alert event.

        Args:
            event: Alert event to send.
            config: Budget configuration.

        Returns:
            True if alert was sent successfully.
        """
        ...


class ConsoleAlertHandler:
    """Handler for console alerts using Rich."""

    def __init__(self, console: Console | None = None) -> None:
        """Initialize with optional console."""
        self.console = console or Console()

    async def send_alert(self, event: AlertEvent, config: BudgetConfig) -> bool:
        """Send alert to console."""
        try:
            # Determine color based on severity
            color_map = {
                "info": "blue",
                "warning": "yellow",
                "critical": "red",
            }
            color = color_map.get(event.severity, "white")

            # Build alert content
            title = f"🚨 CostGuard Alert: {event.alert_type.replace('_', ' ').title()}"

            content_lines = [
                f"Session: {event.session_id}",
                f"Project: {event.project_id}",
                f"Severity: {event.severity.upper()}",
                f"Message: {event.message}",
            ]

            if event.exceeded_limits:
                content_lines.append(f"Exceeded Limits: {', '.join(event.exceeded_limits)}")

            if event.current_spend:
                content_lines.append("Current Spend:")
                for window, amount in event.current_spend.items():
                    content_lines.append(f"  - {window}: ${amount}")

            content = "\n".join(content_lines)

            # Create panel
            panel = Panel(
                Text(content, style=color),
                title=title,
                border_style=color,
                padding=(1, 2),
            )

            self.console.print(panel)
            return True

        except Exception as e:
            logger.error("Failed to send console alert", error=str(e), event_id=str(event.id))
            return False


class WebhookAlertHandler:
    """Handler for webhook alerts."""

    def __init__(self, timeout: float = 30.0) -> None:
        """Initialize with timeout."""
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def send_alert(self, event: AlertEvent, config: BudgetConfig) -> bool:
        """Send alert to configured webhook."""
        if not config.webhook_url:
            logger.warning("Webhook URL not configured, skipping webhook alert")
            return False

        try:
            client = await self._get_client()

            payload = {
                "event": {
                    "id": str(event.id),
                    "timestamp": event.timestamp.isoformat(),
                    "type": event.alert_type,
                    "severity": event.severity,
                },
                "session": {
                    "id": event.session_id,
                    "project_id": event.project_id,
                },
                "limits": {
                    "exceeded": [limit.value for limit in event.exceeded_limits],
                    "current_spend": event.current_spend,
                },
                "message": event.message,
            }

            response = await client.post(
                config.webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )

            if response.status_code < 400:
                logger.info(
                    "Webhook alert sent successfully",
                    event_id=str(event.id),
                    status_code=response.status_code,
                )
                return True
            else:
                logger.error(
                    "Webhook alert failed",
                    event_id=str(event.id),
                    status_code=response.status_code,
                    response=response.text,
                )
                return False

        except httpx.TimeoutException:
            logger.error("Webhook alert timed out", event_id=str(event.id))
            return False
        except Exception as e:
            logger.error("Failed to send webhook alert", error=str(e), event_id=str(event.id))
            return False

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None


class FileAlertHandler:
    """Handler for file-based alerts."""

    def __init__(self, log_dir: Path | None = None) -> None:
        """Initialize with log directory."""
        if log_dir is None:
            log_dir = Path.home() / ".costguard" / "alerts"
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def send_alert(self, event: AlertEvent, config: BudgetConfig) -> bool:
        """Write alert to file."""
        try:
            # Create log file path based on date
            date_str = event.timestamp.strftime("%Y-%m-%d")
            log_file = self.log_dir / f"alerts-{date_str}.jsonl"

            # Prepare log entry
            entry = {
                "id": str(event.id),
                "timestamp": event.timestamp.isoformat(),
                "alert_type": event.alert_type,
                "severity": event.severity,
                "session_id": event.session_id,
                "project_id": event.project_id,
                "exceeded_limits": [limit.value for limit in event.exceeded_limits],
                "current_spend": event.current_spend,
                "message": event.message,
                "acknowledged": event.acknowledged,
            }

            async with self._lock:
                with open(log_file, "a") as f:
                    f.write(json.dumps(entry) + "\n")

            return True

        except Exception as e:
            logger.error("Failed to write alert to file", error=str(e), event_id=str(event.id))
            return False


class AlertManager:
    """Manages alert handlers and dispatch."""

    def __init__(
        self,
        config: BudgetConfig | None = None,
        console: Console | None = None,
    ) -> None:
        """Initialize alert manager.

        Args:
            config: Budget configuration with alert channels.
            console: Optional Rich console for output.
        """
        self.config = config or BudgetConfig()
        self._handlers: dict[AlertChannel, AlertHandler] = {}
        self._lock = asyncio.Lock()

        # Initialize handlers based on config
        self._initialize_handlers(console)

    def _initialize_handlers(self, console: Console | None = None) -> None:
        """Initialize alert handlers based on configuration."""
        for channel in self.config.alert_channels:
            if channel == AlertChannel.CONSOLE:
                self._handlers[channel] = ConsoleAlertHandler(console)
            elif channel == AlertChannel.WEBHOOK:
                self._handlers[channel] = WebhookAlertHandler()
            elif channel == AlertChannel.FILE:
                self._handlers[channel] = FileAlertHandler()

    async def send_alert(self, event: AlertEvent) -> dict[AlertChannel, bool]:
        """Send alert to all configured channels.

        Args:
            event: Alert event to send.

        Returns:
            Dictionary mapping channels to success status.
        """
        results: dict[AlertChannel, bool] = {}

        async with self._lock:
            tasks = []
            channels = []

            for channel, handler in self._handlers.items():
                channels.append(channel)
                tasks.append(handler.send_alert(event, self.config))

            if tasks:
                responses = await asyncio.gather(*tasks, return_exceptions=True)
                for channel, result in zip(channels, responses, strict=False):
                    if isinstance(result, Exception):
                        logger.error(
                            "Alert handler failed",
                            channel=channel.value,
                            error=str(result),
                        )
                        results[channel] = False
                    else:
                        results[channel] = bool(result)

        return results

    async def send_limit_warning(
        self,
        session_id: str,
        project_id: str,
        window: BudgetWindow,
        current_spend: dict[str, str],
        threshold_percentage: float,
    ) -> None:
        """Send limit warning alert.

        Args:
            session_id: Session identifier.
            project_id: Project identifier.
            window: Budget window approaching limit.
            current_spend: Current spend amounts.
            threshold_percentage: Percentage of limit used.
        """
        event = AlertEvent(
            alert_type="limit_warning",
            severity="warning" if threshold_percentage < 90 else "critical",
            session_id=session_id,
            project_id=project_id,
            exceeded_limits=[window],
            current_spend=current_spend,
            message=f"Approaching {window.value} limit: {threshold_percentage:.1f}% used",
        )

        await self.send_alert(event)

    async def send_safe_mode_triggered(
        self,
        session_id: str,
        project_id: str,
        estimated_cost: str,
        threshold: str,
    ) -> None:
        """Send safe mode triggered alert.

        Args:
            session_id: Session identifier.
            project_id: Project identifier.
            estimated_cost: Estimated cost of request.
            threshold: Safe mode threshold.
        """
        event = AlertEvent(
            alert_type="safe_mode_triggered",
            severity="info",
            session_id=session_id,
            project_id=project_id,
            message=f"Safe mode triggered: estimated cost ${estimated_cost} exceeds threshold ${threshold}",
        )

        await self.send_alert(event)

    async def close(self) -> None:
        """Close all handlers."""
        for handler in self._handlers.values():
            if hasattr(handler, "close"):
                await handler.close()


class StructuredLogger:
    """Structured logging wrapper for CostGuard."""

    def __init__(self, name: str = "costguard") -> None:
        """Initialize structured logger."""
        self._logger = structlog.get_logger(name)

    def log_request_start(
        self,
        request_id: str,
        session_id: str,
        model_id: str,
        estimated_cost: str,
    ) -> None:
        """Log request start."""
        self._logger.info(
            "Request started",
            request_id=request_id,
            session_id=session_id,
            model_id=model_id,
            estimated_cost=estimated_cost,
        )

    def log_request_complete(
        self,
        request_id: str,
        session_id: str,
        model_id: str,
        actual_cost: str,
        duration_ms: float,
    ) -> None:
        """Log request completion."""
        self._logger.info(
            "Request completed",
            request_id=request_id,
            session_id=session_id,
            model_id=model_id,
            actual_cost=actual_cost,
            duration_ms=duration_ms,
        )

    def log_request_blocked(
        self,
        request_id: str,
        session_id: str,
        model_id: str,
        reason: str,
        exceeded_limits: list[str],
    ) -> None:
        """Log blocked request."""
        self._logger.warning(
            "Request blocked by circuit breaker",
            request_id=request_id,
            session_id=session_id,
            model_id=model_id,
            reason=reason,
            exceeded_limits=exceeded_limits,
        )

    def log_request_failed(
        self,
        request_id: str,
        session_id: str,
        model_id: str,
        error: str,
    ) -> None:
        """Log failed request."""
        self._logger.error(
            "Request failed",
            request_id=request_id,
            session_id=session_id,
            model_id=model_id,
            error=error,
        )

    def log_limit_exceeded(
        self,
        session_id: str,
        exceeded_limits: list[str],
        current_spend: dict[str, str],
    ) -> None:
        """Log limit exceeded event."""
        self._logger.warning(
            "Spending limit exceeded",
            session_id=session_id,
            exceeded_limits=exceeded_limits,
            current_spend=current_spend,
        )

    def log_safe_mode_confirmation(
        self,
        request_id: str,
        session_id: str,
        confirmed: bool,
        estimated_cost: str,
    ) -> None:
        """Log safe mode confirmation."""
        self._logger.info(
            "Safe mode confirmation",
            request_id=request_id,
            session_id=session_id,
            confirmed=confirmed,
            estimated_cost=estimated_cost,
        )

    def log_dashboard_connect(self, session_id: str, client_id: str) -> None:
        """Log dashboard connection."""
        self._logger.info(
            "Dashboard client connected",
            session_id=session_id,
            client_id=client_id,
        )

    def log_dashboard_disconnect(self, session_id: str, client_id: str) -> None:
        """Log dashboard disconnection."""
        self._logger.info(
            "Dashboard client disconnected",
            session_id=session_id,
            client_id=client_id,
        )


# Global instances
_alert_manager: AlertManager | None = None
_structured_logger: StructuredLogger | None = None


def get_alert_manager(config: BudgetConfig | None = None) -> AlertManager:
    """Get or create global alert manager."""
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager(config)
    return _alert_manager


def get_structured_logger() -> StructuredLogger:
    """Get or create global structured logger."""
    global _structured_logger
    if _structured_logger is None:
        _structured_logger = StructuredLogger()
    return _structured_logger


def reset_alert_manager() -> None:
    """Reset global alert manager (for testing)."""
    global _alert_manager
    _alert_manager = None
