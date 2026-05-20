"""Command-line interface for CostGuard."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from costguard.dashboard import run_dashboard

app = typer.Typer(help="CostGuard - Real-Time AI Spend Circuit Breaker")
console = Console()


@app.command()
def server(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Host to bind to"),
    port: int = typer.Option(8000, "--port", "-p", help="Port to listen on"),
    reload: bool = typer.Option(False, "--reload", "-r", help="Enable auto-reload"),
    openai_api_key: str | None = typer.Option(
        None, "--openai-api-key", envvar="OPENAI_API_KEY", help="OpenAI API key"
    ),
    anthropic_api_key: str | None = typer.Option(
        None, "--anthropic-api-key", envvar="ANTHROPIC_API_KEY", help="Anthropic API key"
    ),
    openrouter_api_key: str | None = typer.Option(
        None, "--openrouter-api-key", envvar="OPENROUTER_API_KEY", help="OpenRouter API key"
    ),
) -> None:
    """Start the CostGuard proxy server."""
    # Print banner
    banner = Panel(
        Text("CostGuard", style="bold blue") + Text(" - AI Spend Circuit Breaker", style="cyan"),
        border_style="blue",
        padding=(1, 2),
    )
    console.print(banner)
    console.print(f"Starting server on {host}:{port}\n")

    # Import here to avoid slow startup
    import uvicorn

    # Set environment variables for the app
    if openai_api_key:
        os.environ["OPENAI_API_KEY"] = openai_api_key
    if anthropic_api_key:
        os.environ["ANTHROPIC_API_KEY"] = anthropic_api_key
    if openrouter_api_key:
        os.environ["OPENROUTER_API_KEY"] = openrouter_api_key

    # Run server
    uvicorn.run(
        "costguard.server:create_app",
        host=host,
        port=port,
        reload=reload,
        factory=True,
    )


@app.command()
def dashboard(
    ws_url: str = typer.Option(
        "ws://127.0.0.1:8000/v1/dashboard/ws",
        "--ws-url",
        "-w",
        help="WebSocket URL",
    ),
    session_id: str = typer.Option(
        "default",
        "--session-id",
        "-s",
        envvar="COSTGUARD_SESSION_ID",
        help="Session ID to monitor",
    ),
    project_id: str = typer.Option(
        "default",
        "--project-id",
        "-p",
        envvar="COSTGUARD_PROJECT_ID",
        help="Project ID to monitor",
    ),
) -> None:
    """Launch the real-time dashboard."""
    # Print banner
    banner = Panel(
        Text("CostGuard", style="bold blue") + Text(" - Dashboard", style="cyan"),
        border_style="blue",
        padding=(1, 2),
    )
    console.print(banner)
    console.print(f"Connecting to {ws_url}")
    console.print(f"Session: {session_id}, Project: {project_id}\n")

    try:
        asyncio.run(run_dashboard(ws_url, session_id, project_id))
    except KeyboardInterrupt:
        console.print("\n[yellow]Dashboard stopped[/yellow]")


@app.command()
def estimate(
    model: str = typer.Option(..., "--model", "-m", help="Model ID"),
    prompt: str | None = typer.Option(None, "--prompt", help="Prompt text"),
    prompt_file: Path | None = typer.Option(
        None, "--prompt-file", "-f", help="File containing prompt"
    ),
    output_tokens: int = typer.Option(1000, "--output-tokens", "-o", help="Estimated output tokens"),
) -> None:
    """Estimate cost for a request without making it."""
    from costguard.models import CostEstimateRequest, Provider
    from costguard.pricing import get_pricing_manager

    # Get prompt
    if prompt_file:
        prompt = prompt_file.read_text()
    elif not prompt:
        console.print("[red]Error: Either --prompt or --prompt-file required[/red]")
        raise typer.Exit(1)

    # Create request
    messages = [{"role": "user", "content": prompt}]

    # Detect provider
    if model.startswith("claude-"):
        provider = Provider.ANTHROPIC
    elif "/" in model:
        provider = Provider.OPENROUTER
    else:
        provider = Provider.OPENAI

    request = CostEstimateRequest(
        provider=provider,
        model_id=model,
        messages=messages,
        estimated_output_tokens=output_tokens,
    )

    # Get estimate
    pricing_manager = get_pricing_manager()
    estimate = pricing_manager.estimate_cost(request)

    if estimate is None:
        console.print(f"[red]Unknown model: {model}[/red]")
        raise typer.Exit(1)

    # Display results
    table = Panel(
        f"""
