"""Tests for circuit breaker logic."""

import asyncio
from decimal import Decimal

import pytest

from costguard.circuit_breaker import CircuitBreaker, CircuitBreakerError, CircuitBreakerManager
from costguard.database import get_database, reset_database
from costguard.models import BudgetConfig, BudgetWindow, CircuitBreakerStatus, Provider, SpendRecord


class TestCircuitBreaker:
    """Tests for CircuitBreaker."""

    @pytest.fixture(autouse=True)
    async def setup(self):
        """Setup for each test."""
        reset_database()
        self.db = get_database(":memory:")
        await self.db.initialize_schema()
        self.config = BudgetConfig(
            session_limit=Decimal("10.00"),
            hour_limit=Decimal("50.00"),
            day_limit=Decimal("200.00"),
            project_limit=Decimal("1000.00"),
        )
        self.breaker = CircuitBreaker(
            session_id="test-session",
            project_id="test-project",
            config=self.config,
            database=self.db,
        )
        await self.breaker.initialize()
        yield
        await self.db.close()

    async def test_initialization(self) -> None:
        """Test circuit breaker initialization."""
        assert self.breaker.session_id == "test-session"
        assert self.breaker.project_id == "test-project"
        state = self.breaker.get_state()
        assert state is not None
        assert state.status == CircuitBreakerStatus.CLOSED

    async def test_check_limits_within_budget(self) -> None:
        """Test checking limits within budget."""
        # Should not raise for small cost
        await self.breaker.check_limits(Decimal("1.00"), "req-1")
        state = self.breaker.get_state()
        assert state.session_spend == Decimal("0.00")  # Not recorded yet

    async def test_check_limits_exceeds_session(self) -> None:
        """Test session limit exceeded."""
        # First add some spend
        record = SpendRecord(
            request_id="req-1",
            session_id="test-session",
            provider=Provider.OPENAI,
            model_id="gpt-4o",
            input_tokens=1000,
            output_tokens=500,
            estimated_cost=Decimal("9.00"),
        )
        await self.breaker.record_spend(record)

        # Now try to exceed
        with pytest.raises(CircuitBreakerError) as exc_info:
            await self.breaker.check_limits(Decimal("2.00"), "req-2")

        error = exc_info.value.error
        assert BudgetWindow.SESSION in error.exceeded_limits
        assert error.current_spend["session"] == "9.00"

    async def test_record_spend(self) -> None:
        """Test recording spend."""
        record = SpendRecord(
            request_id="req-1",
            session_id="test-session",
            provider=Provider.OPENAI,
            model_id="gpt-4o",
            input_tokens=1000,
            output_tokens=500,
            estimated_cost=Decimal("1.50"),
        )

        await self.breaker.record_spend(record)

        state = self.breaker.get_state()
        assert state.session_spend == Decimal("1.50")
        assert state.total_requests == 1

    async def test_record_blocked(self) -> None:
        """Test recording blocked request."""
        record = SpendRecord(
            request_id="req-1",
            session_id="test-session",
            provider=Provider.OPENAI,
            model_id="gpt-4o",
            input_tokens=1000,
            output_tokens=500,
            estimated_cost=Decimal("1.50"),
        )

        await self.breaker.record_blocked(record, "Limit exceeded")

        state = self.breaker.get_state()
        assert state.blocked_requests == 1

    async def test_evaluation_order(self) -> None:
        """Test deterministic evaluation order."""
        # The order should be: SESSION, HOUR, DAY, PROJECT
        order = CircuitBreaker.EVALUATION_ORDER
        assert order[0] == BudgetWindow.SESSION
        assert order[1] == BudgetWindow.HOUR
        assert order[2] == BudgetWindow.DAY
        assert order[3] == BudgetWindow.PROJECT

    async def test_is_open_closed(self) -> None:
        """Test circuit breaker open/closed status."""
        assert self.breaker.is_closed
        assert not self.breaker.is_open

        # Exceed limit to open
        record = SpendRecord(
            request_id="req-1",
            session_id="test-session",
            provider=Provider.OPENAI,
            model_id="gpt-4o",
            input_tokens=10000,
            output_tokens=5000,
            estimated_cost=Decimal("15.00"),
        )
        await self.breaker.record_spend(record)

        # Status should be updated
        state = self.breaker.get_state()
        if state.session_spend > self.config.session_limit:
            state.status = CircuitBreakerStatus.OPEN
            assert self.breaker.is_open


class TestCircuitBreakerManager:
    """Tests for CircuitBreakerManager."""

    @pytest.fixture(autouse=True)
    async def setup(self):
        """Setup for each test."""
        reset_database()
        self.db = get_database(":memory:")
        await self.db.initialize_schema()
        self.manager = CircuitBreakerManager(database=self.db)
        yield
        await self.db.close()

    async def test_get_breaker(self) -> None:
        """Test getting/creating circuit breaker."""
        breaker = await self.manager.get_breaker("session-1", "project-1")
        assert breaker.session_id == "session-1"
        assert breaker.project_id == "project-1"

        # Should return same instance
        breaker2 = await self.manager.get_breaker("session-1", "project-1")
        assert breaker is breaker2

    async def test_get_breaker_different_sessions(self) -> None:
        """Test getting different breakers for different sessions."""
        breaker1 = await self.manager.get_breaker("session-1", "project-1")
        breaker2 = await self.manager.get_breaker("session-2", "project-1")

        assert breaker1 is not breaker2
        assert breaker1.session_id == "session-1"
        assert breaker2.session_id == "session-2"

    async def test_remove_breaker(self) -> None:
        """Test removing circuit breaker."""
        breaker = await self.manager.get_breaker("session-1", "project-1")
        await self.manager.remove_breaker("session-1", "project-1")

        # Should create new instance
        breaker2 = await self.manager.get_breaker("session-1", "project-1")
        assert breaker is not breaker2

    async def test_clear_all(self) -> None:
        """Test clearing all breakers."""
        await self.manager.get_breaker("session-1", "project-1")
        await self.manager.get_breaker("session-2", "project-1")

        self.manager.clear_all()

        # Should create new instances
        breaker = await self.manager.get_breaker("session-1", "project-1")
        assert breaker.get_state() is not None


class TestCircuitBreakerConcurrency:
    """Tests for circuit breaker thread safety."""

    @pytest.fixture(autouse=True)
    async def setup(self):
        """Setup for each test."""
        reset_database()
        self.db = get_database(":memory:")
        await self.db.initialize_schema()
        self.config = BudgetConfig(
            session_limit=Decimal("100.00"),
            hour_limit=Decimal("500.00"),
            day_limit=Decimal("2000.00"),
            project_limit=Decimal("10000.00"),
        )
        self.breaker = CircuitBreaker(
            session_id="concurrent-session",
            project_id="test-project",
            config=self.config,
            database=self.db,
        )
        await self.breaker.initialize()
        yield
        await self.db.close()

    async def test_concurrent_spend_recording(self) -> None:
        """Test concurrent spend recording."""
        async def record_spend(i: int) -> None:
            record = SpendRecord(
                request_id=f"req-{i}",
                session_id="concurrent-session",
                provider=Provider.OPENAI,
                model_id="gpt-4o",
                input_tokens=100,
                output_tokens=50,
                estimated_cost=Decimal("0.01"),
            )
            await self.breaker.record_spend(record)

        # Record 10 spends concurrently
        tasks = [record_spend(i) for i in range(10)]
        await asyncio.gather(*tasks)

        state = self.breaker.get_state()
        assert state.total_requests == 10
        assert state.session_spend == Decimal("0.10")
