"""Tests for core data models."""

from decimal import Decimal

import pytest

from costguard.models import (
    AlertChannel,
    AlertEvent,
    BudgetConfig,
    BudgetWindow,
    CircuitBreakerState,
    CircuitBreakerStatus,
    CostEstimateRequest,
    DashboardMetrics,
    LimitExceededError,
    Provider,
    ProviderPricing,
    ProxyRequest,
    SpendRecord,
)


class TestBudgetConfig:
    """Tests for BudgetConfig model."""

    def test_default_values(self) -> None:
        """Test default budget configuration values."""
        config = BudgetConfig()
        assert config.session_limit == Decimal("10.00")
        assert config.hour_limit == Decimal("50.00")
        assert config.day_limit == Decimal("200.00")
        assert config.project_limit == Decimal("1000.00")
        assert config.safe_mode_threshold == Decimal("5.00")
        assert config.alert_channels == [AlertChannel.CONSOLE]
        assert config.webhook_url is None

    def test_custom_values(self) -> None:
        """Test custom budget configuration."""
        config = BudgetConfig(
            session_limit=Decimal("5.00"),
            hour_limit=Decimal("25.00"),
            day_limit=Decimal("100.00"),
            project_limit=Decimal("500.00"),
            safe_mode_threshold=Decimal("2.50"),
            alert_channels=[AlertChannel.CONSOLE, AlertChannel.WEBHOOK],
            webhook_url="https://example.com/webhook",
        )
        assert config.session_limit == Decimal("5.00")
        assert config.hour_limit == Decimal("25.00")
        assert config.alert_channels == [AlertChannel.CONSOLE, AlertChannel.WEBHOOK]
        assert config.webhook_url == "https://example.com/webhook"

    def test_webhook_validation(self) -> None:
        """Test webhook URL validation."""
        # Should raise error when webhook channel enabled but no URL
        with pytest.raises(ValueError):
            BudgetConfig(
                alert_channels=[AlertChannel.WEBHOOK],
                webhook_url=None,
            )


class TestProviderPricing:
    """Tests for ProviderPricing model."""

    def test_estimate_cost(self) -> None:
        """Test cost estimation."""
        pricing = ProviderPricing(
            provider=Provider.OPENAI,
            model_id="gpt-4o",
            model_name="GPT-4o",
            input_price_per_mtok=Decimal("2.50"),
            output_price_per_mtok=Decimal("10.00"),
            context_window=128000,
        )

        # Test with 1000 input tokens, 500 output tokens
        cost = pricing.estimate_cost(1000, 500)
        # Expected: (1000 * 2.50 / 1M) + (500 * 10.00 / 1M) = 0.0025 + 0.005 = 0.0075
        expected = Decimal("0.0075")
        assert cost == expected

    def test_estimate_cost_zero_tokens(self) -> None:
        """Test cost estimation with zero tokens."""
        pricing = ProviderPricing(
            provider=Provider.OPENAI,
            model_id="gpt-4o",
            model_name="GPT-4o",
            input_price_per_mtok=Decimal("2.50"),
            output_price_per_mtok=Decimal("10.00"),
            context_window=128000,
        )

        cost = pricing.estimate_cost(0, 0)
        assert cost == Decimal("0.00")


class TestSpendRecord:
    """Tests for SpendRecord model."""

    def test_total_tokens_computed(self) -> None:
        """Test that total_tokens is computed from input + output."""
        record = SpendRecord(
            request_id="req-123",
            session_id="session-456",
            provider=Provider.OPENAI,
            model_id="gpt-4o",
            input_tokens=100,
            output_tokens=50,
            estimated_cost=Decimal("0.01"),
        )
        assert record.total_tokens == 150

    def test_model_dump(self) -> None:
        """Test model serialization."""
        record = SpendRecord(
            request_id="req-123",
            session_id="session-456",
            provider=Provider.OPENAI,
            model_id="gpt-4o",
            input_tokens=100,
            output_tokens=50,
            estimated_cost=Decimal("0.01"),
        )

        data = record.model_dump()
        assert data["request_id"] == "req-123"
        assert data["session_id"] == "session-456"
        assert data["provider"] == "openai"
        assert data["total_tokens"] == 150


class TestCircuitBreakerState:
    """Tests for CircuitBreakerState model."""

    def test_update_spend(self) -> None:
        """Test spend update."""
        state = CircuitBreakerState(
            session_id="session-123",
            project_id="project-456",
        )

        initial_requests = state.total_requests
        state.update_spend(Decimal("1.50"))

        assert state.session_spend == Decimal("1.50")
        assert state.hour_spend == Decimal("1.50")
        assert state.day_spend == Decimal("1.50")
        assert state.project_spend == Decimal("1.50")
        assert state.total_requests == initial_requests + 1
        assert state.last_request_time is not None

    def test_increment_blocked(self) -> None:
        """Test blocked request increment."""
        state = CircuitBreakerState(
            session_id="session-123",
            project_id="project-456",
        )

        assert state.blocked_requests == 0
        state.increment_blocked()
        assert state.blocked_requests == 1

    def test_check_limits(self) -> None:
        """Test limit checking."""
        config = BudgetConfig(
            session_limit=Decimal("10.00"),
            hour_limit=Decimal("50.00"),
            day_limit=Decimal("200.00"),
            project_limit=Decimal("1000.00"),
        )

        state = CircuitBreakerState(
            session_id="session-123",
            project_id="project-456",
            session_spend=Decimal("15.00"),
            hour_spend=Decimal("60.00"),
        )

        exceeded = state.check_limits(config)
        assert BudgetWindow.SESSION in exceeded
        assert BudgetWindow.HOUR in exceeded


