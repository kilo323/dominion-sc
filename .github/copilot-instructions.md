# Copilot Instructions for ha-dominionsc-energy

## Project Overview
This project is a Home Assistant custom integration for Dominion Energy South Carolina.

Primary goals:
- Support Home Assistant **2026.4.0**.
- Provide sensors for the Energy Dashboard:
  - Electric consumption total
  - Gas consumption total
  - Electric cost total
  - Gas cost total
- Provide diagnostic sensors for backfill visibility:
  - Backfill cycles (total count + completed/incomplete lists)
  - Current billing cycle (date range)
- Provide UI buttons for manual control:
  - Run Backfill
  - Cleanup External Statistics (rewrite)
- Handle Dominion API billing-cycle resets safely using synthetic cumulative totals.
- Support historical backfill and lag-tolerant daily updates.
- Import historical statistics at correct day timestamps for Energy Dashboard history.

## Repository Structure
- `custom_components/dominionsc/`: Home Assistant integration code.
  - `__init__.py`: setup, service registration, platform forwarding.
  - `config_flow.py`: credentials, 2FA flow, options.
  - `coordinator.py`: polling, sync, backfill, dedupe, statistics import logic.
  - `sensor.py`: energy + diagnostic sensor entities.
  - `button.py`: UI button entities (Run Backfill, Cleanup External Statistics).
  - `const.py`: constants, defaults, platform list.
  - `dominion_sc_client.py`: Dominion Energy API client (auth, usage endpoints).
  - `bidgely_client.py`: Bidgely API client (interval/daily data).
  - `services.yaml`: service definitions.
  - `manifest.json`: integration metadata.
  - `translations/en.json`, `strings.json`: user-facing text.
- `reference/`: reverse engineering and API analysis only (not shipped in HA runtime).
- `scripts/`: standalone scripts/tests for simulation and debugging (not HA runtime code).
- `ha_config/`: local Home Assistant config directory (bind-mounted for dev/test via Docker Compose).
- `docker_compose.yaml`: Docker Compose file for local HA dev/test environment.

## Current Implementation Plan (Authoritative)
### Authentication & Session
- Config flow supports:
  1. Username/password entry.
  2. Optional 2FA method selection (SMS or email, if required by API).
  3. Trigger code send.
  4. User code entry with whitespace stripping.
- Store credentials in `ConfigEntry.data`.
- Store optional 2FA token in `ConfigEntry.data` (key: `tfa_token`).
- Coordinator re-authenticates using stored credentials on each poll.
- If a refreshed 2FA token is returned, persist it back to the config entry.
- No cookie files on disk — all auth state is managed via HA-native persistence.

### Polling & Options
- User-configurable polling frequency in minutes (options flow).
- Default polling frequency: **15 minutes**.
- User-configurable backfill cycle count target (options flow).
- Default backfill cycle count: **6**.
- User-configurable daily lookback days (options flow).
- Default daily lookback days: **5**.

### Data Sources
- **Incremental/current usage**: use 15-minute interval endpoint (`get_hourly_usage`) when available.
- **Backfill/historical**: use daily endpoint (`get_daily_usage`) for efficiency.
- Daily endpoint can lag by ~2 days; use lag-safe sliding window upsert behavior.
- Dominion API may return full billing cycle including future zero-value placeholder days; these must be filtered out.
 - Recent rows inside the daily lookback window that contain only null/zero values for consumption and cost should also be treated as placeholders and skipped during daily reconciliation. Dominion often provides these values a few days later; the lookback window will upsert them when available.
 - For finalized/backfill cycles, treat zero electric consumption as suspect and skip it during import (trust zero for gas and cost). This avoids importing premature zero-days that can break cumulative continuity.

### Dedupe & Idempotency
- Never double-count data.
- Maintain persistent ledgers (via `Store`):
  - Interval ledger keyed by `fuel|timestamp`.
  - Daily ledger keyed by `fuel|YYYY-MM-DD`.
  - Daily cost ledger keyed by `fuel|YYYY-MM-DD`.
- Upsert behavior:
  - Interval data: add only unseen intervals.
  - Daily data: update existing date rows as needed (idempotent).

### Units & Conversion
- Electric usage unit: `kWh`.
- Dominion gas usage source unit: `CCF`.
- Convert gas to cubic feet for HA sensor totals:
  - `ft³ = CCF * 100`.
- Cost unit: `$` (`CURRENCY_DOLLAR`).

