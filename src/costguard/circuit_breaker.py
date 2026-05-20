"""Circuit breaker logic with deterministic evaluation order."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from costguard.database import Database, get_database
from costguard.models import (
    AlertEvent,
    BudgetConfig,
    BudgetWindow,
    CircuitBreakerState,
    CircuitBreakerStatus,
    LimitExceededError,
    SpendRecord,
)


class CircuitBreakerError(Exception):
    """Circuit breaker triggered error."""

    def __init__(self, error: LimitExceededError) -> None:
        """Initialize with structured error."""
        self.error = error
        super().__init__(error.message)


class CircuitBreaker:
    """Circuit breaker for enforcing spending limits.

    Evaluates limits in deterministic order:
    1. Session limit (most restrictive)
    2. Hour limit
    3. Day limit
    4. Project limit (least restrictive)
    """

    # Evaluation order: most restrictive to least restrictive
    EVALUATION_ORDER: list[BudgetWindow] = [
        BudgetWindow.SESSION,
        BudgetWindow.HOUR,
        BudgetWindow.DAY,
        BudgetWindow.PROJECT,
    ]

    def __init__(
        self,
        session_id: str,
        project_id: str = "default",
        config: BudgetConfig | None = None,
        database: Database | None = None,
    ) -> None:
        """Initialize circuit breaker.

        Args:
            session_id: Unique session identifier.
            project_id: Project identifier.
            config: Budget configuration.
            database: Database instance.
        """
        self.session_id = session_id
        self.project_id = project_id
        self.config = config or BudgetConfig()
        self._db = database or get_database()
        self._state: CircuitBreakerState | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize circuit breaker state from database."""
        await self._db.initialize_schema()
        self._state = await self._db.get_or_create_circuit_breaker_state(
            self.session_id,
            self.project_id,
        )

        # Refresh spend from database to ensure accuracy
        await self._refresh_spend()

    async def _refresh_spend(self) -> None:
        """Refresh spend amounts from database."""
        if self._state is None:
            return

        # Get session spend
        session_spend = await self._db.get_session_spend(self.session_id)

        # Get hourly spend
        hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        hour_spend = await self._db.get_hourly_spend(self.project_id, hour_ago)

        # Get daily spend
        day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        day_spend = await self._db.get_daily_spend(self.project_id, day_start)

        # Get project spend
        project_spend = await self._db.get_project_spend(self.project_id)

        # Update state
        self._state.session_spend = session_spend
        self._state.hour_spend = hour_spend
        self._state.day_spend = day_spend
        self._state.project_spend = project_spend

    async def check_limits(self, estimated_cost: Decimal, request_id: str | None = None) -> None:
        """Check if request should be allowed.

        Evaluates limits in deterministic order and raises CircuitBreakerError
        if any limit is exceeded.

        Args:
            estimated_cost: Estimated cost of the request.
            request_id: Optional request identifier for error tracking.

        Raises:
            CircuitBreakerError: If any limit is exceeded.
        """
        if self._state is None:
            await self.initialize()

        assert self._state is not None

        async with self._lock:
            # Refresh spend before checking
            await self._refresh_spend()

            # Calculate projected spend
            projected_session = self._state.session_spend + estimated_cost
            projected_hour = self._state.hour_spend + estimated_cost
            projected_day = self._state.day_spend + estimated_cost
            projected_project = self._state.project_spend + estimated_cost

            # Check limits in deterministic order
            exceeded: list[BudgetWindow] = []

            # 1. Session limit (most restrictive)
            if projected_session > self.config.session_limit:
                exceeded.append(BudgetWindow.SESSION)

            # 2. Hour limit
            if projected_hour > self.config.hour_limit:
                exceeded.append(BudgetWindow.HOUR)

            # 3. Day limit
            if projected_day > self.config.day_limit:
                exceeded.append(BudgetWindow.DAY)

            # 4. Project limit (least restrictive)
            if projected_project > self.config.project_limit:
                exceeded.append(BudgetWindow.PROJECT)

            if exceeded:
                # Update state
                self._state.status = CircuitBreakerStatus.OPEN
                self._state.triggered_limit = exceeded[0]  # First triggered
                self._state.increment_blocked()
                await self._db.save_circuit_breaker_state(self._state)

                # Create alert
                await self._create_limit_alert(exceeded)

                # Raise error
                error = LimitExceededError.from_state(
                    self._state,
                    self.config,
                    exceeded,
                    request_id,
                )
                raise CircuitBreakerError(error)

            # Update last request time
            self._state.last_request_time = datetime.now(timezone.utc)
            await self._db.save_circuit_breaker_state(self._state)

    async def record_spend(
        self,
        spend_record: SpendRecord,
        actual_cost: Decimal | None = None,
    ) -> None:
        """Record actual spend after request completion.

        Args:
            spend_record: Spend record to save.
            actual_cost: Optional actual cost (if different from estimate).
        """
        if self._state is None:
            await self.initialize()

        assert self._state is not None

        async with self._lock:
            # Update spend record
            spend_record.status = "completed"
            if actual_cost:
                spend_record.actual_cost = actual_cost

            await self._db.save_spend_record(spend_record)

            # Update state
            cost = actual_cost if actual_cost else spend_record.estimated_cost
            self._state.update_spend(cost)

            # Check if we should reset status
            exceeded = self._state.check_limits(self.config)
            if not exceeded and self._state.status == CircuitBreakerStatus.OPEN:
                self._state.status = CircuitBreakerStatus.CLOSED
                self._state.triggered_limit = None

            await self._db.save_circuit_breaker_state(self._state)

    async def record_blocked(self, spend_record: SpendRecord, error_message: str) -> None:
        """Record a blocked request.

        Args:
            spend_record: Spend record (will be marked as blocked).
            error_message: Error message explaining why blocked.
        """
        if self._state is None:
            await self.initialize()

        assert self._state is not None

        async with self._lock:
            spend_record.status = "blocked"
            spend_record.error_message = error_message
            await self._db.save_spend_record(spend_record)

            self._state.increment_blocked()
            await self._db.save_circuit_breaker_state(self._state)

    async def record_failed(self, spend_record: SpendRecord, error_message: str) -> None:
        """Record a failed request.

        Args:
            spend_record: Spend record (will be marked as failed).
            error_message: Error message explaining failure.
        """
        spend_record.status = "failed"
        spend_record.error_message = error_message
        await self._db.save_spend_record(spend_record)

    async def _create_limit_alert(self, exceeded: list[BudgetWindow]) -> None:
        """Create alert event for exceeded limits."""
        if self._state is None:
            return

        severity: Any = "warning"
        if BudgetWindow.SESSION in exceeded or BudgetWindow.HOUR in exceeded:
            severity = "critical"
        elif BudgetWindow.DAY in exceeded:
            severity = "warning"

        alert = AlertEvent(
            alert_type="limit_exceeded",
            severity=severity,
            session_id=self.session_id,
            project_id=self.project_id,
            exceeded_limits=exceeded,
            current_spend={
                "session": str(self._state.session_spend),
                "hour": str(self._state.hour_spend),
                "day": str(self._state.day_spend),
                "project": str(self._state.project_spend),
            },
            message=f"Spending limits exceeded: {', '.join(exceeded)}",
        )

        await self._db.save_alert_event(alert)

    def get_state(self) -> CircuitBreakerState | None:
        """Get current circuit breaker state."""
        return self._state

    async def reset(self) -> None:
        """Reset circuit breaker state (for testing)."""
        if self._state:
            self._state.status = CircuitBreakerStatus.CLOSED
            self._state.triggered_limit = None
            await self._db.save_circuit_breaker_state(self._state)

    @property
    def is_open(self) -> bool:
        """Check if circuit breaker is open."""
        return self._state is not None and self._state.status == CircuitBreakerStatus.OPEN

    @property
    def is_closed(self) -> bool:
        """Check if circuit breaker is closed."""
        return self._state is None or self._state.status == CircuitBreakerStatus.CLOSED