class TestLimitExceededError:
    """Tests for LimitExceededError model."""

    def test_from_state(self) -> None:
        """Test creating error from state."""
        config = BudgetConfig()
        state = CircuitBreakerState(
            session_id="session-123",
            project_id="project-456",
            session_spend=Decimal("15.00"),
        )

        error = LimitExceededError.from_state(
            state=state,
            config=config,
            exceeded=[BudgetWindow.SESSION],
            request_id="req-789",
        )

        assert error.error_type == "limit_exceeded"
        assert BudgetWindow.SESSION in error.exceeded_limits
        assert error.session_id == "session-123"
        assert error.request_id == "req-789"
        assert "session" in error.current_spend
        assert "session" in error.limits


class TestProxyRequest:
    """Tests for ProxyRequest model."""

    def test_valid_messages(self) -> None:
        """Test valid message validation."""
        request = ProxyRequest(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "Hello"},
            ],
            max_tokens=100,
        )
        assert len(request.messages) == 2

    def test_empty_messages(self) -> None:
        """Test empty messages validation."""
        with pytest.raises(ValueError, match="messages cannot be empty"):
            ProxyRequest(
                model="gpt-4o",
                messages=[],
            )

    def test_missing_role(self) -> None:
        """Test missing role validation."""
        with pytest.raises(ValueError, match="role"):
            ProxyRequest(
                model="gpt-4o",
                messages=[{"content": "Hello"}],
            )

    def test_invalid_role(self) -> None:
        """Test invalid role validation."""
        with pytest.raises(ValueError, match="invalid role"):
            ProxyRequest(
                model="gpt-4o",
                messages=[{"role": "invalid", "content": "Hello"}],
            )


class TestDashboardMetrics:
    """Tests for DashboardMetrics model."""

    def test_percentages(self) -> None:
        """Test percentage calculations."""
        metrics = DashboardMetrics(
            session_id="session-123",
            project_id="project-456",
            status=CircuitBreakerStatus.CLOSED,
            session_spend=Decimal("5.00"),
            hour_spend=Decimal("25.00"),
            day_spend=Decimal("100.00"),
            project_spend=Decimal("500.00"),
            session_limit=Decimal("10.00"),
            hour_limit=Decimal("50.00"),
            day_limit=Decimal("200.00"),
            project_limit=Decimal("1000.00"),
            total_requests=100,
            blocked_requests=5,
        )

        assert metrics.session_percentage == 50.0
        assert metrics.hour_percentage == 50.0
        assert metrics.day_percentage == 50.0
        assert metrics.project_percentage == 50.0

    def test_zero_limit_percentage(self) -> None:
        """Test percentage with zero limit."""
        metrics = DashboardMetrics(
            session_id="session-123",
            project_id="project-456",
            status=CircuitBreakerStatus.CLOSED,
            session_spend=Decimal("5.00"),
            hour_spend=Decimal("0.00"),
            day_spend=Decimal("0.00"),
            project_spend=Decimal("0.00"),
            session_limit=Decimal("0.00"),
            hour_limit=Decimal("50.00"),
            day_limit=Decimal("200.00"),
            project_limit=Decimal("1000.00"),
            total_requests=0,
            blocked_requests=0,
        )

        assert metrics.session_percentage == 0.0


class TestCostEstimateRequest:
    """Tests for CostEstimateRequest model."""

    def test_basic_request(self) -> None:
        """Test basic cost estimate request."""
        request = CostEstimateRequest(
            provider=Provider.OPENAI,
            model_id="gpt-4o",
            messages=[
                {"role": "user", "content": "Hello"},
            ],
            estimated_output_tokens=500,
        )
        assert request.provider == Provider.OPENAI
        assert request.model_id == "gpt-4o"
        assert request.estimated_output_tokens == 500


class TestAlertEvent:
    """Tests for AlertEvent model."""

    def test_alert_creation(self) -> None:
        """Test alert event creation."""
        alert = AlertEvent(
            alert_type="limit_exceeded",
            severity="critical",
            session_id="session-123",
            project_id="project-456",
            exceeded_limits=[BudgetWindow.SESSION],
            current_spend={"session": "15.00"},
            message="Session limit exceeded",
        )
        assert alert.alert_type == "limit_exceeded"
        assert alert.severity == "critical"
        assert alert.acknowledged is False
