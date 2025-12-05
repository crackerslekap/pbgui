# PBGUI Improvement Recommendations

The recommendations below outline actionable ideas to improve PBGUI's stability, usability, and maintainability.

## Architecture and Reliability
- **Introduce typing**: Gradually add type hints and a type checker (e.g., `pyright` or `mypy`) starting with shared modules such as `Config.py`, `Base.py`, and service helpers. This will reduce runtime errors in components like `Optimize.py` and `PBRun.py`.
- **Dependency isolation**: Replace ad-hoc environment setup scripts with a reproducible lockfile workflow (e.g., `pip-tools` or `uv`) and document per-environment dependencies for the `requirements.txt` and `requirements_vps.txt` installs.
- **Configuration schema**: Define a single config schema (e.g., `pydantic` model) for files like `pbgui.ini.example` and `config.json.jinja` so inputs are validated at startup.
- **Logging standardization**: Consolidate logging via `logging_helpers.py` with structured fields and consistent log levels across `PBMon.py`, `Monitor.py`, and `Status.py` to simplify troubleshooting.

## Observability and Testing
- **Automated tests**: Introduce unit tests for core utilities (e.g., `PBData.py`, `Exchange.py`) and regression tests for grid/optimization flows (`OptimizeV7.py`, `RunV7.py`). Wire these into CI pipelines used by `master-update-*.yml` workflows.
- **Mockable services**: Abstract external API access (e.g., exchange data fetches in `PBCoinData.py`) behind interfaces so tests can mock network calls and avoid live dependencies.
- **Health checks**: Add lightweight runtime health endpoints or CLI checks to validate database connectivity (`Database.py`) and daemon liveness (`Services.py`).

## User Experience
- **CLI ergonomics**: Provide a single entrypoint script that exposes subcommands (start/stop/status) instead of multiple ad-hoc launchers (`pbgui.py`, `starter.py`, `start.sh.example`).
- **Progress and status reporting**: Enhance dashboard updates for long-running jobs (`OptimizeMulti.py`, `Backtest.py`) with progress percentages and estimated completion times.
- **Documentation flow**: Expand `README.md` with a quick-start covering installation, configuration, and common tasks; include screenshots of dashboard views and examples using `index.html.jinja` templates.

## Safety and Deployment
- **Secrets handling**: Move credentials and API keys out of config files into environment variables or a secrets manager; document expected variables in deployment guides.
- **Graceful restarts**: Ensure `Services.py` and VPS automation scripts perform graceful shutdowns and restart backoffs to prevent partial runs.
- **Resource tuning**: Add default resource limits and concurrency controls for batch runners (`OptimizeMulti.py`, `Multi.py`) to prevent resource exhaustion on VPS deployments.

## Performance
- **Caching**: Cache frequently used market data in `PBData.py`/`PBCoinData.py` with TTL-based invalidation to reduce duplicate fetches.
- **Batch processing**: Vectorize or parallelize scoring routines in `OptimizeScore.py` and `NeatGrid.py` where safe, and profile hotspots before scaling.

Each item is intended as a standalone, incremental improvement. Prioritize reliability (typing, testing, configuration validation) first, then build toward UX and performance enhancements.