### Synthetic Cumulative Totals
Because API values reset by billing cycle, integration must maintain persistent cumulative totals:
- `electric_kwh_total` (monotonic, never decreases)
- `gas_ft3_total` (monotonic, never decreases)
- `electric_cost_total`
- `gas_cost_total`

Energy Dashboard-facing usage sensors use `total_increasing` (consumption) or `total` (cost) and must never unexpectedly decrease.

### Historical Statistics Import
- After each backfill or scheduled update, import daily cumulative sums into HA recorder long-term statistics.
- Statistics are written to `sensor.*` entity statistic IDs (recorder source path).
- Four series are maintained:
  - `sensor.electric_cumulative_consumption` (kWh, from `daily_ledger`)
  - `sensor.gas_cumulative_consumption` (ft³, from `daily_ledger`)
  - `sensor.electric_cumulative_cost` ($, from `daily_cost_ledger`)
  - `sensor.gas_cumulative_cost` ($, from `daily_cost_ledger`)
- Append mode (`async_add_external_statistics`) for incremental day imports.
- Import mode (`async_import_statistics`) for initial or rewrite imports.
- Both recorder stats functions are **not awaitable** despite `async_` naming; do NOT use `await`.
- Include `mean_type=StatisticMeanType.NONE` in metadata to satisfy HA 2026.11+ requirements.
- Future-dated zero-value placeholder rows must be filtered before import.
 - Future-dated zero-value placeholder rows must be filtered before import.
 - Additionally, filter recent null/zero rows (within the daily lookback window) before upserting into ledgers or building `StatisticData`, because parsing may convert `null` to `0.0`. Apply fuel-aware rules: skip finalized zero electric consumption but trust finalized zero gas/cost.
- If newly backfilled days are older than previously imported days, auto-trigger full rewrite for that series.
- Track imported days per series in persistent state for idempotency.

### Backfill Behavior
- Automatic run: process at most **one** missing billing cycle per scheduled update.
- Cycles are selected oldest-first from `missing_cycles` queue.
- `missing_cycles` is computed from `BACKFILL_CYCLES_TARGET` (most recent N completed months).
- Manual service: `run_backfill`
  - Processes one billing cycle per call.
  - Optional `overwrite` flag:
    - `false`: fill missing only.
    - `true`: re-import selected cycle data and replace prior stored values safely.
- Manual service: `rewrite_statistics`
  - Clears legacy `dominionsc:*` external statistics.
  - Force-rewrites all historical data into `sensor.*` recorder statistics.

### Missing Data Tracking
- Persist billing cycle status and missing-cycle cursor in `Store`.
- Each scheduled run:
  1. Authenticate automatically.
  2. Ingest incremental usage (intervals).
  3. Perform lag-safe daily reconciliation (lookback window).
  4. Process next eligible backfill cycle (max one).
  5. Sync statistics to recorder.

## Home Assistant Compatibility Rules
- Target Home Assistant **2026.4.0** APIs and patterns.
- Use `DataUpdateCoordinator` for polling orchestration.
- Use `ConfigEntry` + options flow for configuration.
- Use `Store` (`homeassistant.helpers.storage.Store`) for persistent integration state.
- Use `DeviceInfo` with `entry_type=DeviceEntryType.SERVICE` for device registration.
- Use `SensorEntityDescription` for sensor metadata (not custom dataclasses).
- Set `native_unit_of_measurement` (not `suggested_unit_of_measurement`).
- Monetary sensors must use `state_class=TOTAL` (not `TOTAL_INCREASING`).
- Consumption sensors use `state_class=TOTAL_INCREASING`.
- Keep entity metadata correct for long-term statistics and Energy Dashboard compatibility.
- All auth state uses HA-native persistence (`ConfigEntry` + `Store`); no cookie files on disk.

## Sensor Requirements
### Energy Sensors (required for Energy Dashboard)
1. Electric cumulative consumption total (`kWh`, `total_increasing`, `device_class=ENERGY`)
2. Gas cumulative consumption total (`ft³`, `total_increasing`, `device_class=GAS`)
3. Electric cumulative cost total (`$`, `total`, `device_class=MONETARY`)
4. Gas cumulative cost total (`$`, `total`, `device_class=MONETARY`)

