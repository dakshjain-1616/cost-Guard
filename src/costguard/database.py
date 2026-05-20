"""SQLite persistence layer with async operations."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import aiosqlite

from costguard.models import (
    AlertChannel,
    AlertEvent,
    BudgetConfig,
    BudgetWindow,
    CircuitBreakerState,
    CircuitBreakerStatus,
    Provider,
    ProviderPricing,
    SpendRecord,
)


class DatabaseError(Exception):
    """Database operation error."""

    pass


class Database:
    """Async SQLite database manager."""

    def __init__(self, db_path: str | Path = "~/.costguard/costguard.db") -> None:
        """Initialize database connection.

        Args:
            db_path: Path to SQLite database file.
        """
        if str(db_path) == ":memory:":
            self.db_path = Path(":memory:")
        else:
            self.db_path = Path(db_path).expanduser().resolve()
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Establish database connection."""
        if self._connection is None:
            connect_path = ":memory:" if str(self.db_path) == ":memory:" else str(self.db_path)
            self._connection = await aiosqlite.connect(connect_path)
            self._connection.row_factory = aiosqlite.Row
            # Enable foreign keys
            await self._connection.execute("PRAGMA foreign_keys = ON")
            # Optimize for concurrent reads
            await self._connection.execute("PRAGMA journal_mode = WAL")
            await self._connection.execute("PRAGMA synchronous = NORMAL")

    async def close(self) -> None:
        """Close database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None

    async def initialize_schema(self) -> None:
        """Create database tables if they don't exist."""
        if not self._connection:
            await self.connect()

        assert self._connection is not None

        # Spend records table
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS spend_records (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                provider TEXT NOT NULL,
                model_id TEXT NOT NULL,
                request_id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'default',
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost TEXT NOT NULL,
                actual_cost TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                error_message TEXT,
                metadata TEXT
            )
        """)

        # Circuit breaker state table
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS circuit_breaker_states (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL DEFAULT 'default',
                status TEXT NOT NULL DEFAULT 'closed',
                session_start TEXT NOT NULL,
                last_request_time TEXT,
                session_spend TEXT NOT NULL DEFAULT '0.00',
                hour_spend TEXT NOT NULL DEFAULT '0.00',
                day_spend TEXT NOT NULL DEFAULT '0.00',
                project_spend TEXT NOT NULL DEFAULT '0.00',
                total_requests INTEGER NOT NULL DEFAULT 0,
                blocked_requests INTEGER NOT NULL DEFAULT 0,
                last_alert_time TEXT,
                triggered_limit TEXT
            )
        """)

        # Alert events table
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS alert_events (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'warning',
                session_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'default',
                exceeded_limits TEXT,
                current_spend TEXT,
                message TEXT NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0
            )
        """)

        # Provider pricing table
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS provider_pricing (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                model_id TEXT NOT NULL UNIQUE,
                model_name TEXT NOT NULL,
                input_price_per_mtok TEXT NOT NULL,
                output_price_per_mtok TEXT NOT NULL,
                context_window INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                effective_date TEXT NOT NULL
            )
        """)

        # Budget configuration table
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS budget_configs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL UNIQUE,
                session_limit TEXT NOT NULL DEFAULT '10.00',
                hour_limit TEXT NOT NULL DEFAULT '50.00',
                day_limit TEXT NOT NULL DEFAULT '200.00',
                project_limit TEXT NOT NULL DEFAULT '1000.00',
                safe_mode_threshold TEXT NOT NULL DEFAULT '5.00',
                alert_channels TEXT NOT NULL DEFAULT '["console"]',
                webhook_url TEXT,
                updated_at TEXT NOT NULL
            )
        """)

        # Create indexes for performance
        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_spend_session ON spend_records(session_id)
        """)
        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_spend_project ON spend_records(project_id)
        """)
        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_spend_timestamp ON spend_records(timestamp)
        """)
        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_alerts_session ON alert_events(session_id)
        """)
        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alert_events(timestamp)
        """)

        await self._connection.commit()

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Provide a transactional scope around a series of operations."""
        if not self._connection:
            await self.connect()
        assert self._connection is not None

        try:
            yield self._connection
            await self._connection.commit()
        except Exception:
            await self._connection.rollback()
            raise

    # Spend Record Operations

    async def save_spend_record(self, record: SpendRecord) -> None:
        """Save a spend record to the database."""
        if not self._connection:
            await self.connect()
        assert self._connection is not None

        await self._connection.execute(
            """
            INSERT OR REPLACE INTO spend_records (
                id, timestamp, provider, model_id, request_id, session_id,
                project_id, input_tokens, output_tokens, total_tokens,
                estimated_cost, actual_cost, status, error_message, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(record.id),
                record.timestamp.isoformat(),
                record.provider.value,
                record.model_id,
                record.request_id,
                record.session_id,
                record.project_id,
                record.input_tokens,
                record.output_tokens,
                record.total_tokens,
                str(record.estimated_cost),
                str(record.actual_cost) if record.actual_cost else None,
                record.status,
                record.error_message,
                json.dumps(record.metadata) if record.metadata else None,
            ),
        )
        await self._connection.commit()

    async def get_spend_records(
        self,
        session_id: str | None = None,
        project_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[SpendRecord]:
        """Retrieve spend records with optional filtering."""
        if not self._connection:
            await self.connect()
        assert self._connection is not None

        query = "SELECT * FROM spend_records WHERE 1=1"
        params: list[Any] = []

        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if since:
            query += " AND timestamp >= ?"
            params.append(since.isoformat())

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        async with self._connection.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_spend_record(row) for row in rows]

    def _row_to_spend_record(self, row: aiosqlite.Row) -> SpendRecord:
        """Convert database row to SpendRecord."""
        return SpendRecord(
            id=UUID(row["id"]),
            timestamp=datetime.fromisoformat(row["timestamp"]),
            provider=Provider(row["provider"]),
            model_id=row["model_id"],
            request_id=row["request_id"],
            session_id=row["session_id"],
            project_id=row["project_id"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            total_tokens=row["total_tokens"],
            estimated_cost=Decimal(row["estimated_cost"]),
            actual_cost=Decimal(row["actual_cost"]) if row["actual_cost"] else None,
            status=row["status"],
            error_message=row["error_message"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )

    # Circuit Breaker State Operations

    async def get_or_create_circuit_breaker_state(
        self,
        session_id: str,
        project_id: str = "default",
    ) -> CircuitBreakerState:
        """Get existing state or create new one."""
        if not self._connection:
            await self.connect()
        assert self._connection is not None

        async with self._connection.execute(
            "SELECT * FROM circuit_breaker_states WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if row:
            return self._row_to_circuit_breaker_state(row)

        # Create new state
        state = CircuitBreakerState(
            session_id=session_id,
            project_id=project_id,
            session_start=datetime.now(timezone.utc),
        )
        await self.save_circuit_breaker_state(state)
        return state

    async def save_circuit_breaker_state(self, state: CircuitBreakerState) -> None:
        """Save circuit breaker state to database."""
        if not self._connection:
            await self.connect()
        assert self._connection is not None

        await self._connection.execute(
            """
            INSERT OR REPLACE INTO circuit_breaker_states (
                id, session_id, project_id, status, session_start, last_request_time,
                session_spend, hour_spend, day_spend, project_spend,
                total_requests, blocked_requests, last_alert_time, triggered_limit
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(state.id),
                state.session_id,
                state.project_id,
                state.status.value,
                state.session_start.isoformat(),
                state.last_request_time.isoformat() if state.last_request_time else None,
                str(state.session_spend),
                str(state.hour_spend),
                str(state.day_spend),
                str(state.project_spend),
                state.total_requests,
                state.blocked_requests,
                state.last_alert_time.isoformat() if state.last_alert_time else None,
                state.triggered_limit.value if state.triggered_limit else None,
            ),
        )
        await self._connection.commit()

    def _row_to_circuit_breaker_state(self, row: aiosqlite.Row) -> CircuitBreakerState:
        """Convert database row to CircuitBreakerState."""
        return CircuitBreakerState(
            id=UUID(row["id"]),
            session_id=row["session_id"],
            project_id=row["project_id"],
            status=CircuitBreakerStatus(row["status"]),
            session_start=datetime.fromisoformat(row["session_start"]),
            last_request_time=datetime.fromisoformat(row["last_request_time"])
            if row["last_request_time"]
            else None,
            session_spend=Decimal(row["session_spend"]),
            hour_spend=Decimal(row["hour_spend"]),
            day_spend=Decimal(row["day_spend"]),
            project_spend=Decimal(row["project_spend"]),
            total_requests=row["total_requests"],
            blocked_requests=row["blocked_requests"],
            last_alert_time=datetime.fromisoformat(row["last_alert_time"])
            if row["last_alert_time"]
            else None,
            triggered_limit=BudgetWindow(row["triggered_limit"]) if row["triggered_limit"] else None,
        )

    # Alert Event Operations

    async def save_alert_event(self, event: AlertEvent) -> None:
        """Save an alert event to the database."""
        if not self._connection:
            await self.connect()
        assert self._connection is not None

        await self._connection.execute(
            """
            INSERT INTO alert_events (
                id, timestamp, alert_type, severity, session_id, project_id,
                exceeded_limits, current_spend, message, acknowledged
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(event.id),
                event.timestamp.isoformat(),
                event.alert_type,
                event.severity,
                event.session_id,
                event.project_id,
                json.dumps([limit.value for limit in event.exceeded_limits]),
                json.dumps(event.current_spend),
                event.message,
                int(event.acknowledged),
            ),
        )
        await self._connection.commit()

    async def get_recent_alerts(
        self,
        session_id: str | None = None,
        limit: int = 10,
    ) -> list[AlertEvent]:
        """Get recent alert events."""
        if not self._connection:
            await self.connect()
        assert self._connection is not None

        query = "SELECT * FROM alert_events WHERE 1=1"
        params: list[Any] = []

        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        async with self._connection.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_alert_event(row) for row in rows]

    def _row_to_alert_event(self, row: aiosqlite.Row) -> AlertEvent:
        """Convert database row to AlertEvent."""
        return AlertEvent(
            id=UUID(row["id"]),
            timestamp=datetime.fromisoformat(row["timestamp"]),
            alert_type=row["alert_type"],
            severity=row["severity"],
            session_id=row["session_id"],
            project_id=row["project_id"],
            exceeded_limits=[
                BudgetWindow(limit_name) for limit_name in json.loads(row["exceeded_limits"])
            ]
            if row["exceeded_limits"]
            else [],
            current_spend=json.loads(row["current_spend"]) if row["current_spend"] else {},
            message=row["message"],
            acknowledged=bool(row["acknowledged"]),
        )

    # Provider Pricing Operations

    async def save_provider_pricing(self, pricing: ProviderPricing) -> None:
        """Save or update provider pricing."""
        if not self._connection:
            await self.connect()
        assert self._connection is not None

        await self._connection.execute(
            """
            INSERT OR REPLACE INTO provider_pricing (
                id, provider, model_id, model_name, input_price_per_mtok,
                output_price_per_mtok, context_window, active, effective_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(UUID(int=hash((pricing.provider.value, pricing.model_id)) % (2**128))),
                pricing.provider.value,
                pricing.model_id,
                pricing.model_name,
                str(pricing.input_price_per_mtok),
                str(pricing.output_price_per_mtok),
                pricing.context_window,
                int(pricing.active),
                pricing.effective_date.isoformat(),
            ),
        )
        await self._connection.commit()

    async def get_provider_pricing(
        self,
        provider: Provider | None = None,
        model_id: str | None = None,
    ) -> list[ProviderPricing]:
        """Get provider pricing with optional filtering."""
        if not self._connection:
            await self.connect()
        assert self._connection is not None

        query = "SELECT * FROM provider_pricing WHERE active = 1"
        params: list[Any] = []

        if provider:
            query += " AND provider = ?"
            params.append(provider.value)
        if model_id:
            query += " AND model_id = ?"
            params.append(model_id)

        async with self._connection.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_provider_pricing(row) for row in rows]

    def _row_to_provider_pricing(self, row: aiosqlite.Row) -> ProviderPricing:
        """Convert database row to ProviderPricing."""
        return ProviderPricing(
            provider=Provider(row["provider"]),
            model_id=row["model_id"],
            model_name=row["model_name"],
            input_price_per_mtok=Decimal(row["input_price_per_mtok"]),
            output_price_per_mtok=Decimal(row["output_price_per_mtok"]),
            context_window=row["context_window"],
            active=bool(row["active"]),
            effective_date=datetime.fromisoformat(row["effective_date"]),
        )

    # Budget Config Operations

    async def get_budget_config(self, project_id: str = "default") -> BudgetConfig:
        """Get budget configuration for a project."""
        if not self._connection:
            await self.connect()
        assert self._connection is not None

        async with self._connection.execute(
            "SELECT * FROM budget_configs WHERE project_id = ?",
            (project_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if row:
            return self._row_to_budget_config(row)

        # Return default config
        return BudgetConfig()

    async def save_budget_config(self, project_id: str, config: BudgetConfig) -> None:
        """Save budget configuration."""
        if not self._connection:
            await self.connect()
        assert self._connection is not None

        await self._connection.execute(
            """
            INSERT OR REPLACE INTO budget_configs (
                id, project_id, session_limit, hour_limit, day_limit, project_limit,
                safe_mode_threshold, alert_channels, webhook_url, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(UUID(int=hash(project_id) % (2**128))),
                project_id,
                str(config.session_limit),
                str(config.hour_limit),
                str(config.day_limit),
                str(config.project_limit),
                str(config.safe_mode_threshold),
                json.dumps([ch.value for ch in config.alert_channels]),
                config.webhook_url,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self._connection.commit()

    def _row_to_budget_config(self, row: aiosqlite.Row) -> BudgetConfig:
        """Convert database row to BudgetConfig."""
        return BudgetConfig(
            session_limit=Decimal(row["session_limit"]),
            hour_limit=Decimal(row["hour_limit"]),
            day_limit=Decimal(row["day_limit"]),
            project_limit=Decimal(row["project_limit"]),
            safe_mode_threshold=Decimal(row["safe_mode_threshold"]),
            alert_channels=[AlertChannel(ch) for ch in json.loads(row["alert_channels"])],
            webhook_url=row["webhook_url"],
        )

    # Aggregate Queries

    async def get_session_spend(self, session_id: str) -> Decimal:
        """Get total spend for a session."""
        if not self._connection:
            await self.connect()
        assert self._connection is not None

        async with self._connection.execute(
            """
            SELECT COALESCE(SUM(CAST(estimated_cost AS DECIMAL)), 0) as total
            FROM spend_records
            WHERE session_id = ? AND status IN ('completed', 'pending')
            """,
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return Decimal(str(row["total"])) if row else Decimal("0.00")

    async def get_hourly_spend(
        self,
        project_id: str | None = None,
        hour_ago: datetime | None = None,
    ) -> Decimal:
        """Get spend in the last hour."""
        if not self._connection:
            await self.connect()
        assert self._connection is not None

        if hour_ago is None:
            hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)

        query = """
            SELECT COALESCE(SUM(CAST(estimated_cost AS DECIMAL)), 0) as total
            FROM spend_records
            WHERE timestamp >= ? AND status IN ('completed', 'pending')
        """
        params: list[Any] = [hour_ago.isoformat()]

        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)

        async with self._connection.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return Decimal(str(row["total"])) if row else Decimal("0.00")

    async def get_daily_spend(
        self,
        project_id: str | None = None,
        day_start: datetime | None = None,
    ) -> Decimal:
        """Get spend for today."""
        if not self._connection:
            await self.connect()
        assert self._connection is not None

        if day_start is None:
            now = datetime.now(timezone.utc)
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        query = """
            SELECT COALESCE(SUM(CAST(estimated_cost AS DECIMAL)), 0) as total
            FROM spend_records
            WHERE timestamp >= ? AND status IN ('completed', 'pending')
        """
        params: list[Any] = [day_start.isoformat()]

        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)

        async with self._connection.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return Decimal(str(row["total"])) if row else Decimal("0.00")

    async def get_project_spend(self, project_id: str) -> Decimal:
        """Get total spend for a project."""
        if not self._connection:
            await self.connect()
        assert self._connection is not None

        async with self._connection.execute(
            """
            SELECT COALESCE(SUM(CAST(estimated_cost AS DECIMAL)), 0) as total
            FROM spend_records
            WHERE project_id = ? AND status IN ('completed', 'pending')
            """,
            (project_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return Decimal(str(row["total"])) if row else Decimal("0.00")


# Global database instance
_db: Database | None = None


def get_database(db_path: str | Path | None = None) -> Database:
    """Get or create global database instance."""
    global _db
    if _db is None:
        _db = Database(db_path) if db_path else Database()
    return _db


def reset_database() -> None:
    """Reset global database instance (for testing)."""
    global _db
    _db = None
