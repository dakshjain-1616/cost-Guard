"""Real-time dashboard with WebSocket and terminal display."""

from __future__ import annotations

import asyncio
import json
import signal
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import websockets
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from costguard.database import Database, get_database
from costguard.models import (
    BudgetConfig,
    CircuitBreakerStatus,
    DashboardMetrics,
)


@dataclass
class DashboardConfig:
    """Configuration for dashboard display."""

    refresh_interval: float = 1.0
    max_recent_transactions: int = 10
    max_recent_alerts: int = 5
    show_progress_bars: bool = True
    session_id: str = "default"
    project_id: str = "default"


class DashboardClient:
    """WebSocket client for dashboard data."""

    def __init__(
        self,
        ws_url: str = "ws://localhost:8000/v1/dashboard/ws",
        config: DashboardConfig | None = None,
    ) -> None:
        """Initialize dashboard client.

        Args:
            ws_url: WebSocket URL for dashboard.
            config: Dashboard configuration.
        """
        self.ws_url = ws_url
        self.config = config or DashboardConfig()
        self._websocket: Any = None
        self._running = False
        self._metrics: DashboardMetrics | None = None
        self._callbacks: list[Callable[[DashboardMetrics], None]] = []

    async def connect(self) -> bool:
        """Connect to WebSocket server.

        Returns:
            True if connected successfully.
        """
        try:
            # Add query parameters
            url = f"{self.ws_url}?session_id={self.config.session_id}&project_id={self.config.project_id}"
            self._websocket = await websockets.connect(url)
            self._running = True

            # Start receive loop
            asyncio.create_task(self._receive_loop())

            return True
        except Exception as e:
            print(f"Failed to connect to dashboard: {e}")
            return False

    async def disconnect(self) -> None:
        """Disconnect from WebSocket server."""
        self._running = False
        if self._websocket:
            await self._websocket.close()
            self._websocket = None

    async def _receive_loop(self) -> None:
        """Receive messages from WebSocket."""
        if not self._websocket:
            return

        try:
            async for message in self._websocket:
                try:
                    data = json.loads(message)
                    self._metrics = DashboardMetrics(**data)

                    # Notify callbacks
                    for callback in self._callbacks:
                        with suppress(Exception):
                            callback(self._metrics)

                except json.JSONDecodeError:
                    pass
                except Exception:
                    pass

        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception:
            pass

    async def send_ping(self) -> None:
        """Send ping to keep connection alive."""
        if self._websocket:
            await self._websocket.send("ping")

    def on_update(self, callback: Callable[[DashboardMetrics], None]) -> None:
        """Register callback for metrics updates."""
        self._callbacks.append(callback)

    def get_metrics(self) -> DashboardMetrics | None:
        """Get latest metrics."""
        return self._metrics


