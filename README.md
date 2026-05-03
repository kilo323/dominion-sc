# Dominion Energy SC – Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A custom Home Assistant integration for **Dominion Energy South Carolina** that provides energy consumption and cost tracking for the [Energy Dashboard](https://www.home-assistant.io/docs/energy/).

## Features

- **Electric & Gas consumption** sensors with full historical backfill
- **Electric & Gas cost** sensors
- **Energy Dashboard compatible** — sensors use `total_increasing` / `total` state classes with correct units
- **Historical statistics import** — backfilled data appears at the correct dates in long-term statistics
- **Automatic incremental polling** — 15-minute interval data ingestion with deduplication
- **Lag-tolerant daily reconciliation** — handles Dominion's ~2-day reporting delay
- **Billing cycle backfill** — automatically fills one historical cycle per poll; configurable depth
- **2FA support** — SMS and email verification during setup
- **Persistent state** — survives restarts without data loss or double-counting
- **Manual backfill & statistics rewrite** — UI buttons and services for on-demand control

## Important updates (2026-04-03)

- Default SSL verification is now enabled. The integration will verify HTTPS certificates by default (the `verify_ssl` option defaults to `true` for new installs). If you previously disabled certificate verification, reconfigure the integration to re-enable it for that entry.
- The following settings can now be configured during the initial setup (and during reconfigure):
   - Poll interval (minutes)
   - Backfill cycles target
   - Daily lookback days

For existing Home Assistant entries, change these values via **Settings → Devices & Services → Dominion SC Energy → Reconfigure** or via the **Configure** (Options) dialog.

## Requirements

