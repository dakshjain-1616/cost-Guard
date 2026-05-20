"""Tests for safe mode functionality."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from costguard.models import BudgetConfig
from costguard.safe_mode import PendingConfirmation, SafeModeManager


class TestSafeModeManager:
    """Tests for SafeModeManager."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup for each test."""
        self.config = BudgetConfig(
            safe_mode_threshold=Decimal("5.00"),
        )
        self.manager = SafeModeManager(config=self.config)

    async def test_check_request_below_threshold(self) -> None:
        """Test request below safe mode threshold."""
        result = await self.manager.check_request(
            request_id="req-1",
            session_id="session-1",
            estimated_cost=Decimal("3.00"),
        )

        assert result.requires_confirmation is False
        assert result.confirmed is True
        assert result.estimated_cost == Decimal("3.00")
        assert result.threshold == Decimal("5.00")

    async def test_check_request_above_threshold(self) -> None:
        """Test request above safe mode threshold."""
        result = await self.manager.check_request(
            request_id="req-1",
            session_id="session-1",
            estimated_cost=Decimal("10.00"),
        )

        assert result.requires_confirmation is True
        assert result.confirmed is False
        assert result.estimated_cost == Decimal("10.00")
        assert result.threshold == Decimal("5.00")
        assert result.expires_at is not None

    async def test_confirm_request(self) -> None:
        """Test confirming a request."""
        # First create a pending request
        await self.manager.check_request(
            request_id="req-1",
            session_id="session-1",
            estimated_cost=Decimal("10.00"),
        )

        # Confirm it
        result = await self.manager.confirm_request(
            request_id="req-1",
            session_id="session-1",
            confirmed=True,
        )

        assert result.requires_confirmation is True
        assert result.confirmed is True
        assert result.estimated_cost == Decimal("10.00")

    async def test_reject_request(self) -> None:
        """Test rejecting a request."""
        # First create a pending request
        await self.manager.check_request(
            request_id="req-1",
            session_id="session-1",
            estimated_cost=Decimal("10.00"),
        )

        # Reject it
        result = await self.manager.confirm_request(
            request_id="req-1",
            session_id="session-1",
            confirmed=False,
        )

        assert result.requires_confirmation is True
        assert result.confirmed is False

    async def test_is_confirmed(self) -> None:
        """Test checking if request is confirmed."""
        # Create pending request
        await self.manager.check_request(
            request_id="req-1",
            session_id="session-1",
            estimated_cost=Decimal("10.00"),
        )

        # Not confirmed yet
        is_confirmed = await self.manager.is_confirmed("req-1")
        assert is_confirmed is False

        # Confirm it
        await self.manager.confirm_request(
            request_id="req-1",
            session_id="session-1",
            confirmed=True,
        )

        # Now confirmed
        is_confirmed = await self.manager.is_confirmed("req-1")
        assert is_confirmed is True

    async def test_confirm_nonexistent_request(self) -> None:
        """Test confirming a request that doesn't exist."""
        result = await self.manager.confirm_request(
            request_id="nonexistent",
            session_id="session-1",
            confirmed=True,
        )

        assert result.requires_confirmation is False
        assert result.confirmed is False

    async def test_cleanup_expired(self) -> None:
        """Test cleaning up expired confirmations."""
        # Create a pending request with very short timeout
        self.manager.CONFIRMATION_TIMEOUT_SECONDS = 0.01

        await self.manager.check_request(
            request_id="req-1",
            session_id="session-1",
            estimated_cost=Decimal("10.00"),
        )

        assert self.manager.get_pending_count() == 1

        # Wait for expiration
        await asyncio.sleep(0.1)

        # Cleanup
        cleaned = await self.manager.cleanup_expired()
        assert cleaned == 1
        assert self.manager.get_pending_count() == 0

    async def test_get_pending_requests(self) -> None:
        """Test getting pending requests."""
        # Create multiple pending requests
        for i in range(3):
            await self.manager.check_request(
                request_id=f"req-{i}",
                session_id="session-1",
                estimated_cost=Decimal("10.00"),
            )

        pending = self.manager.get_pending_requests()
        assert len(pending) == 3

        # Confirm one
        await self.manager.confirm_request(
            request_id="req-0",
            session_id="session-1",
            confirmed=True,
        )

        # Should still show as pending (confirmed but not removed)
        pending = self.manager.get_pending_requests()
        assert len(pending) == 2  # req-1 and req-2


class TestPendingConfirmation:
    """Tests for PendingConfirmation."""

    def test_is_expired(self) -> None:
        """Test expiration check."""
        # Create expired confirmation
        expired = PendingConfirmation(
            request_id="req-1",
            session_id="session-1",
            estimated_cost=Decimal("10.00"),
            threshold=Decimal("5.00"),
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )

        assert expired.is_expired() is True

        # Create non-expired confirmation
        not_expired = PendingConfirmation(
            request_id="req-2",
            session_id="session-1",
            estimated_cost=Decimal("10.00"),
            threshold=Decimal("5.00"),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

        assert not_expired.is_expired() is False

    def test_confirm(self) -> None:
        """Test confirmation."""
        pending = PendingConfirmation(
            request_id="req-1",
            session_id="session-1",
            estimated_cost=Decimal("10.00"),
            threshold=Decimal("5.00"),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

        assert pending.confirmed is None
        assert pending.confirmed_at is None

        pending.confirm(True)

        assert pending.confirmed is True
        assert pending.confirmed_at is not None


class TestSafeModeConcurrency:
    """Tests for safe mode concurrency."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup for each test."""
        self.config = BudgetConfig(
            safe_mode_threshold=Decimal("5.00"),
        )
        self.manager = SafeModeManager(config=self.config)

    async def test_concurrent_check_requests(self) -> None:
        """Test concurrent check requests."""
        async def check_request(i: int) -> None:
            await self.manager.check_request(
                request_id=f"req-{i}",
                session_id="session-1",
                estimated_cost=Decimal("10.00"),
            )

        # Create 10 pending requests concurrently
        tasks = [check_request(i) for i in range(10)]
        await asyncio.gather(*tasks)

        assert self.manager.get_pending_count() == 10

    async def test_concurrent_confirmations(self) -> None:
        """Test concurrent confirmations."""
        # Create pending requests
        for i in range(5):
            await self.manager.check_request(
                request_id=f"req-{i}",
                session_id="session-1",
                estimated_cost=Decimal("10.00"),
            )

        # Confirm concurrently
        async def confirm_request(i: int) -> None:
            await self.manager.confirm_request(
                request_id=f"req-{i}",
                session_id="session-1",
                confirmed=True,
            )

        tasks = [confirm_request(i) for i in range(5)]
        await asyncio.gather(*tasks)

        # All should be confirmed
        for i in range(5):
            is_confirmed = await self.manager.is_confirmed(f"req-{i}")
            assert is_confirmed is True