class TerminalDashboard:
    """Terminal-based dashboard display using Rich."""

    def __init__(self, config: DashboardConfig | None = None) -> None:
        """Initialize terminal dashboard.

        Args:
            config: Dashboard configuration.
        """
        self.config = config or DashboardConfig()
        self.console = Console()
        self._live: Live | None = None
        self._running = False

    def _create_layout(self) -> Layout:
        """Create dashboard layout."""
        layout = Layout()

        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="footer", size=3),
        )

        layout["main"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="right", ratio=1),
        )

        layout["left"].split_column(
            Layout(name="spend", ratio=2),
            Layout(name="alerts", ratio=1),
        )

        layout["right"].split_column(
            Layout(name="transactions", ratio=2),
            Layout(name="status", ratio=1),
        )

        return layout

    def _create_header(self, metrics: DashboardMetrics | None) -> Panel:
        """Create header panel."""
        if metrics is None:
            return Panel(
                Text("CostGuard Dashboard - Connecting...", style="yellow"),
                border_style="blue",
            )

        status_color = "green" if metrics.status == CircuitBreakerStatus.CLOSED else "red"
        status_text = metrics.status.value.upper()

        header_text = Text()
        header_text.append("CostGuard Dashboard  ", style="bold blue")
        header_text.append(f"Session: {metrics.session_id}  ", style="cyan")
        header_text.append(f"Project: {metrics.project_id}  ", style="cyan")
        header_text.append("Status: ", style="white")
        header_text.append(status_text, style=f"bold {status_color}")

        return Panel(header_text, border_style="blue")

    def _create_spend_panel(self, metrics: DashboardMetrics | None) -> Panel:
        """Create spend tracking panel."""
        if metrics is None:
            return Panel(
                Text("Loading...", style="dim"),
                title="Spend Tracking",
                border_style="blue",
            )

        table = Table(show_header=False, box=None)
        table.add_column("Window", style="cyan")
        table.add_column("Progress", width=30)
        table.add_column("Amount", justify="right", style="green")
        table.add_column("Limit", justify="right", style="yellow")
        table.add_column("%", justify="right")

        windows = [
            ("Session", metrics.session_spend, metrics.session_limit, metrics.session_percentage),
            ("Hour", metrics.hour_spend, metrics.hour_limit, metrics.hour_percentage),
            ("Day", metrics.day_spend, metrics.day_limit, metrics.day_percentage),
            ("Project", metrics.project_spend, metrics.project_limit, metrics.project_percentage),
        ]

        for name, spend, limit, percentage in windows:
            # Determine color based on percentage
            if percentage >= 100:
                bar_color = "red"
                pct_style = "bold red"
            elif percentage >= 80:
                bar_color = "yellow"
                pct_style = "bold yellow"
            else:
                bar_color = "green"
                pct_style = "green"

            # Create progress bar
            bar_width = 20
            filled = int((min(percentage, 100) / 100) * bar_width)
            bar = "█" * filled + "░" * (bar_width - filled)

            table.add_row(
                name,
                f"[{bar_color}]{bar}[/{bar_color}]",
                f"${spend:.2f}",
                f"${limit:.2f}",
                f"[{pct_style}]{percentage:.1f}%[/{pct_style}]",
            )

        return Panel(table, title="Spend Tracking", border_style="blue")

    def _create_transactions_panel(self, metrics: DashboardMetrics | None) -> Panel:
        """Create recent transactions panel."""
        if metrics is None or not metrics.recent_transactions:
            return Panel(
                Text("No recent transactions", style="dim"),
                title="Recent Transactions",
                border_style="blue",
            )

        table = Table(show_header=True, box=None)
        table.add_column("Time", style="dim", width=8)
        table.add_column("Model", style="cyan", width=20)
        table.add_column("Tokens", justify="right", width=8)
        table.add_column("Cost", justify="right", width=10)
        table.add_column("Status", width=10)

        for tx in metrics.recent_transactions[: self.config.max_recent_transactions]:
            time_str = tx.timestamp.strftime("%H:%M:%S")
            model_short = tx.model_id.split("/")[-1][:18]
            status_style = {
                "completed": "green",
                "pending": "yellow",
                "failed": "red",
                "blocked": "red",
            }.get(tx.status, "white")

            table.add_row(
                time_str,
                model_short,
                str(tx.total_tokens),
                f"${tx.estimated_cost:.4f}",
                f"[{status_style}]{tx.status}[/{status_style}]",
            )

        return Panel(table, title="Recent Transactions", border_style="blue")

    def _create_alerts_panel(self, metrics: DashboardMetrics | None) -> Panel:
        """Create alerts panel."""
        if metrics is None or not metrics.recent_alerts:
            return Panel(
                Text("No recent alerts", style="dim"),
                title="Recent Alerts",
                border_style="blue",
            )

        table = Table(show_header=False, box=None)
        table.add_column("Time", style="dim", width=8)
        table.add_column("Type", style="cyan")
        table.add_column("Message", style="white")

        for alert in metrics.recent_alerts[: self.config.max_recent_alerts]:
            time_str = alert.timestamp.strftime("%H:%M:%S")
            severity_style = {
                "info": "blue",
                "warning": "yellow",
                "critical": "red",
            }.get(alert.severity, "white")

            table.add_row(
                time_str,
                f"[{severity_style}]{alert.alert_type}[/{severity_style}]",
                alert.message[:40] + "..." if len(alert.message) > 40 else alert.message,
            )

        return Panel(table, title="Recent Alerts", border_style="blue")

    def _create_status_panel(self, metrics: DashboardMetrics | None) -> Panel:
        """Create status panel."""
        if metrics is None:
            return Panel(
                Text("Loading...", style="dim"),
                title="Status",
                border_style="blue",
            )

        table = Table(show_header=False, box=None)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="white")

        table.add_row("Total Requests", str(metrics.total_requests))
        table.add_row("Blocked Requests", str(metrics.blocked_requests))
        table.add_row("Success Rate", f"{self._calculate_success_rate(metrics):.1f}%")

        if metrics.recent_transactions:
            total_cost = sum(tx.estimated_cost for tx in metrics.recent_transactions)
            table.add_row("Recent Total", f"${total_cost:.4f}")

        return Panel(table, title="Status", border_style="blue")

    def _create_footer(self, metrics: DashboardMetrics | None) -> Panel:
        """Create footer panel."""
        footer_text = Text()
        footer_text.append("Press ", style="dim")
        footer_text.append("Ctrl+C", style="bold")
        footer_text.append(" to exit  |  ", style="dim")
        footer_text.append("CostGuard v1.0.0", style="cyan")

        if metrics:
            footer_text.append("  |  Last update: ", style="dim")
            footer_text.append(metrics.timestamp.strftime("%H:%M:%S"), style="cyan")

        return Panel(footer_text, border_style="blue")

    def _calculate_success_rate(self, metrics: DashboardMetrics) -> float:
        """Calculate success rate percentage."""
        if metrics.total_requests == 0:
            return 100.0
        successful = metrics.total_requests - metrics.blocked_requests
        return (successful / metrics.total_requests) * 100

    def update(self, metrics: DashboardMetrics | None) -> Layout:
        """Update dashboard with new metrics."""
        layout = self._create_layout()

        layout["header"].update(self._create_header(metrics))
        layout["spend"].update(self._create_spend_panel(metrics))
        layout["transactions"].update(self._create_transactions_panel(metrics))
        layout["alerts"].update(self._create_alerts_panel(metrics))
        layout["status"].update(self._create_status_panel(metrics))
        layout["footer"].update(self._create_footer(metrics))

        return layout

    async def run(self, client: DashboardClient | None = None) -> None:
        """Run the dashboard display.

        Args:
            client: Optional dashboard client for WebSocket data.
        """
        self._running = True

        # Setup signal handler
        def signal_handler(sig: int, frame: Any) -> None:
            self._running = False

        signal.signal(signal.SIGINT, signal_handler)

        # Connect client if provided
        if client:
            connected = await client.connect()
            if not connected:
                self.console.print("[red]Failed to connect to dashboard server[/red]")
                return

        try:
            with Live(
                self.update(None),
                console=self.console,
                refresh_per_second=1 / self.config.refresh_interval,
                screen=True,
            ) as live:
                while self._running:
                    # Get latest metrics
                    metrics = None
                    if client:
                        metrics = client.get_metrics()

                    # Update display
                    live.update(self.update(metrics))

                    # Send ping to keep connection alive
                    if client:
                        await client.send_ping()

                    await asyncio.sleep(self.config.refresh_interval)

        finally:
            if client:
                await client.disconnect()

    def stop(self) -> None:
        """Stop the dashboard."""
        self._running = False