### Diagnostic Sensors
5. Backfill Cycles (configured target as state; attributes: `completed_count` of all-time successfully backfilled cycles, `incomplete_count` of eligible cycles not yet completed, `completed_cycles` persistent list as `YYYY-MMM`, `incomplete_cycles` eligible-but-not-yet-completed list as `YYYY-MMM`)
6. Current Billing Cycle (formatted as `MMM D - MMM D`, e.g. `Mar 9 - Apr 4`; attributes: `start` and `end` raw ISO dates)

### Account & Billing Informational Sensors
These sensors expose account and billing metadata useful for dashboards and automations. They are diagnostic/monetary sensors backed by the coordinator's persisted state and read from Dominion/Bidgely payloads.

7. Bill Status (string) — source: `account_summary.raw_data.account.accountStatus` or `account_summary.account_balance` textual fallback.
8. Last Payment (monetary, $) — source: `account_summary.last_payment_amount` (strings like `$333.19` parsed to numeric 333.19).
10. Account Balance (monetary, $) — source: `account_summary.account_balance`. Treat textual `Bill Paid` (case-insensitive) as numeric `0.0`.
11. Current Cost (monetary, $) — source: `bill_projection.currentPrice`.
12. Projected Price (monetary, $) — source: `bill_projection.projectionPrice`.
13. Days Left (integer) — source: `bill_projection.daysLeft`.
14. Electric Charges (monetary, $) — source: `current_daily_usage.bill_summary.electric_total` (or `electricTotalAmount` in raw payload).
15. Gas Charges (monetary, $) — source: `current_daily_usage.bill_summary.gas_total`.
16. Electric Other Charges (monetary, $) — source: `current_daily_usage.bill_summary.electric_other_charges`.
17. Gas Other Charges (monetary, $) — source: `current_daily_usage.bill_summary.gas_other_charges`.
18. Total Charges (monetary, $) — source: `current_daily_usage.bill_summary.total_bill_amount`.

Parsing & state rules for contributors
- Store fetched payloads in the coordinator persistent state keys: `account_summary`, `bill_projection`, `current_daily_usage`, and `current_bill_summary` (a convenience alias for `current_daily_usage.bill_summary`).
- Fetch these payloads during the coordinator update loop using `hass.async_add_executor_job(...)` since the client is blocking.
- Parse money strings by stripping `$` and commas; accept numeric values as-is. Treat `Bill Paid` and similar textual indications of a zero balance as `0.0` for monetary sensors.
- Parse due dates from `MMM D, YYYY` (e.g., `Mar 9, 2026`) and ISO `YYYY-MM-DD`; return `None` if unparseable.
- Keep these informational sensors separate from the cumulative/statistics sensors; they do not affect ledger or recorder import logic.

### Button Entities
7. Run Backfill — calls `dominionsc.run_backfill` service
8. Cleanup External Statistics — calls `dominionsc.rewrite_statistics` service

## Service Requirements
### `run_backfill`
- Optional `config_entry_id` (string)
- Optional `overwrite` (boolean, default `false`)

### `rewrite_statistics`
- Optional `config_entry_id` (string)
- Clears legacy `dominionsc:*` external statistics
- Force-rewrites all `sensor.*` historical statistics

## Reference Guidance
- Use `reference/` scripts and JSON dumps to understand API payloads.
- Do not copy reference scripts directly into integration runtime code.
- Prefer `get_daily_usage` for cycle backfill and `get_hourly_usage` for incremental updates.

## Home Assistant Version Compatibility
This integration is designed for and tested with Home Assistant **2026.4.0**. It leverages newer APIs and patterns that may not be available in older versions, including:
- DataUpdateCoordinator features
- Modern ConfigEntry options flow
- Enhanced recorder statistics handling
- Updated sensor entity metadata requirements

## Script Development Guidance
- Place non-HA simulation/test helpers in `scripts/`.
- `scripts/test_integration.py` simulates:
  - Login + optional 2FA
  - Auth reuse
  - Interval dedupe
  - Daily lag handling
  - One-cycle backfill progression
  - Overwrite backfill behavior
  - Monotonic totals and final summary assertions
- Uses `DOMINIONSC_TEST_BACKFILL_CYCLES_TARGET` (number of most recent billing cycles to backfill).

### HA DB query helper

- For quick validation or debugging of recorder statistics, use the included helper script `scripts/query_ha_stats.py`.
- The script inspects `ha_config/home-assistant_v2.db`, prints `statistics_meta` entries for the Dominion series, shows recent rows, and performs a monotonicity check on the full series.
- Run it from the repo root inside the project's virtualenv. Example (PowerShell):

