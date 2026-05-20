"""Core data models for CostGuard using Pydantic."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing_extensions import Self


class BudgetWindow(str, Enum):
    """Budget time window types."""

    SESSION = "session"
    HOUR = "hour"
    DAY = "day"
    PROJECT = "project"


class Provider(str, Enum):
    """Supported AI providers."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"


class AlertChannel(str, Enum):
    """Alert notification channels."""

    CONSOLE = "console"
    WEBHOOK = "webhook"
    FILE = "file"


class CircuitBreakerStatus(str, Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Limit exceeded, blocking requests
    HALF_OPEN = "half_open"  # Testing if limit reset


class BudgetConfig(BaseModel):
    """Configuration for spending limits across time windows."""

    model_config = ConfigDict(frozen=True)

    session_limit: Decimal = Field(
        default=Decimal("10.00"),
        description="Maximum spend per session (USD)",
        ge=Decimal("0.00"),
    )
    hour_limit: Decimal = Field(
        default=Decimal("50.00"),
        description="Maximum spend per hour (USD)",
        ge=Decimal("0.00"),
    )
    day_limit: Decimal = Field(
        default=Decimal("200.00"),
        description="Maximum spend per day (USD)",
        ge=Decimal("0.00"),
    )
    project_limit: Decimal = Field(
        default=Decimal("1000.00"),
        description="Maximum spend per project (USD)",
        ge=Decimal("0.00"),
    )
    safe_mode_threshold: Decimal = Field(
        default=Decimal("5.00"),
        description="Cost threshold for safe mode confirmation (USD)",
        ge=Decimal("0.00"),
    )
    alert_channels: list[AlertChannel] = Field(
        default=[AlertChannel.CONSOLE],
        description="Channels for limit alerts",
    )
    webhook_url: str | None = Field(
        default=None,
        description="Webhook URL for alerts",
    )

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook_url(cls, v: str | None, info: Any) -> str | None:
        """Validate webhook URL when webhook channel is enabled."""
        if v is None:
            data = info.data
            alert_channels = data.get("alert_channels", [])
            if AlertChannel.WEBHOOK in alert_channels:
                raise ValueError("webhook_url required when WEBHOOK channel is enabled")
        return v


class ProviderPricing(BaseModel):
    """Pricing information for a specific model/provider."""

    model_config = ConfigDict(frozen=True)

    provider: Provider
    model_id: str = Field(..., description="Provider-specific model identifier")
    model_name: str = Field(..., description="Human-readable model name")
    input_price_per_mtok: Decimal = Field(
        ...,
        description="Price per million input tokens (USD)",
        ge=Decimal("0.00"),
    )
    output_price_per_mtok: Decimal = Field(
        ...,
        description="Price per million output tokens (USD)",
        ge=Decimal("0.00"),
    )
    context_window: int = Field(
        ...,
        description="Maximum context window in tokens",
        gt=0,
    )
    active: bool = Field(
        default=True,
        description="Whether this pricing is currently active",
    )
    effective_date: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this pricing became effective",
    )

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        """Calculate estimated cost for given token counts."""
        input_cost = Decimal(input_tokens) * self.input_price_per_mtok / Decimal("1000000")
        output_cost = Decimal(output_tokens) * self.output_price_per_mtok / Decimal("1000000")
        return input_cost + output_cost


class SpendRecord(BaseModel):
    """Record of a single API call spend."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    provider: Provider
    model_id: str
    request_id: str = Field(..., description="Unique request identifier")
    session_id: str = Field(..., description="Session identifier")
    project_id: str = Field(default="default", description="Project identifier")
    input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    total_tokens: int = Field(default=0, ge=0)
    estimated_cost: Decimal = Field(..., ge=Decimal("0.00"))
    actual_cost: Decimal | None = Field(
        default=None,
        description="Actual cost if different from estimate",
    )
    status: Literal["pending", "completed", "failed", "blocked"] = Field(
        default="pending",
        description="Request status",
    )
    error_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def compute_total_tokens(self) -> Self:
        """Ensure total_tokens equals input + output."""
        self.total_tokens = self.input_tokens + self.output_tokens
        return self


class CircuitBreakerState(BaseModel):
    """Current state of the circuit breaker."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    session_id: str
    project_id: str = "default"
    status: CircuitBreakerStatus = CircuitBreakerStatus.CLOSED
    session_start: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_request_time: datetime | None = None
    session_spend: Decimal = Field(default=Decimal("0.00"))
    hour_spend: Decimal = Field(default=Decimal("0.00"))
    day_spend: Decimal = Field(default=Decimal("0.00"))
    project_spend: Decimal = Field(default=Decimal("0.00"))
    total_requests: int = Field(default=0, ge=0)
    blocked_requests: int = Field(default=0, ge=0)
    last_alert_time: datetime | None = None
    triggered_limit: BudgetWindow | None = None

    def update_spend(self, amount: Decimal) -> None:
        """Add spend amount to all windows."""
        self.session_spend += amount
        self.hour_spend += amount
        self.day_spend += amount
        self.project_spend += amount
        self.total_requests += 1
        self.last_request_time = datetime.now(timezone.utc)

    def increment_blocked(self) -> None:
        """Increment blocked request counter."""
        self.blocked_requests += 1

    def check_limits(self, config: BudgetConfig) -> list[BudgetWindow]:
        """Check which limits are exceeded. Returns list of exceeded windows."""
        exceeded: list[BudgetWindow] = []
        if self.session_spend >= config.session_limit:
            exceeded.append(BudgetWindow.SESSION)
        if self.hour_spend >= config.hour_limit:
            exceeded.append(BudgetWindow.HOUR)
        if self.day_spend >= config.day_limit:
            exceeded.append(BudgetWindow.DAY)
        if self.project_spend >= config.project_limit:
            exceeded.append(BudgetWindow.PROJECT)
        return exceeded


