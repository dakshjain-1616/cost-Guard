# CostGuard — Real-Time AI Spend Circuit Breaker

## Goal
Build a production-ready local proxy that enforces hard spending limits across multiple time windows (session, hour, day, project) with real-time dashboard, safe mode, and comprehensive provider pricing support.

## Research Summary
- **OpenAI Pricing (April 2026)**: GPT-5.5 ($5/$30 per MTok), GPT-5.4 ($2.50/$15), GPT-5.4 mini ($0.75/$4.50), GPT-4o ($2.50/$10)
- **Anthropic Pricing (April 2026)**: Claude Opus 4.7 ($5/$25), Sonnet 4.6 ($3/$15), Haiku 4.5 ($1/$5)
- **OpenRouter**: 400+ models, 5.5% platform fee, passthrough pricing, OpenAI-compatible API
- **Key Insight**: All providers use per-million-token pricing; OpenRouter provides unified access to multiple providers

## Approach
**Architecture**: Async FastAPI proxy server with SQLite persistence, WebSocket dashboard, and pluggable provider pricing. Circuit breaker evaluates limits in deterministic order (session → hour → day → project) before allowing requests.

**Key Design Decisions**:
1. **SQLite** for local-only persistence (requirement)
2. **Async architecture** for handling concurrent requests efficiently
3. **WebSocket** for real-time dashboard updates
4. **Tiktoken** for accurate token counting
5. **Pydantic** for strict validation
6. **Provider-agnostic** via OpenRouter + direct provider support

## Subtasks

### 1. Project Structure & Dependencies
- Create pyproject.toml with all dependencies (fastapi, uvicorn, websockets, tiktoken, pydantic, pytest, mypy, ruff)
- Setup directory structure: src/costguard/, tests/, config/
- Create .env.example with all configuration options

### 2. Core Data Models & Configuration
- Pydantic models for: BudgetConfig, SpendRecord, CircuitBreakerState, ProviderPricing
- Configuration loader with validation
- Environment variable support

### 3. SQLite Persistence Layer
- Database schema: spend_records, budget_limits, alerts
- Async SQLite operations via aiosqlite
- Migration system
- Repository pattern for data access

### 4. Cost Estimation Engine
- Token counting using tiktoken
- Provider pricing database (OpenAI, Anthropic, OpenRouter)
- Cost calculation with input/output token rates
- Pre-call estimation for safe mode

### 5. Circuit Breaker Logic
- Budget tracker with multiple windows (session, hour, day, project)
- Deterministic evaluation order: session → hour → day → project
- Thread-safe spend tracking
- Structured error responses when limits exceeded

### 6. Alert & Logging System
- Immediate alerts when limits triggered
- Structured logging with rotation
- Alert channels: console, webhook (extensible)

### 7. FastAPI Proxy Server
- OpenAI-compatible endpoints (/v1/chat/completions, /v1/models)
- Request/response interception for cost tracking
- Timeout and error handling
- Authentication validation

### 8. Safe Mode Implementation
- Pre-call cost estimation
- Confirmation threshold configuration
- Interactive confirmation flow
- Rejection for calls above threshold without confirmation

### 9. Real-Time Dashboard
- WebSocket endpoint for live updates
- Terminal-based dashboard using rich/blessed
- Current spend display across all windows
- Recent transactions and alerts

### 10. Comprehensive Tests
- Unit tests for circuit breaker logic
- Integration tests for proxy endpoints
- Cost estimation accuracy tests
- Safe mode gate tests
- Structured error validation

### 11. Tooling Configuration
- Ruff for linting and formatting
- MyPy for type checking
- Pytest with coverage
- Pre-commit hooks

### 12. Documentation
- README.md with architecture diagram (Mermaid)
- Installation and configuration guide
- Usage examples with real commands
- API documentation

## Deliverables
| File Path | Description |
|-----------|-------------|
| /home/daksh/may20/projects/costguard/pyproject.toml | Project dependencies |
| /home/daksh/may20/projects/costguard/.env.example | Configuration template |
| /home/dash/may20/projects/costguard/src/costguard/ | Main source code |
| /home/daksh/may20/projects/costguard/tests/ | Test suite |
| /home/daksh/may20/projects/costguard/README.md | Documentation |
| /home/daksh/may20/projects/costguard/config/ | Tooling configs |

## Evaluation Criteria
- All tests pass (pytest)
- Type checking passes (mypy)
- Linting passes (ruff)
- Proxy server starts and handles requests
- Circuit breaker correctly enforces limits
- Dashboard displays real-time data
- No hardcoded secrets
- Valid model names only (April 2026)

## Notes
- Use OpenRouter as primary provider (single API key, 400+ models)
- Support direct OpenAI and Anthropic for users with specific keys
- All pricing validated against April 2026 sources
- SQLite database at ~/.costguard/costguard.db by default