```powershell
$env:PYTHONPATH = '.'; & '.\.venv\Scripts\Activate.ps1'; python .\scripts\query_ha_stats.py
```

Add any additional checks you need to the script (deltas, CSV export, ledger comparisons) rather than inventing new one-off DB queries in PRs.

## Coding Conventions
- Keep changes minimal and focused.
- Preserve existing project style where possible.
- Avoid introducing unnecessary dependencies.
- Use clear, deterministic keying for ledgers (`fuel|timestamp` or `fuel|YYYY-MM-DD`).
- Prefer explicit naming over implicit behavior.
- Use `_LOGGER = logging.getLogger(__name__)` in all modules (not root logger).
- Add debug breadcrumbs at key decision points (auth, backfill selection, stats sync, future-date filtering).
- Comment recorder stats calls with "do not await" to prevent regressions.

## Known HA API Gotchas
- `async_add_external_statistics()` and `async_import_statistics()` are NOT coroutines despite the naming; do NOT `await` them.
- `recorder.async_clear_statistics()` is also NOT awaitable.
- `StatisticMeanType.NONE` must be included in metadata starting HA 2026.11.
- `SensorDeviceClass.MONETARY` only supports `state_class=TOTAL` (not `TOTAL_INCREASING`).
- Statistic IDs must be lowercase, single-colon format, no hyphens (e.g., `dominionsc:abc_def`).
- Energy Dashboard entity picker requires at least one recorder statistics compilation cycle (~5 min) before entities appear.

## Notes
- If uncertain about endpoint semantics, add defensive validation and logs.
- Prioritize correctness for Energy Dashboard statistics over extra features.
- All auth state uses HA-native persistence (`ConfigEntry` + `Store`); no cookie files on disk.

### Quick DB inspector: `scripts/query_ha_db.py`

Use `scripts/query_ha_db.py` for fast, human-readable inspection of recorder statistics and states for a specific date or date range. It's handy when you want to:
- Validate which `statistics_meta` rows exist for the integration.
- Inspect smallest sums or suspicious rows for a day or range.
- Find future-dated statistics rows and negative sums.
- Spot missing or non-midnight rows before attempting recorder rewrites.

Key flags:
- `-d, --db` PATH — path to `home-assistant_v2.db` (defaults to `ha_config/home-assistant_v2.db`).
- `-s, --start-date` YYYY-MM-DD — start date (optional; defaults to today).
- `-e, --end-date` YYYY-MM-DD — end date (optional). If provided the query covers the inclusive range [start, end].
- `-q, --quiet` — suppress verbose schema and per-entity dumps; useful for scripting.

Examples (PowerShell):
```powershell
# single-day (defaults DB):
python.exe .\scripts\query_ha_db.py -s 2026-04-01

# date range:
python.exe .\scripts\query_ha_db.py -s 2026-04-01 -e 2026-04-03

# custom DB and quiet mode (machine-friendly summary):
python.exe .\scripts\query_ha_db.py -d 'C:\path\home-assistant_v2.db' -s 2026-04-01 -q
```

When to run it: before and after importing or rewriting external statistics, or when debugging Energy Dashboard/recorder anomalies. It complements `scripts/query_ha_stats.py` by providing quick heuristics and human-friendly summaries.

## Testing & Validation Workflow

**CRITICAL: Always run the test suite after making code changes.**

### Running Tests

After **every** change to `coordinator.py`, `sensor.py`, `button.py`, or other core integration logic, run pytests in tests folder.

All 24 tests must pass before considering changes complete. Async tests require `pytest-asyncio` (included in `requirements-dev.txt`).

If you see test failures:
1. Do not proceed without fixing them
2. Review the error output carefully — it will point to broken assumptions
3. Do NOT skip or ignore failing tests

### Test Coverage

- `tests/test_coordinator.py`: Core coordinator logic including:
  - Backfill cycle generation and initialization
  - Empty-row handling (no infinite retries)
  - Cycle selection and processing
  - Statistics sync and recorder import
  - Deduplication logic
- `tests/test_sensor_unit.py`: Sensor parsing and formatting

### Key Invariants Validated by Tests

- Backfill cycles exclude the current month (only completed past periods)
- Cycles are regenerated on each initialization (not stuck with stale cycles)
- Empty backfill responses mark cycles complete (no infinite retries)
- Monotonic totals never decrease
- Statistics are properly deduplicated