"""Tests for database operations."""

import asyncio
from decimal import Decimal

import pytest

from costguard.database import get_database, reset_database
from costguard.models import (
    AlertEvent,
    BudgetConfig,
    BudgetWindow,
    CircuitBreakerStatus,
    Provider,
    ProviderPricing,
    SpendRecord,
)


class TestDatabase:
    """Tests for Database class."""

    @pytest.fixture(autouse=True)
    async def setup(self):
        """Setup for each test."""
        reset_database()
        self.db = get_database(":memory:")
        await self.db.initialize_schema()
        yield
        await self.db.close()

    async def test_initialize_schema(self) -> None:
        """Test schema initialization."""
        # Schema should be created without errors
        await self.db.initialize_schema()
        assert self.db._connection is not None

    async def test_save_and_get_spend_record(self) -> None:
        """Test saving and retrieving spend records."""
        record = SpendRecord(
            request_id="req-123",
            session_id="session-456",
            project_id="project-789",
            provider=Provider.OPENAI,
            model_id="gpt-4o",
            input_tokens=1000,
            output_tokens=500,
            estimated_cost=Decimal("0.01"),
            status="completed",
        )

        await self.db.save_spend_record(record)

        # Retrieve records
        records = await self.db.get_spend_records(session_id="session-456")
        assert len(records) == 1
        assert records[0].request_id == "req-123"
        assert records[0].input_tokens == 1000

    async def test_get_spend_records_filtering(self) -> None:
        """Test spend record filtering."""
        # Create multiple records
        for i in range(5):
            record = SpendRecord(
                request_id=f"req-{i}",
                session_id="session-1" if i < 3 else "session-2",
                project_id="project-1",
                provider=Provider.OPENAI,
                model_id="gpt-4o",
                input_tokens=100,
                output_tokens=50,
                estimated_cost=Decimal("0.001"),
            )
            await self.db.save_spend_record(record)

        # Filter by session
        records = await self.db.get_spend_records(session_id="session-1")
        assert len(records) == 3

        # Filter by project
        records = await self.db.get_spend_records(project_id="project-1")
        assert len(records) == 5

    async def test_circuit_breaker_state(self) -> None:
        """Test circuit breaker state operations."""
        # Get or create state
        state = await self.db.get_or_create_circuit_breaker_state(
            session_id="test-session",
            project_id="test-project",
        )

        assert state.session_id == "test-session"
        assert state.project_id == "test-project"
        assert state.status == CircuitBreakerStatus.CLOSED

        # Update state
        state.session_spend = Decimal("10.00")
        state.total_requests = 5
        await self.db.save_circuit_breaker_state(state)

        # Retrieve updated state
        state2 = await self.db.get_or_create_circuit_breaker_state(
            session_id="test-session",
            project_id="test-project",
        )
        assert state2.session_spend == Decimal("10.00")
        assert state2.total_requests == 5

    async def test_alert_events(self) -> None:
        """Test alert event operations."""
        event = AlertEvent(
            alert_type="limit_exceeded",
            severity="critical",
            session_id="test-session",
            project_id="test-project",
            exceeded_limits=[BudgetWindow.SESSION],
            current_spend={"session": "15.00"},
            message="Session limit exceeded",
        )

        await self.db.save_alert_event(event)

        # Retrieve alerts
        alerts = await self.db.get_recent_alerts(session_id="test-session")
        assert len(alerts) == 1
        assert alerts[0].alert_type == "limit_exceeded"
        assert alerts[0].severity == "critical"

    async def test_provider_pricing(self) -> None:
        """Test provider pricing operations."""
        pricing = ProviderPricing(
            provider=Provider.OPENAI,
            model_id="test-model",
            model_name="Test Model",
            input_price_per_mtok=Decimal("1.00"),
            output_price_per_mtok=Decimal("2.00"),
            context_window=8000,
        )

        await self.db.save_provider_pricing(pricing)

        # Retrieve pricing
        pricings = await self.db.get_provider_pricing(model_id="test-model")
        assert len(pricings) == 1
        assert pricings[0].model_id == "test-model"
        assert pricings[0].input_price_per_mtok == Decimal("1.00")

    async def test_budget_config(self) -> None:
        """Test budget configuration operations."""
        config = BudgetConfig(
            session_limit=Decimal("5.00"),
            hour_limit=Decimal("25.00"),
            day_limit=Decimal("100.00"),
            project_limit=Decimal("500.00"),
        )

        await self.db.save_budget_config("test-project", config)

        # Retrieve config
        retrieved = await self.db.get_budget_config("test-project")
        assert retrieved.session_limit == Decimal("5.00")
        assert retrieved.hour_limit == Decimal("25.00")

    async def test_get_session_spend(self) -> None:
        """Test session spend aggregation."""
        # Create records
        for i in range(3):
            record = SpendRecord(
                request_id=f"req-{i}",
                session_id="session-1",
                project_id="project-1",
                provider=Provider.OPENAI,
                model_id="gpt-4o",
                input_tokens=100,
                output_tokens=50,
                estimated_cost=Decimal("1.00"),
                status="completed",
            )
            await self.db.save_spend_record(record)

        # Get session spend
        spend = await self.db.get_session_spend("session-1")
        assert spend == Decimal("3.00")

    async def test_get_hourly_spend(self) -> None:
        """Test hourly spend aggregation."""
        # Create recent record
        record = SpendRecord(
            request_id="req-1",
            session_id="session-1",
            project_id="project-1",
            provider=Provider.OPENAI,
            model_id="gpt-4o",
            input_tokens=100,
            output_tokens=50,
            estimated_cost=Decimal("5.00"),
            status="completed",
        )
        await self.db.save_spend_record(record)

        # Get hourly spend
        spend = await self.db.get_hourly_spend(project_id="project-1")
        assert spend == Decimal("5.00")

    async def test_get_daily_spend(self) -> None:
        """Test daily spend aggregation."""
        # Create record
        record = SpendRecord(
            request_id="req-1",
            session_id="session-1",
            project_id="project-1",
            provider=Provider.OPENAI,
            model_id="gpt-4o",
            input_tokens=100,
            output_tokens=50,
            estimated_cost=Decimal("10.00"),
            status="completed",
        )
        await self.db.save_spend_record(record)

        # Get daily spend
        spend = await self.db.get_daily_spend(project_id="project-1")
        assert spend == Decimal("10.00")

    async def test_get_project_spend(self) -> None:
        """Test project spend aggregation."""
        # Create records for different sessions
        for i in range(3):
            record = SpendRecord(
                request_id=f"req-{i}",
                session_id=f"session-{i}",
                project_id="project-1",
                provider=Provider.OPENAI,
                model_id="gpt-4o",
                input_tokens=100,
                output_tokens=50,
                estimated_cost=Decimal("2.00"),
                status="completed",
            )
            await self.db.save_spend_record(record)

        # Get project spend
        spend = await self.db.get_project_spend("project-1")
        assert spend == Decimal("6.00")


class TestDatabaseConcurrency:
    """Tests for database concurrency."""

    @pytest.fixture(autouse=True)
    async def setup(self):
        """Setup for each test."""
        reset_database()
        self.db = get_database(":memory:")
        await self.db.initialize_schema()
        yield
        await self.db.close()

    async def test_concurrent_spend_records(self) -> None:
        """Test concurrent spend record insertion."""
        async def insert_record(i: int) -> None:
            record = SpendRecord(
                request_id=f"req-{i}",
                session_id="session-1",
                project_id="project-1",
                provider=Provider.OPENAI,
                model_id="gpt-4o",
                input_tokens=100,
                output_tokens=50,
                estimated_cost=Decimal("0.01"),
            )
            await self.db.save_spend_record(record)

        # Insert 10 records concurrently
        tasks = [insert_record(i) for i in range(10)]
        await asyncio.gather(*tasks)

        # Verify all records saved
        records = await self.db.get_spend_records(session_id="session-1")
        assert len(records) == 10
