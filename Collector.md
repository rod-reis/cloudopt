# cloudopt — Instructions

## What This Project Is

Read-only Python CLI that collects Azure VM inventory + performance metrics across subscriptions and produces a JSON report. A separate `analyze` step generates an Excel workbook and launches a local FastAPI dashboard.

## Two-Phase Workflow

```
cloudopt collect   →  cloudopt_report.json   (customer runs this)
cloudopt analyze   →  cloudopt_report.xlsx   (Microsoft engineer runs this)
cloudopt dashboard →  local web UI           (Microsoft engineer runs this)
```

## Key Source Modules

| Path                       | Purpose                                                               |
| -------------------------- | --------------------------------------------------------------------- |
| `src/cloudopt              | Typer CLI entry point                                                 |
| `src/cloudopt/`            | Azure SDK data collectors (inventory, metrics, quota, advisor, zones) |
| `src/cloudopt/throttle.py` | Token-bucket rate limiter + exponential backoff for ARM API           |
| `src/cloudopt/auth.py`     | DefaultAzureCredential helpers                                        |
| `src/cloudopt`             | Recommendations engine + SKU catalog                                  |
| `src/cloudopt/`            | FastAPI + Jinja2 local web dashboard                                  |
| `src/cloudopt              | JSON / CSV / Excel export                                             |
| `src/cloudopt`             | Pydantic data models                                                  |
| `src/cloudopt              | Subscription/resource scope resolution                                |

## Architecture Constraints

- **Read-only**: the tool NEVER writes to Azure; it only reads via ARM, Monitor, Advisor, and Log Analytics APIs.
- **No secrets in code**: auth is exclusively via `DefaultAzureCredential`; no client secrets or keys are ever hardcoded.
- **JSON-first**: `collect` outputs only JSON. Excel/CSV are derived formats produced by `analyze`.
- **Async throughout**: all Azure API calls use `asyncio` + `asyncio.Semaphore` + token-bucket rate limiting.
- **Customer data stays local**: output files (`output/`, `output-*/`) are gitignored and MUST NOT be committed.

## Running the Tool

```bash
# Install in dev mode
pip install -e ".[dev]"

# Collect from Azure
cloudopt collect --output output/

# Analyze collected data
cloudopt analyze --from output/cloudopt_report.json

# Launch dashboard
cloudopt dashboard --data output/cloudopt_report.xlsx
```

## Running Tests

```bash
pytest                          # all tests
pytest --cov=cloudoptverage
pytest tests/test_metrics.py    # single file
```

## Code Style

- Python 3.11+, type hints required on all public functions
- `from __future__ import annotations` at top of every module
- Pydantic v2 for all data models
- `rich` for all console output (no bare `print()`)
- Immutable data patterns — prefer `@dataclass(frozen=True)` and Pydantic models over dicts
- Functions < 50 lines; files < 800 lines

## Security Requirements

- All Azure credential handling goes through `collector/auth.py`
- Subscription IDs and tenant IDs are treated as sensitive — never log them at INFO level
- Input validation on all CLI args (especially file paths and subscription ID format)
- No `eval()`, `exec()`, or `subprocess` with user-controlled input

## When Adding New Azure Collectors

1. Add the collector module under `src/cloudopt/`
2. Use `ThrottleManager` from `throttle.py` for all ARM/Monitor calls
3. Add corresponding Pydantic models to `models.py`
4. Write mocked unit tests in `tests/` (mock the Azure SDK clients, not HTTP)
5. Export new data in `export/json_export.py`
