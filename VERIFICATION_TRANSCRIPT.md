# Verification Transcript — costguard

Date (UTC): 2026-05-20
Workspace: `/home/daksh/may20/projects/costguard`
Python: `3.12.3`

## Dependency Install
- Command: `venv/bin/pip install -e '.[dev]'`
- Result: PASS

## Quality Gates
- Command: `venv/bin/ruff check src tests`
- Result: PASS (with Ruff deprecation warning about top-level `select`/`ignore` config keys)

- Command: `venv/bin/mypy src/costguard`
- Result: PASS

- Command: `venv/bin/pytest`
- Result: PASS (`84 passed`)
- Coverage: PASS (`Required test coverage of 50.0% reached. Total coverage: 50.97%`)

## CLI Verification
- Command: `venv/bin/costguard --help`
- Result: PASS

- Command: `venv/bin/costguard server --help`
- Result: PASS

- Command: `venv/bin/costguard dashboard --help`
- Result: PASS

- Command: `venv/bin/costguard estimate --help`
- Result: PASS

- Command: `venv/bin/costguard status --help`
- Result: PASS

- Command: `venv/bin/costguard init --help`
- Result: PASS

- Command: `venv/bin/costguard init --db-path /tmp/costguard-verify.db`
- Result: PASS (database initialized)

- Command: `venv/bin/costguard status --db-path /tmp/costguard-verify.db --session-id verify-session --project-id verify-project`
- Result: PASS (shows CLOSED status and zero spend)

- Command: `venv/bin/costguard estimate --model gpt-4o --prompt 'hello world' --output-tokens 64`
- Result: PASS (returns estimate and pricing block)

## README Command Truthfulness Checks
- Installation (`pip install -e '.[dev]'`): PASS
- Uvicorn launch command updated to factory form:
  - `uvicorn costguard.server:create_app --factory --reload --port 8000`
- CLI command examples (`server`, `dashboard`, `estimate`, `status`, `init`): PASS by direct invocation/help checks above

## Model Reference Validation Notes (April 2026)
- Canonical model IDs updated in pricing + README to include API-style names such as:
  - OpenAI: `gpt-4.1`, `gpt-4.1-mini`, `gpt-4.1-nano`, `gpt-4o`, `gpt-4o-mini`
  - Anthropic: `claude-opus-4-20250514`, `claude-sonnet-4-20250514`, `claude-3-7-sonnet-20250219`
- Backward-compatible legacy aliases retained for existing tests/integrations.
