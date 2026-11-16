# AGENTS.md

Home Assistant custom integration (**Python**) for **Dominion Energy South Carolina**.
It polls the Dominion/Fusion billing API for electricity and gas usage and cost,
accumulates monotonic running totals, and feeds them into Home Assistant's Energy
Dashboard and long-term statistics.

## Stack

- Python **3.11–3.13** (CI matrix); develop/tested on latest 3.x
- Home Assistant **2026.4.0** (see `manifest.json` `homeassistant`)
- Runtime dependency: `beautifulsoup4>=4.12.3` (the API client scrapes HTML)
- Packaging: HACS custom integration (`hacs.json`) — **no** npm/package.json, it's pure Python

## Layout

- `custom_components/dominionsc/` — the shipped integration
  - `__init__.py` — setup, service registration, platform forwarding
  - `config_flow.py` — credentials + optional 2FA + options flow
  - `coordinator.py` — polling, daily reconciliation, backfill, statistics import (largest module, ~1488 lines)
  - `dominion_sc_client.py` / `bidgely_client.py` — blocking HTTP clients (Dominion + Bidgely)
  - `sensor.py` — energy + diagnostic + account/billing sensors
  - `button.py` — Run Backfill / Cleanup External Statistics buttons
  - `const.py`, `manifest.json`, `services.yaml`, `strings.json`, `translations/en.json`
- `tests/` — unit tests (`test_coordinator.py`, `test_sensor_unit.py`); `conftest.py` injects
  lightweight HA/bs4 stubs into `sys.modules` so imports work without a full HA install
- `diagnostics/` — `probe_fusion_daily_usage.{py,sh,ps1}` (interactive daily-usage probe)
- `ha_config/` — sample HA config (git-ignored); `docker_compose.yaml` mounts it for local dev

## Commands

- **Test**: `python -m pytest -q` (run from repo root). CI runs the same.
  Tests pass with just the `requirements-dev.txt` deps; no `pytest.ini`/ruff config exists.
- **Lint**: `ruff check` (see `.github/workflows/ci.yml`). Linting is defined but **not**
  enforced in CI, so it won't block a merge.
- **Dev deps**: `pip install -r requirements-dev.txt` (`pytest`, `pytest-asyncio`,
  `requests`, `beautifulsoup4`)
- **Local HA dev**: `docker compose -f docker_compose.yaml up` (binds `ha_config/` + the
  component into a linuxserver Home Assistant image on :8123)
- **Fusion probe**: `bash diagnostics/probe_fusion_daily_usage.sh` (or the `.ps1` on Windows).
  Creates a `.venv-fusion-probe` venv, prompts for Dominion credentials, prints only date/range/row counts.

## Conventions & gotchas

- **Run the test suite after editing** `coordinator.py`, `sensor.py`, `button.py`, or other core logic.
  Async tests need `pytest-asyncio` (already in dev deps).
- Statistics import: `async_import_statistics` / `async_add_external_statistics` and
  `recorder.async_clear_statistics` are **not** awaitable despite the `async_` prefix — do **not** `await` them.
- `StatisticMeanType.NONE` must be set in stat metadata (HA 2026.11+).
- Monetary sensors use `state_class=TOTAL` (never `TOTAL_INCREASING`).
- Gas is reported in CCF; the coordinator converts to ft³ (`CCF × 100`).
- Keep auth state in HA-native `ConfigEntry` + `Store` persistence only — no cookie files on disk.

## Notes

- The `reference/` and `scripts/` directories documented in `.github/copilot-instructions.md`
  do **not** currently exist; that file is a large, partly-out-of-date plan. Treat it as
  guidance, not the current source of truth — prefer the code and this file.