class LimitExceededError(BaseModel):
    """Structured error response when limits are exceeded."""

    error_type: Literal["limit_exceeded"] = "limit_exceeded"
    message: str
    exceeded_limits: list[BudgetWindow]
    current_spend: dict[str, str]
    limits: dict[str, str]
    session_id: str
    project_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    request_id: str | None = None

    @classmethod
    def from_state(
        cls,
        state: CircuitBreakerState,
        config: BudgetConfig,
        exceeded: list[BudgetWindow],
        request_id: str | None = None,
    ) -> Self:
        """Create error from circuit breaker state."""
        return cls(
            message=f"Spending limit exceeded: {', '.join(exceeded)}",
            exceeded_limits=exceeded,
            current_spend={
                "session": f"{state.session_spend:.2f}",
                "hour": f"{state.hour_spend:.2f}",
                "day": f"{state.day_spend:.2f}",
                "project": f"{state.project_spend:.2f}",
            },
            limits={
                "session": f"{config.session_limit:.2f}",
                "hour": f"{config.hour_limit:.2f}",
                "day": f"{config.day_limit:.2f}",
                "project": f"{config.project_limit:.2f}",
            },
            session_id=state.session_id,
            project_id=state.project_id,
            request_id=request_id,
        )


class AlertEvent(BaseModel):
    """Alert event when limit is triggered."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    alert_type: Literal["limit_warning", "limit_exceeded", "safe_mode_triggered"]
    severity: Literal["info", "warning", "critical"] = "warning"
    session_id: str
    project_id: str
    exceeded_limits: list[BudgetWindow] = Field(default_factory=list)
    current_spend: dict[str, str] = Field(default_factory=dict)
    message: str
    acknowledged: bool = False


class CostEstimateRequest(BaseModel):
    """Request for cost estimation."""

    provider: Provider
    model_id: str
    messages: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Chat messages for token estimation",
    )
    estimated_output_tokens: int = Field(
        default=1000,
        description="Estimated output token count",
        ge=0,
    )


class CostEstimateResponse(BaseModel):
    """Response with cost estimation."""

    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_total_tokens: int
    estimated_cost: Decimal
    pricing_used: ProviderPricing
    safe_mode_required: bool
    safe_mode_threshold: Decimal


class ProxyRequest(BaseModel):
    """OpenAI-compatible chat completion request."""

    model: str
    messages: list[dict[str, Any]]
    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    stream: bool = False
    user: str | None = None

    @field_validator("messages")
    @classmethod
    def validate_messages(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Validate message format."""
        if not v:
            raise ValueError("messages cannot be empty")
        for msg in v:
            if "role" not in msg:
                raise ValueError("each message must have a 'role' field")
            if "content" not in msg:
                raise ValueError("each message must have a 'content' field")
            if msg["role"] not in ("system", "user", "assistant", "tool"):
                raise ValueError(f"invalid role: {msg['role']}")
        return v


class ProxyResponse(BaseModel):
    """OpenAI-compatible chat completion response."""

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[dict[str, Any]]
    usage: dict[str, int] | None = None
    costguard_metadata: dict[str, Any] | None = Field(
        default=None,
        description="CostGuard-specific metadata",
    )


class DashboardMetrics(BaseModel):
    """Real-time metrics for dashboard display."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    session_id: str
    project_id: str
    status: CircuitBreakerStatus
    session_spend: Decimal
    hour_spend: Decimal
    day_spend: Decimal
    project_spend: Decimal
    session_limit: Decimal
    hour_limit: Decimal
    day_limit: Decimal
    project_limit: Decimal
    total_requests: int
    blocked_requests: int
    recent_transactions: list[SpendRecord] = Field(default_factory=list)
    recent_alerts: list[AlertEvent] = Field(default_factory=list)

    @property
    def session_percentage(self) -> float:
        """Session spend as percentage of limit."""
        if self.session_limit == 0:
            return 0.0
        return float(self.session_spend / self.session_limit * 100)

    @property
    def hour_percentage(self) -> float:
        """Hour spend as percentage of limit."""
        if self.hour_limit == 0:
            return 0.0
        return float(self.hour_spend / self.hour_limit * 100)

    @property
    def day_percentage(self) -> float:
        """Day spend as percentage of limit."""
        if self.day_limit == 0:
            return 0.0
        return float(self.day_spend / self.day_limit * 100)

    @property
    def project_percentage(self) -> float:
        """Project spend as percentage of limit."""
        if self.project_limit == 0:
            return 0.0
        return float(self.project_spend / self.project_limit * 100)
