"""Tests for alert system."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from costguard.alerts import (
    AlertManager,
    ConsoleAlertHandler,
    FileAlertHandler,
    WebhookAlertHandler,
    get_alert_manager,
    reset_alert_manager,
)
from costguard.models import AlertChannel, AlertEvent, BudgetConfig, BudgetWindow


class TestConsoleAlertHandler:
    """Tests for ConsoleAlertHandler."""

    def setup_method(self) -> None:
        """Setup for each test."""
        from rich.console import Console

        self.console = Console()
        self.handler = ConsoleAlertHandler(console=self.console)
        self.config = BudgetConfig()

    async def test_send_alert(self) -> None:
        """Test sending console alert."""
        event = AlertEvent(
            alert_type="limit_exceeded",
            severity="critical",
            session_id="test-session",
            project_id="test-project",
            exceeded_limits=[BudgetWindow.SESSION],
            current_spend={"session": "15.00"},
            message="Session limit exceeded",
        )

        result = await self.handler.send_alert(event, self.config)
        assert result is True

    async def test_send_alert_info(self) -> None:
        """Test sending info alert."""
        event = AlertEvent(
            alert_type="limit_warning",
            severity="info",
            session_id="test-session",
            project_id="test-project",
            message="Approaching limit",
        )

        result = await self.handler.send_alert(event, self.config)
        assert result is True


class TestWebhookAlertHandler:
    """Tests for WebhookAlertHandler."""

    def setup_method(self) -> None:
        """Setup for each test."""
        self.handler = WebhookAlertHandler(timeout=5.0)
        self.config = BudgetConfig(
            webhook_url="https://example.com/webhook",
        )

    @pytest.mark.asyncio
    async def test_send_alert_no_webhook_url(self) -> None:
        """Test sending alert without webhook URL."""
        config = BudgetConfig(webhook_url=None)
        event = AlertEvent(
            alert_type="limit_exceeded",
            severity="critical",
            session_id="test-session",
            project_id="test-project",
            message="Test message",
        )

        result = await self.handler.send_alert(event, config)
        assert result is False

    @pytest.mark.asyncio
    async def test_send_alert_success(self) -> None:
        """Test successful webhook alert."""
        event = AlertEvent(
            alert_type="limit_exceeded",
            severity="critical",
            session_id="test-session",
            project_id="test-project",
            exceeded_limits=[BudgetWindow.SESSION],
            current_spend={"session": "15.00"},
            message="Session limit exceeded",
        )

        # Mock the HTTP client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "OK"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.object(self.handler, "_get_client", return_value=mock_client):
            result = await self.handler.send_alert(event, self.config)
            assert result is True

    @pytest.mark.asyncio
    async def test_send_alert_failure(self) -> None:
        """Test failed webhook alert."""
        event = AlertEvent(
            alert_type="limit_exceeded",
            severity="critical",
            session_id="test-session",
            project_id="test-project",
            message="Test message",
        )

        # Mock failed response
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.object(self.handler, "_get_client", return_value=mock_client):
            result = await self.handler.send_alert(event, self.config)
            assert result is False


class TestFileAlertHandler:
    """Tests for FileAlertHandler."""

    def setup_method(self) -> None:
        """Setup for each test."""
        import tempfile

        self.temp_dir = tempfile.mkdtemp()
        self.handler = FileAlertHandler(log_dir=self.temp_dir)
        self.config = BudgetConfig()

    async def test_send_alert(self) -> None:
        """Test writing alert to file."""
        event = AlertEvent(
            alert_type="limit_exceeded",
            severity="critical",
            session_id="test-session",
            project_id="test-project",
            exceeded_limits=[BudgetWindow.SESSION],
            current_spend={"session": "15.00"},
            message="Session limit exceeded",
        )

        result = await self.handler.send_alert(event, self.config)
        assert result is True


class TestAlertManager:
    """Tests for AlertManager."""

    def setup_method(self) -> None:
        """Setup for each test."""
        reset_alert_manager()
        self.config = BudgetConfig(
            alert_channels=[AlertChannel.CONSOLE],
        )
        self.manager = AlertManager(config=self.config)

    async def test_send_alert(self) -> None:
        """Test sending alert through manager."""
        event = AlertEvent(
            alert_type="limit_exceeded",
            severity="critical",
            session_id="test-session",
            project_id="test-project",
            exceeded_limits=[BudgetWindow.SESSION],
            current_spend={"session": "15.00"},
            message="Session limit exceeded",
        )

        results = await self.manager.send_alert(event)
        assert AlertChannel.CONSOLE in results

    async def test_send_limit_warning(self) -> None:
        """Test sending limit warning."""
        await self.manager.send_limit_warning(
            session_id="test-session",
            project_id="test-project",
            window=BudgetWindow.SESSION,
            current_spend={"session": "9.00"},
            threshold_percentage=90.0,
        )

        # Should complete without error

    async def test_send_safe_mode_triggered(self) -> None:
        """Test sending safe mode triggered alert."""
        await self.manager.send_safe_mode_triggered(
            session_id="test-session",
            project_id="test-project",
            estimated_cost="10.00",
            threshold="5.00",
        )

        # Should complete without error


class TestStructuredLogger:
    """Tests for StructuredLogger."""

    def setup_method(self) -> None:
        """Setup for each test."""
        from costguard.alerts import StructuredLogger

        self.logger = StructuredLogger()

    def test_log_request_start(self) -> None:
        """Test logging request start."""
        self.logger.log_request_start(
            request_id="req-1",
            session_id="session-1",
            model_id="gpt-4o",
            estimated_cost="0.01",
        )
        # Should complete without error

    def test_log_request_complete(self) -> None:
        """Test logging request completion."""
        self.logger.log_request_complete(
            request_id="req-1",
            session_id="session-1",
            model_id="gpt-4o",
            actual_cost="0.015",
            duration_ms=1500.0,
        )
        # Should complete without error

    def test_log_request_blocked(self) -> None:
        """Test logging blocked request."""
        self.logger.log_request_blocked(
            request_id="req-1",
            session_id="session-1",
            model_id="gpt-4o",
            reason="Limit exceeded",
            exceeded_limits=["session"],
        )
        # Should complete without error

    def test_log_limit_exceeded(self) -> None:
        """Test logging limit exceeded."""
        self.logger.log_limit_exceeded(
            session_id="session-1",
            exceeded_limits=["session"],
            current_spend={"session": "15.00"},
        )
        # Should complete without error


class TestGetAlertManager:
    """Tests for get_alert_manager function."""

    def setup_method(self) -> None:
        """Setup for each test."""
        reset_alert_manager()

    def test_get_alert_manager_singleton(self) -> None:
        """Test that get_alert_manager returns singleton."""
        manager1 = get_alert_manager()
        manager2 = get_alert_manager()
        assert manager1 is manager2

    def test_get_alert_manager_with_config(self) -> None:
        """Test getting manager with custom config."""
        config = BudgetConfig(
            session_limit=Decimal("5.00"),
        )
        manager = get_alert_manager(config)
        assert manager.config.session_limit == Decimal("5.00")
