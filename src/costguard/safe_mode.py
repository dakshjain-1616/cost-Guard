"""Safe mode implementation with pre-call estimation and confirmation gates."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from costguard.database import Database, get_database
from costguard.models import AlertChannel, BudgetConfig


@dataclass
class SafeModeCheckResult:
    """Result of safe mode check."""

    requires_confirmation: bool
    confirmed: bool
    estimated_cost: Decimal
    threshold: Decimal
    request_id: str
    session_id: str
    expires_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "requires_confirmation": self.requires_confirmation,
            "confirmed": self.confirmed,
            "estimated_cost": str(self.estimated_cost),
            "threshold": str(self.threshold),
            "request_id": self.request_id,
            "session_id": self.session_id,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


class PendingConfirmation:
    """Pending confirmation entry."""

    def __init__(
        self,
        request_id: str,
        session_id: str,
        estimated_cost: Decimal,
        threshold: Decimal,
        expires_at: datetime,
    ) -> None:
        """Initialize pending confirmation."""
        self.request_id = request_id
        self.session_id = session_id
        self.estimated_cost = estimated_cost
        self.threshold = threshold
        self.expires_at = expires_at
        self.confirmed: bool | None = None
        self.confirmed_at: datetime | None = None

    def is_expired(self) -> bool:
        """Check if confirmation has expired."""
        return datetime.now(timezone.utc) > self.expires_at

    def confirm(self, confirmed: bool) -> None:
        """Mark as confirmed or rejected."""
        self.confirmed = confirmed
        self.confirmed_at = datetime.now(timezone.utc)


class SafeModeManager:
    """Manages safe mode with pre-call estimation and confirmation gates."""

    # Default confirmation timeout
    CONFIRMATION_TIMEOUT_SECONDS = 300  # 5 minutes

    def __init__(
        self,
        config: BudgetConfig | None = None,
        database: Database | None = None,
    ) -> None:
        """Initialize safe mode manager.

        Args:
            config: Budget configuration with safe mode threshold.
            database: Database instance.
        """
        self.config = config or BudgetConfig()
        self._db = database or get_database()
        self._pending_confirmations: dict[str, PendingConfirmation] = {}
        self._lock = asyncio.Lock()

    async def check_request(
        self,
        request_id: str,
        session_id: str,
        estimated_cost: Decimal,
    ) -> SafeModeCheckResult:
        """Check if request requires safe mode confirmation.

        Args:
            request_id: Unique request identifier.
            session_id: Session identifier.
            estimated_cost: Estimated cost of the request.

        Returns:
            SafeModeCheckResult with confirmation requirements.
        """
        threshold = self.config.safe_mode_threshold

        # Check if cost exceeds threshold
        if estimated_cost < threshold:
            # Below threshold, no confirmation needed
            return SafeModeCheckResult(
                requires_confirmation=False,
                confirmed=True,
                estimated_cost=estimated_cost,
                threshold=threshold,
                request_id=request_id,
                session_id=session_id,
            )

        # Check if already confirmed
        async with self._lock:
            if request_id in self._pending_confirmations:
                pending = self._pending_confirmations[request_id]
                if not pending.is_expired() and pending.confirmed is not None:
                    # Already processed
                    return SafeModeCheckResult(
                        requires_confirmation=True,
                        confirmed=pending.confirmed,
                        estimated_cost=estimated_cost,
                        threshold=threshold,
                        request_id=request_id,
                        session_id=session_id,
                    )

            # Create pending confirmation
            expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=self.CONFIRMATION_TIMEOUT_SECONDS
            )
            pending = PendingConfirmation(
                request_id=request_id,
                session_id=session_id,
                estimated_cost=estimated_cost,
                threshold=threshold,
                expires_at=expires_at,
            )
            self._pending_confirmations[request_id] = pending

        # Send alert if configured
        if AlertChannel.CONSOLE in self.config.alert_channels:
            self._send_console_alert(request_id, session_id, estimated_cost, threshold)

        return SafeModeCheckResult(
            requires_confirmation=True,
            confirmed=False,
            estimated_cost=estimated_cost,
            threshold=threshold,
            request_id=request_id,
            session_id=session_id,
            expires_at=expires_at,
        )

    async def confirm_request(
        self,
        request_id: str,
        session_id: str,
        confirmed: bool,
    ) -> SafeModeCheckResult:
        """Confirm or reject a pending request.

        Args:
            request_id: Request identifier.
            session_id: Session identifier.
            confirmed: True to confirm, False to reject.

        Returns:
            SafeModeCheckResult with updated status.
        """
        async with self._lock:
            if request_id not in self._pending_confirmations:
                # No pending confirmation found
                return SafeModeCheckResult(
                    requires_confirmation=False,
                    confirmed=False,
                    estimated_cost=Decimal("0.00"),
                    threshold=self.config.safe_mode_threshold,
                    request_id=request_id,
                    session_id=session_id,
                )

            pending = self._pending_confirmations[request_id]

            if pending.is_expired():
                # Confirmation expired
                del self._pending_confirmations[request_id]
                return SafeModeCheckResult(
                    requires_confirmation=True,
                    confirmed=False,
                    estimated_cost=pending.estimated_cost,
                    threshold=pending.threshold,
                    request_id=request_id,
                    session_id=session_id,
                )

            # Update confirmation status
            pending.confirm(confirmed)

            result = SafeModeCheckResult(
                requires_confirmation=True,
                confirmed=confirmed,
                estimated_cost=pending.estimated_cost,
                threshold=pending.threshold,
                request_id=request_id,
                session_id=session_id,
            )

            # Clean up if rejected or keep for a bit if confirmed
            if not confirmed:
                del self._pending_confirmations[request_id]

            return result

    async def is_confirmed(self, request_id: str) -> bool:
        """Check if a request has been confirmed.

        Args:
            request_id: Request identifier.

        Returns:
            True if confirmed, False otherwise.
        """
        async with self._lock:
            if request_id not in self._pending_confirmations:
                return False

            pending = self._pending_confirmations[request_id]

            if pending.is_expired():
                del self._pending_confirmations[request_id]
                return False

            return pending.confirmed is True

    async def cleanup_expired(self) -> int:
        """Clean up expired pending confirmations.

        Returns:
            Number of entries cleaned up.
        """
        cleaned = 0

        async with self._lock:
            expired_ids = [
                req_id
                for req_id, pending in self._pending_confirmations.items()
                if pending.is_expired()
            ]

            for req_id in expired_ids:
                del self._pending_confirmations[req_id]
                cleaned += 1

        return cleaned

    def _send_console_alert(
        self,
        request_id: str,
        session_id: str,
        estimated_cost: Decimal,
        threshold: Decimal,
    ) -> None:
        """Send console alert for safe mode trigger."""
        try:
            from rich.console import Console
            from rich.panel import Panel

            console = Console()

            message = (
                f"🛡️ Safe Mode Triggered\n\n"
                f"Request ID: {request_id}\n"
                f"Session: {session_id}\n"
                f"Estimated Cost: ${estimated_cost:.4f}\n"
                f"Threshold: ${threshold:.4f}\n\n"
                f"This request exceeds the safe mode threshold.\n"
                f"Use POST /v1/safe-mode/confirm to approve."
            )

            panel = Panel(
                message,
                title="Safe Mode",
                border_style="yellow",
                padding=(1, 2),
            )
            console.print(panel)

        except Exception:
            # Silently fail if rich not available
            pass

    def get_pending_count(self) -> int:
        """Get number of pending confirmations."""
        return len(self._pending_confirmations)

    def get_pending_requests(self) -> list[dict[str, Any]]:
        """Get list of pending confirmation requests."""
        return [
            {
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "estimated_cost": str(pending.estimated_cost),
                "threshold": str(pending.threshold),
                "expires_at": pending.expires_at.isoformat(),
                "time_remaining_seconds": max(
                    0,
                    (pending.expires_at - datetime.now(timezone.utc)).total_seconds(),
                ),
            }
            for pending in self._pending_confirmations.values()
            if not pending.is_expired() and pending.confirmed is None
        ]


class SafeModeMiddleware:
    """Middleware for safe mode in FastAPI."""

    def __init__(self, safe_mode_manager: SafeModeManager | None = None) -> None:
        """Initialize middleware."""
        self.manager = safe_mode_manager or SafeModeManager()

    async def check_request(
        self,
        request_id: str,
        session_id: str,
        estimated_cost: Decimal,
    ) -> SafeModeCheckResult:
        """Check if request requires confirmation."""
        return await self.manager.check_request(
            request_id=request_id,
            session_id=session_id,
            estimated_cost=estimated_cost,
        )

    async def confirm_request(
        self,
        request_id: str,
        session_id: str,
        confirmed: bool,
    ) -> SafeModeCheckResult:
        """Confirm or reject a request."""
        return await self.manager.confirm_request(
            request_id=request_id,
            session_id=session_id,
            confirmed=confirmed,
        )