- Home Assistant **2026.4.0** or later
- A [Dominion Energy South Carolina](https://www.dominionenergy.com/south-carolina) online account
- HACS (for easy installation) or manual installation

## Home Assistant Compatibility

This integration is designed for Home Assistant **2026.4.0** and later versions. It uses modern Home Assistant patterns and APIs including:
- DataUpdateCoordinator for polling
- ConfigEntry and options flow for configuration
- Store for persistent state management
- SensorEntityDescription for sensor metadata
- Long-term statistics integration with recorder

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant.
2. Go to **Integrations** → **⋮ (menu)** → **Custom repositories**.
3. Add this repository URL and select **Integration** as the category:
   ```
   https://github.com/kilo323/dominion-sc
   ```
4. Click **Add**, then find **Dominion SC Energy** in the HACS store and click **Download**.
5. Restart Home Assistant.

### Manual

1. Copy the `custom_components/dominionsc` folder into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## Setup

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **Dominion SC Energy**.
3. Enter your Dominion Energy credentials:
   - **Username**
   - **Password**
4. If your account requires two-factor authentication:
   - Select your preferred 2FA method (SMS or email).
   - Enter the verification code when received.
5. Configure options:
   - **Poll interval** (minutes) — how often to check for new data (default: `15`)
   - **Backfill cycles target** — how many previous billing cycles to backfill (default: `6`)
   - **Daily lookback days** — lag-tolerant reconciliation window (default: `5`)

## Entities

After setup, the integration creates a **Dominion SC Energy** device with the following entities:

### Energy Sensors (for Energy Dashboard)

| Entity | Unit | State Class | Description |
|--------|------|-------------|-------------|
| Electric Cumulative Consumption | kWh | `total_increasing` | Running total of electric usage |
| Gas Cumulative Consumption | ft³ | `total_increasing` | Running total of gas usage |
| Electric Cumulative Cost | $ | `total` | Running total of electric cost |
| Gas Cumulative Cost | $ | `total` | Running total of gas cost |

### Diagnostic Sensors

| Entity | State | Attributes | Description |
|--------|-------|------------|-------------|
| Backfill Cycles | Total cycle count | `completed_count`, `incomplete_count`, `completed_cycles` (YYYY-MMM list), `incomplete_cycles` (YYYY-MMM list) | Overview of backfill progress |
| Current Billing Cycle | `MMM D - MMM D` (e.g. `Mar 9 - Apr 4`) | `start`, `end` (raw ISO dates) | Current billing period |

### Buttons

| Entity | Description |
|--------|-------------|
| Run Backfill | Triggers one backfill cycle (append mode) |
| Cleanup External Statistics | Clears legacy external statistics and force-rewrites all historical data |

### Account & Billing Informational Sensors

These additional informational sensors expose account and billing details (useful in dashboards or automations). They are created on the same `Dominion SC Energy` device and are diagnostic/monetary where appropriate.

| Entity | Unit / Type | Description |
|--------|------------:|-------------|
| Last Payment | $ | Most recent payment amount (USD) |
| Account Balance | $ | Current account balance; textual values like `Bill Paid` are reported as `$0.00` |
| Current Cost | $ | Current accumulated cost for billing period (from projection API) |
| Projected Price | $ | Projected total price for the current billing period |
| Days Left | integer | Days remaining in the billing cycle (from projection API) |
| Electric Charges | $ | Electric charge subtotal for the current billing period (daily bill summary) |
| Gas Charges | $ | Gas charge subtotal for the current billing period (daily bill summary) |
| Electric Other Charges | $ | Other electric charges (fees, taxes) in the current bill summary |
| Gas Other Charges | $ | Other gas charges in the current bill summary |
| Total Charges | $ | Combined total bill amount (from daily bill summary) |

These sensors read Dominion's `account_summary`, `bill_projection`, and `current_daily_usage` payloads and perform lightweight parsing (money strings like `$333.19`, and dates such as `Mar 9, 2026`). The integration treats textual `Bill Paid` values as `$0.00` to keep monetary sensors numeric for dashboards and automations.

## Energy Dashboard Configuration

1. Go to **Settings → Dashboards → Energy**.
2. Under **Electricity**:
   - **Grid consumption**: select `Electric Cumulative Consumption`
3. Under **Gas**:
   - **Gas consumption**: select `Gas Cumulative Consumption`

> **Note:** After initial setup, it may take 5–10 minutes for the sensors to appear in the Energy Dashboard picker. The recorder needs at least one statistics compilation cycle.

## Services

### `dominionsc.run_backfill`

Manually trigger a backfill cycle.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `config_entry_id` | string | _(all entries)_ | Target a specific config entry |
| `overwrite` | boolean | `false` | If `true`, re-imports the cycle and replaces prior stored values |
| `allow_initialize_missing` | boolean | `false` | If `true`, allow the service to repopulate the internal `missing_cycles` list when empty (forces initialization). If `false` (default), manual calls will warn-and-exit when there are no incomplete cycles to process. |

### `dominionsc.run_backfill_all`

Manually trigger all currently incomplete backfill cycles.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `config_entry_id` | string | _(all entries)_ | Target a specific config entry |
| `overwrite` | boolean | `false` | If `true`, re-imports all incomplete cycles and replaces prior stored values |

### `dominionsc.rewrite_statistics`

Clears legacy external statistics (`dominionsc:*` IDs) and force-rewrites all historical data into the recorder `sensor.*` statistics path.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `config_entry_id` | string | _(all entries)_ | Target a specific config entry |

## How It Works

### Data Flow

```
Dominion API
    │
    ├─ 15-min interval endpoint ──→ Interval ledger (dedupe by timestamp+fuel)
    │                                    │
    ├─ Daily usage endpoint ──────→ Daily ledger (upsert by date+fuel)
    │                                    │
    └─ Billing cycle backfill ────→ Daily ledger (one cycle per poll)
                                         │
                                         ▼
                                  Synthetic cumulative totals
                                  (monotonic, never decrease)
                                         │
                                    ┌────┴────┐
                                    ▼         ▼
                              HA Sensors   Recorder LTS
                              (live state)  (historical)
```

### Authentication

- On first setup, the config flow handles login and optional 2FA.
- Credentials are stored in the config entry.
- The coordinator re-authenticates automatically on each poll using stored credentials.
- If a refreshed 2FA token is returned, it is persisted back to the config entry.

### Backfill Strategy

- On each scheduled poll, the coordinator processes **at most one** missing billing cycle.
- Cycles are processed oldest-first.
- The **Run Backfill** button (or `run_backfill` service) can be pressed multiple times to accelerate backfill.
- With `overwrite: true`, existing cycle data is replaced (useful for correcting stale data).

### Statistics Import

- Historical data is imported into Home Assistant's long-term statistics at the correct day timestamps.
- The integration uses `async_import_statistics` for initial/rewrite imports and `async_add_external_statistics` for append-only updates.
- Future-dated placeholder rows (zero-value days beyond today) are automatically filtered out.
- If newly backfilled days are older than previously imported days, the integration automatically triggers a full rewrite for that series to maintain cumulative sum continuity.

Additional import/filtering behavior

- Recent days within the configured "daily lookback" window are ignored if Dominion returns only null/zero for both consumption and cost. These all-zero/null rows are considered placeholders and are skipped during daily reconciliation; the lag-safe lookback window will upsert their real values when Dominion supplies them a few days later.
- For finalized/backfill cycles, zero electric consumption for a full day is treated as suspicious and is skipped (true zero electric for a full day is extremely unlikely); zero gas or zero cost on finalized days is trusted as valid.
- Filtering is applied before ledger upsert and before building `StatisticData` for recorder import so that null values (which may be parsed as 0.0) do not create premature zero-value statistics.

### Units & Conversion

- **Electric**: reported in `kWh` (no conversion needed).
- **Gas**: Dominion reports in `CCF`; converted to `ft³` (`CCF × 100`) for Home Assistant's gas device class.

## Debugging

Enable debug logging by adding this to `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.dominionsc: debug
```

Then restart Home Assistant. Debug output covers:
- Authentication state and token refresh
- Backfill cycle selection and row counts
- Daily reconciliation window
- Statistics sync (import counts, rewrite triggers, future-date filtering)
- Future placeholder row skipping

## Options (Reconfigurable)

After setup, you can adjust options via **Settings → Devices & Services → Dominion SC Energy → Configure**:

- **Poll interval** (minutes)
- **Backfill cycles target**
- **Daily lookback days**

Changes take effect on the next poll cycle.

## Troubleshooting

### "No statistics available" in Energy Dashboard
- Wait 5–10 minutes after initial setup for the recorder to compile statistics.
- Verify the sensor state is not `unavailable` or `unknown`.

### "Verification code was not accepted"
- Ensure you entered the code without leading/trailing spaces.
- If the code was valid, this may indicate a post-auth session issue. Check HA logs for the actual error.

### Sensors show data only from today
- Run the **Cleanup External Statistics** button to force-rewrite historical data into the recorder.
- Verify backfill cycles have completed (check the diagnostic sensors).

### Negative values in statistics
- This can occur if backfill cycles were imported out of order. Run **Cleanup External Statistics** to rebuild the cumulative series.

## License

[MIT](LICENSE)