class CircuitBreakerManager:
    """Manages circuit breakers for multiple sessions."""

    def __init__(self, database: Database | None = None) -> None:
        """Initialize manager.

        Args:
            database: Database instance.
        """
        self._db = database or get_database()
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = asyncio.Lock()

    async def get_breaker(
        self,
        session_id: str,
        project_id: str = "default",
        config: BudgetConfig | None = None,
    ) -> CircuitBreaker:
        """Get or create circuit breaker for session.

        Args:
            session_id: Session identifier.
            project_id: Project identifier.
            config: Budget configuration.

        Returns:
            CircuitBreaker instance.
        """
        key = f"{project_id}:{session_id}"

        async with self._lock:
            if key not in self._breakers:
                breaker = CircuitBreaker(
                    session_id=session_id,
                    project_id=project_id,
                    config=config,
                    database=self._db,
                )
                await breaker.initialize()
                self._breakers[key] = breaker

            return self._breakers[key]

    async def remove_breaker(self, session_id: str, project_id: str = "default") -> None:
        """Remove circuit breaker for session.

        Args:
            session_id: Session identifier.
            project_id: Project identifier.
        """
        key = f"{project_id}:{session_id}"

        async with self._lock:
            if key in self._breakers:
                del self._breakers[key]

    def clear_all(self) -> None:
        """Clear all circuit breakers."""
        self._breakers.clear()


# Global manager instance
_manager: CircuitBreakerManager | None = None


def get_circuit_breaker_manager(database: Database | None = None) -> CircuitBreakerManager:
    """Get or create global circuit breaker manager."""
    global _manager
    if _manager is None:
        _manager = CircuitBreakerManager(database)
    return _manager


def reset_circuit_breaker_manager() -> None:
    """Reset global manager (for testing)."""
    global _manager
    _manager = None