class DashboardMetricsBuilder:
    """Builds dashboard metrics from database."""

    def __init__(self, database: Database | None = None) -> None:
        """Initialize metrics builder.

        Args:
            database: Database instance.
        """
        self._db = database or get_database()

    async def build_metrics(
        self,
        session_id: str,
        project_id: str = "default",
        config: BudgetConfig | None = None,
    ) -> DashboardMetrics:
        """Build dashboard metrics from database.

        Args:
            session_id: Session identifier.
            project_id: Project identifier.
            config: Budget configuration.

        Returns:
            Dashboard metrics.
        """

        cfg = config or BudgetConfig()

        # Get circuit breaker state
        state = await self._db.get_or_create_circuit_breaker_state(session_id, project_id)

        # Get recent transactions
        transactions = await self._db.get_spend_records(
            session_id=session_id,
            limit=10,
        )

        # Get recent alerts
        alerts = await self._db.get_recent_alerts(
            session_id=session_id,
            limit=5,
        )

        return DashboardMetrics(
            session_id=session_id,
            project_id=project_id,
            status=state.status,
            session_spend=state.session_spend,
            hour_spend=state.hour_spend,
            day_spend=state.day_spend,
            project_spend=state.project_spend,
            session_limit=cfg.session_limit,
            hour_limit=cfg.hour_limit,
            day_limit=cfg.day_limit,
            project_limit=cfg.project_limit,
            total_requests=state.total_requests,
            blocked_requests=state.blocked_requests,
            recent_transactions=transactions,
            recent_alerts=alerts,
        )


async def run_dashboard(
    ws_url: str = "ws://localhost:8000/v1/dashboard/ws",
    session_id: str = "default",
    project_id: str = "default",
) -> None:
    """Run the dashboard.

    Args:
        ws_url: WebSocket URL.
        session_id: Session identifier.
        project_id: Project identifier.
    """
    config = DashboardConfig(
        session_id=session_id,
        project_id=project_id,
    )

    client = DashboardClient(ws_url=ws_url, config=config)
    dashboard = TerminalDashboard(config=config)

    await dashboard.run(client)


if __name__ == "__main__":
    asyncio.run(run_dashboard())