[bold]Model:[/bold] {model}
[bold]Provider:[/bold] {estimate.pricing_used.provider.value}

[bold]Estimated Tokens:[/bold]
  Input: {estimate.estimated_input_tokens:,}
  Output: {estimate.estimated_output_tokens:,}
  Total: {estimate.estimated_total_tokens:,}

[bold]Estimated Cost:[/bold] ${estimate.estimated_cost:.6f}

[bold]Pricing:[/bold]
  Input: ${estimate.pricing_used.input_price_per_mtok}/MTok
  Output: ${estimate.pricing_used.output_price_per_mtok}/MTok
        """,
        title="Cost Estimate",
        border_style="green",
    )
    console.print(table)


@app.command()
def status(
    session_id: str = typer.Option(
        "default",
        "--session-id",
        "-s",
        help="Session ID to check",
    ),
    project_id: str = typer.Option(
        "default",
        "--project-id",
        "-p",
        help="Project ID to check",
    ),
    db_path: Path | None = typer.Option(
        None,
        "--db-path",
        envvar="COSTGUARD_DB_PATH",
        help="Path to database",
    ),
) -> None:
    """Check circuit breaker status."""
    import asyncio

    from costguard.database import Database

    async def get_status() -> None:
        db = Database(db_path) if db_path else Database()
        await db.initialize_schema()

        state = await db.get_or_create_circuit_breaker_state(session_id, project_id)
        config = await db.get_budget_config(project_id)

        # Display status
        status_color = "green" if state.status.value == "closed" else "red"

        panel = Panel(
            f"""
[bold]Session:[/bold] {state.session_id}
[bold]Project:[/bold] {state.project_id}
[bold]Status:[/bold] [{status_color}]{state.status.value.upper()}[/{status_color}]

[bold]Spend:[/bold]
  Session: ${state.session_spend:.2f} / ${config.session_limit:.2f}
  Hour: ${state.hour_spend:.2f} / ${config.hour_limit:.2f}
  Day: ${state.day_spend:.2f} / ${config.day_limit:.2f}
  Project: ${state.project_spend:.2f} / ${config.project_limit:.2f}

[bold]Requests:[/bold]
  Total: {state.total_requests}
  Blocked: {state.blocked_requests}
            """,
            title="Circuit Breaker Status",
            border_style=status_color,
        )
        console.print(panel)

        await db.close()

    asyncio.run(get_status())


@app.command()
def init(
    db_path: Path | None = typer.Option(
        None,
        "--db-path",
        envvar="COSTGUARD_DB_PATH",
        help="Path to database",
    ),
) -> None:
    """Initialize CostGuard database."""
    import asyncio

    from costguard.database import Database

    async def init_db() -> None:
        db = Database(db_path) if db_path else Database()
        await db.initialize_schema()
        console.print(f"[green]Database initialized at {db.db_path}[/green]")
        await db.close()

    asyncio.run(init_db())


@app.command()
def config() -> None:
    """Show current configuration."""
    from costguard.models import BudgetConfig

    cfg = BudgetConfig()

    panel = Panel(
        f"""
[bold]Budget Limits:[/bold]
  Session: ${cfg.session_limit}
  Hour: ${cfg.hour_limit}
  Day: ${cfg.day_limit}
  Project: ${cfg.project_limit}

[bold]Safe Mode:[/bold]
  Threshold: ${cfg.safe_mode_threshold}

[bold]Alerts:[/bold]
  Channels: {', '.join(ch.value for ch in cfg.alert_channels)}
  Webhook: {cfg.webhook_url or 'Not configured'}
        """,
        title="Configuration",
        border_style="blue",
    )
    console.print(panel)


def main() -> None:
    """Entry point for CLI."""
    app()


if __name__ == "__main__":
    main()
