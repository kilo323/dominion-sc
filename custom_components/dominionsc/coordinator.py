"""Data coordinator for Dominion SC integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
import logging
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_BACKFILL_CYCLES_TARGET,
    CONF_DAILY_LOOKBACK_DAYS,
    CONF_PASSWORD,
    CONF_POLL_MINUTES,
    CONF_TFA_TOKEN,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DEFAULT_BACKFILL_CYCLES_TARGET,
    DEFAULT_DAILY_LOOKBACK_DAYS,
    DEFAULT_POLL_MINUTES,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    STORE_KEY_PREFIX,
    STORE_VERSION,
    TOTAL_ELECTRIC_COST,
    TOTAL_ELECTRIC_KWH,
    TOTAL_GAS_COST,
    TOTAL_GAS_FT3,
)
from .dominion_sc_client import DominionSCClient

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BillingCycle:
    start: date
    end: date

    @property
    def key(self) -> str:
        return f"{self.start.isoformat()}|{self.end.isoformat()}"


class DominionSCCoordinator(DataUpdateCoordinator[dict[str, float]]):
    """Coordinate Dominion SC data fetches and persistent synthetic totals."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.config_entry = entry

        poll_minutes = int(entry.options.get(CONF_POLL_MINUTES, entry.data.get(CONF_POLL_MINUTES, DEFAULT_POLL_MINUTES)))
        update_interval = timedelta(minutes=max(1, poll_minutes))

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=update_interval,
        )

        verify_ssl = bool(entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL))
        self._client = DominionSCClient(verify_ssl=verify_ssl)
        self._store = Store[dict[str, Any]](hass, STORE_VERSION, f"{STORE_KEY_PREFIX}{entry.entry_id}")
        self._state: dict[str, Any] = self._default_state()

    async def async_setup(self) -> None:
        """Load persisted state before first refresh."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self._state = self._merge_state(stored)
        # Recompute totals from any persisted ledgers if totals appear missing
        # or were not written by older state formats. This ensures the
        # synthetic cumulative totals are available immediately after a
        # restart and avoid negative Energy Dashboard deltas when recorder
        # statistics exist but in-memory totals are zero.
        try:
            self._recompute_totals_from_ledgers()
        except Exception:  # defensive: do not let a recompute break startup
            _LOGGER.debug("Could not recompute totals from ledgers on setup", exc_info=True)

        # Ensure backfill cycles are properly initialized after setup
        self._initialize_backfill_cycles()
        
    @property
    def totals(self) -> dict[str, float]:
        return self._state["totals"]

    @property
    def backfill(self) -> dict[str, Any]:
        return self._state["backfill"]

    @property
    def account_summary(self) -> dict[str, Any]:
        """Return the latest fetched account/billing payload.

        This value is populated during updates and may be an empty dict
        if no data has been fetched yet.
        """
        return self._state.get("account_summary", {}) or {}

    @property
    def bill_projection(self) -> dict[str, Any]:
        """Return the latest fetched bill projection payload."""
        return self._state.get("bill_projection", {}) or {}

    @property
    def current_bill_summary(self) -> dict[str, Any]:
        """Return the latest summarized current bill payload (bill_summary)."""
        return self._state.get("current_bill_summary", {}) or {}

    @property
    def current_daily_usage(self) -> dict[str, Any]:
        """Return the latest fetched current daily usage payload."""
        return self._state.get("current_daily_usage", {}) or {}

    @property
    def backfill_cycles_target(self) -> int:
        """Return the currently configured backfill cycles target."""
        return int(
            self.config_entry.options.get(
                CONF_BACKFILL_CYCLES_TARGET,
                self.config_entry.data.get(CONF_BACKFILL_CYCLES_TARGET, DEFAULT_BACKFILL_CYCLES_TARGET),
            )
        )

    @property
    def backfill_summary(self) -> dict[str, Any]:
        """Compute a consistent backfill summary based on current target.

        Returns a dict with:
        - target: configured backfill cycles target
        - eligible_keys: cycle keys eligible for the current target
        - completed_cycles: all-time completed cycle keys (persistent)
        - completed_in_scope: completed cycles that are in the current eligible set
        - incomplete_cycles: eligible cycles that are NOT yet completed
        """
        target = self.backfill_cycles_target
        eligible = self._build_recent_monthly_cycles(
            now_date=datetime.now().date(), target=target
        )
        eligible_keys = [c.key for c in eligible]
        all_completed = set(self.backfill.get("completed_cycles", []))

        completed_in_scope = [k for k in eligible_keys if k in all_completed]
        incomplete = [k for k in eligible_keys if k not in all_completed]

        return {
            "target": target,
            "eligible_keys": eligible_keys,
            "completed_cycles": sorted(all_completed),
            "completed_in_scope": completed_in_scope,
            "incomplete_cycles": incomplete,
        }

    @property
    def current_billing_cycle(self) -> BillingCycle:
        """Return current billing cycle using fetched projection/current payloads.

        Prefer `bill_projection.billStartDateFormatted` / `billEndDateFormatted` (ISO)
        or `billStartDate` / `billEndDate` (human) when available. Fall back to
        current_daily_usage fields, then to month-start -> today.
        """
        bp = self._state.get("bill_projection") or {}
        start = None
        end = None

        def _try_parse(s: str) -> date | None:
            if not s:
                return None
            # Try ISO first
            try:
                return date.fromisoformat(s)
            except Exception:
                pass
            # Try common human-readable format like 'Mar 9, 2026'
            try:
                return datetime.strptime(s, "%b %d, %Y").date()
            except Exception:
                return None

        # Try bill_projection first
        if isinstance(bp, dict):
            sd = bp.get("billStartDateFormatted") or bp.get("billStartDate")
            ed = bp.get("billEndDateFormatted") or bp.get("billEndDate")
            start = _try_parse(sd) if sd else None
            end = _try_parse(ed) if ed else None

        # Debug: log what we found in bill_projection
        if _LOGGER.isEnabledFor(logging.DEBUG):
            try:
                _LOGGER.debug(
                    "Billing cycle source: bill_projection sd=%s ed=%s parsed_start=%s parsed_end=%s",
                    bp.get("billStartDateFormatted") or bp.get("billStartDate"),
                    bp.get("billEndDateFormatted") or bp.get("billEndDate"),
                    start.isoformat() if start else None,
                    end.isoformat() if end else None,
                )
            except Exception:
                _LOGGER.debug("Billing cycle source: bill_projection present but could not log values")

        # Fallback: current_daily_usage payload
        if (not start or not end):
            cd = self._state.get("current_daily_usage") or {}
            sd = cd.get("billStartDateFormatted") or cd.get("billStartDate")
            ed = cd.get("billEndDateFormatted") or cd.get("billEndDate")
            if not start and sd:
                start = _try_parse(sd)
            if not end and ed:
                end = _try_parse(ed)

        # Debug: log what we found in current_daily_usage
        if _LOGGER.isEnabledFor(logging.DEBUG):
            try:
                _LOGGER.debug(
                    "Billing cycle source: current_daily_usage sd=%s ed=%s parsed_start=%s parsed_end=%s",
                    cd.get("billStartDateFormatted") or cd.get("billStartDate"),
                    cd.get("billEndDateFormatted") or cd.get("billEndDate"),
                    start.isoformat() if start else None,
                    end.isoformat() if end else None,
                )
            except Exception:
                _LOGGER.debug("Billing cycle source: current_daily_usage present but could not log values")

        if start and end:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Resolved current billing cycle: start=%s end=%s",
                    start.isoformat(), end.isoformat(),
                )
            return BillingCycle(start=start, end=end)

        # Final fallback: month start to today
        now = datetime.now().date()
        start = date(now.year, now.month, 1)
        return BillingCycle(start=start, end=now)

    @property
    def last_sync(self) -> str | None:
        """Return ISO 8601 timestamp string for last successful sync, or None."""
        return self._state.get("last_sync")

    async def async_run_backfill(
        self,
        overwrite: bool = False,
        cycle_key: str | None = None,
        allow_initialize_missing: bool = False,
    ) -> None:
        """Manually process one backfill cycle."""
        _LOGGER.debug(
            "Manual backfill requested: entry=%s overwrite=%s cycle_key=%s",
            self.config_entry.entry_id,
            overwrite,
            cycle_key,
        )
        await self._ensure_authenticated()
        # When triggered manually via the service/button, callers may choose
        # whether to allow repopulating missing cycles. Pass the flag through
        # to the processing routine.
        await self._process_backfill(
            overwrite=overwrite, cycle_key=cycle_key, allow_initialize_missing=allow_initialize_missing
        )
        await self._sync_external_statistics(force_rewrite=overwrite)
        self._apply_monotonic_guard()
        # Update last_sync because a manual backfill is a successful sync operation
        self._set_last_sync()
        await self._save_state()
        self.async_set_updated_data(dict(self.totals))
        _LOGGER.debug("Manual backfill finished: entry=%s totals=%s", self.config_entry.entry_id, self.totals)

    async def async_rewrite_statistics(self) -> None:
        """Rebuild recorder-backed sensor statistics and clean legacy external series."""
        _LOGGER.debug("Manual statistics rewrite requested: entry=%s", self.config_entry.entry_id)
        await self._clear_external_statistics()
        await self._sync_external_statistics(force_rewrite=True)
        # Update last_sync because rewriting statistics is an intentional sync operation
        self._set_last_sync()
        await self._save_state()
        self.async_set_updated_data(dict(self.totals))
        _LOGGER.debug("Manual statistics rewrite finished: entry=%s", self.config_entry.entry_id)

    async def async_recalculate_backfill_target(self, new_target: int) -> None:
        """Recalculate missing backfill cycles when the target changes.

        If the target increased, newly eligible cycles that are not already
        completed are added to missing_cycles.  If decreased, cycles beyond
        the new target that have not yet been backfilled are removed.
        """
        old_target = int(
            self.config_entry.data.get(
                CONF_BACKFILL_CYCLES_TARGET, DEFAULT_BACKFILL_CYCLES_TARGET
            )
        )
        # Also check what was previously stored in state
        state_target = self._state.get("backfill_cycles_target", old_target)
        if new_target == state_target:
            _LOGGER.debug("Backfill target unchanged at %d", state_target)
            return

        _LOGGER.info(
            "Backfill target changed: %d -> %d, recalculating missing cycles",
            state_target,
            new_target,
        )

        # Compute eligible cycles for the new target
        all_eligible = self._build_recent_monthly_cycles(
            now_date=datetime.now().date(), target=new_target
        )
        all_eligible_keys = [c.key for c in all_eligible]

        completed = set(self._state["backfill"].get("completed_cycles", []))
        new_missing = [k for k in all_eligible_keys if k not in completed]

        old_missing = self._state["backfill"].get("missing_cycles", [])
        _LOGGER.debug(
            "Backfill recalc: eligible=%d, completed=%d, "
            "old_missing=%d, new_missing=%d",
            len(all_eligible_keys),
            len(completed),
            len(old_missing),
            len(new_missing),
        )

        self._state["backfill"]["missing_cycles"] = new_missing
        self._state["backfill_cycles_target"] = new_target
        await self._save_state()
        self.async_set_updated_data(dict(self.totals))

    async def _async_update_data(self) -> dict[str, float]:
        try:
            await self._ensure_authenticated()
            # Fetch account and billing data used by informational sensors.
            # Use the executor because the client uses blocking requests.
            try:
                account_summary = await self.hass.async_add_executor_job(self._client.get_account_summary)
                self._state["account_summary"] = account_summary
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("Could not fetch account_summary: %s", err)

            try:
                current_daily = await self.hass.async_add_executor_job(self._client.get_current_daily_usage)
                # store entire current_daily payload and the summarized bill_summary
                self._state["current_daily_usage"] = current_daily
                self._state["current_bill_summary"] = current_daily.get("bill_summary", {})
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("Could not fetch current_daily_usage: %s", err)

            try:
                bill_projection = await self.hass.async_add_executor_job(self._client.get_bill_projection)
                self._state["bill_projection"] = bill_projection
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("Could not fetch bill_projection: %s", err)
            await self._process_intervals(datetime.now())
            await self._daily_reconcile(datetime.now().date())
            # Process at most one missing backfill cycle per scheduled update
            await self._process_backfill(overwrite=False)
            await self._sync_external_statistics(force_rewrite=False)
            self._apply_monotonic_guard()
            # Update last_sync timestamp for scheduled/automatic update
            self._set_last_sync()
            await self._save_state()
            return dict(self.totals)
        except Exception as err:  # pylint: disable=broad-except
            raise UpdateFailed(str(err)) from err

    async def _clear_external_statistics(self) -> None:
        """Clear custom external dominionsc statistics IDs for this config entry."""
        try:
            from homeassistant.components.recorder import get_instance  # pylint: disable=import-outside-toplevel
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Recorder unavailable while clearing external stats: %s", err)
            return

        statistic_ids = [
            stat_id
            for stat_id in (
                self.get_legacy_external_statistic_id(TOTAL_ELECTRIC_KWH),
                self.get_legacy_external_statistic_id(TOTAL_GAS_FT3),
            )
            if stat_id
        ]
        if not statistic_ids:
            return

        recorder = get_instance(self.hass)
        recorder.async_clear_statistics(statistic_ids)
        _LOGGER.info("Cleared external statistic IDs: %s", statistic_ids)

    def get_statistic_id(self, total_key: str) -> str | None:
        """Return recorder statistic_id (sensor entity based) used by Energy Dashboard."""
        # The actual entity IDs created by this integration use the device
        # name "Dominion SC Energy" and HA's entity naming rules. Those
        # entities are generated as e.g.
        #   sensor.dominion_sc_energy_electric_cumulative_consumption
        # Importing statistics under the plain names (e.g. "sensor.electric_cumulative_consumption")
        # will not link the recorder statistics to the real entity_id and
        # therefore they won't appear as selectable Energy Dashboard sensors.
        #
        # Use the device-based entity IDs so recorder data matches the
        # actual `entity_id` present in the entity registry.
        base = "dominion_sc_energy"
        mapping = {
            TOTAL_ELECTRIC_KWH: f"sensor.{base}_electric_cumulative_consumption",
            TOTAL_GAS_FT3: f"sensor.{base}_gas_cumulative_consumption",
            TOTAL_ELECTRIC_COST: f"sensor.{base}_electric_cumulative_cost",
            TOTAL_GAS_COST: f"sensor.{base}_gas_cumulative_cost",
        }
        return mapping.get(total_key)

    def get_legacy_external_statistic_id(self, total_key: str) -> str | None:
        """Return previous custom external statistic_id used by older integration versions."""
        entry_id = re.sub(r"[^a-z0-9_]", "_", str(self.config_entry.entry_id).lower())
        mapping = {
            TOTAL_ELECTRIC_KWH: f"{DOMAIN}:{entry_id}_electric_consumption",
            TOTAL_GAS_FT3: f"{DOMAIN}:{entry_id}_gas_consumption",
        }
        return mapping.get(total_key)

    async def _sync_external_statistics(self, *, force_rewrite: bool = False) -> None:
        """Import historical daily usage into recorder sensor.* statistics with day timestamps."""
        try:
            from homeassistant.components.recorder import get_instance  # pylint: disable=import-outside-toplevel
            from homeassistant.components.recorder.statistics import (  # pylint: disable=import-outside-toplevel
                StatisticData,
                StatisticMetaData,
                StatisticMeanType,
                async_import_statistics,
            )
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Recorder statistics import unavailable: %s", err)
            return

        statistics_state = self._state.setdefault("statistics_import", {"electric": [], "gas": []})
        fuel_config: list[tuple[str, str, str, str]] = [
            # (series_key, total_key, unit, ledger_name)
            ("electric", TOTAL_ELECTRIC_KWH, "kWh", "daily_ledger"),
            ("gas", TOTAL_GAS_FT3, "ft³", "daily_ledger"),
            ("electric_cost", TOTAL_ELECTRIC_COST, "$", "daily_cost_ledger"),
            ("gas_cost", TOTAL_GAS_COST, "$", "daily_cost_ledger"),
        ]

        for series_key, total_key, unit, ledger_name in fuel_config:
            # For cost series, the ledger keys use "electric|date" / "gas|date" (no "_cost" prefix)
            fuel_prefix = series_key.replace("_cost", "")
            imported_days_raw = statistics_state.setdefault(series_key, [])
            imported_days = set(imported_days_raw if isinstance(imported_days_raw, list) else [])

            daily_points: list[tuple[date, float]] = []
            today = datetime.now().date()
            lookback_days = int(
                self.config_entry.options.get(
                    CONF_DAILY_LOOKBACK_DAYS,
                    self.config_entry.data.get(
                        CONF_DAILY_LOOKBACK_DAYS, DEFAULT_DAILY_LOOKBACK_DAYS,
                    ),
                )
            )
            lookback_cutoff = today - timedelta(days=max(1, lookback_days))

            for key, value in self._state.get(ledger_name, {}).items():
                if not isinstance(key, str) or not key.startswith(f"{fuel_prefix}|"):
                    continue
                day_str = key.split("|", 1)[1]
                try:
                    day = date.fromisoformat(day_str)
                except ValueError:
                    continue

                consumption = max(float(value), 0.0) if value is not None else 0.0

                # Skip today and future dates — data is never ready yet
                if day >= today:
                    _LOGGER.debug(
                        "Statistics sync filtering today/future date: %s=%s (%s)",
                        key, consumption, series_key,
                    )
                    continue

                # Skip recent null/zero rows — Dominion data may arrive later
                # via the lag-safe lookback window
                if day >= lookback_cutoff and consumption == 0.0:
                    _LOGGER.debug(
                        "Statistics sync filtering recent zero row "
                        "(data may arrive later): %s (%s)",
                        key, series_key,
                    )
                    continue

                # For finalized dates: skip zero electric consumption
                # (true zero essentially impossible — HA itself draws power).
                # Trust zero for gas (legitimate in summer) and all cost series.
                is_cost_series = "_cost" in series_key
                if (
                    day < lookback_cutoff
                    and consumption == 0.0
                    and fuel_prefix == "electric"
                    and not is_cost_series
                ):
                    _LOGGER.debug(
                        "Statistics sync filtering finalized zero electric "
                        "consumption (likely data gap): %s",
                        key,
                    )
                    continue

                daily_points.append((day, consumption))

            if not daily_points:
                _LOGGER.debug("Statistics sync skipped for %s: no daily points", series_key)
                continue

            daily_points.sort(key=lambda item: item[0])
            # Build a set of available day strings from the ledger points so
            # we can detect when previously imported days are missing from
            # the current ledger. If any previously imported day is not
            # present, force a rewrite to avoid importing a smaller
            # cumulative sequence (which would create negative deltas).
            point_day_set = {d.isoformat() for d, _ in daily_points}
            running_sum = 0.0
            to_import: list[StatisticData] = []

            statistic_id = self.get_statistic_id(total_key)
            if not statistic_id:
                continue

            _LOGGER.debug(
                "Statistics sync start: series=%s force_rewrite=%s points=%s imported_days=%s statistic_id=%s",
                series_key,
                force_rewrite,
                len(daily_points),
                len(imported_days),
                statistic_id,
            )

            rewrite_for_fuel = force_rewrite
            if not rewrite_for_fuel and imported_days:
                try:
                    max_imported_day = max(date.fromisoformat(day_key) for day_key in imported_days)
                except ValueError:
                    max_imported_day = None
                if max_imported_day is not None:
                    has_older_new_day = any(
                        point_day < max_imported_day and point_day.isoformat() not in imported_days
                        for point_day, _ in daily_points
                    )
                    if has_older_new_day:
                        rewrite_for_fuel = True
                        _LOGGER.info(
                            "Statistics sync forcing rewrite for %s due to newly discovered older days",
                            series_key,
                        )
                # If any previously imported day is missing from the ledger
                # we must force a rewrite. This can happen after state restore
                # / reboot if the ledger was changed or filtered differently
                # and helps avoid importing cumulative values that are lower
                # than what was previously written to recorder.
                if any(day_key not in point_day_set for day_key in imported_days):
                    rewrite_for_fuel = True
                    _LOGGER.info(
                        "Statistics sync forcing rewrite for %s because previously imported days are missing from ledger",
                        series_key,
                    )

            if rewrite_for_fuel:
                imported_days.clear()
                recorder = get_instance(self.hass)
                # NOTE: Despite the async-style name, this is not an awaitable coroutine
                # in current HA recorder API. Do not add `await` here.
                recorder.async_clear_statistics([statistic_id])
                _LOGGER.info("Cleared existing statistics for %s", statistic_id)

            for day, consumption in daily_points:
                running_sum += consumption
                day_key = day.isoformat()
                if day_key in imported_days:
                    continue
                to_import.append(
                    StatisticData(
                        start=datetime.combine(day, time.min, tzinfo=UTC),
                        state=running_sum,
                        sum=running_sum,
                    )
                )
                imported_days.add(day_key)

            if not to_import:
                _LOGGER.debug("Statistics sync no-op for %s: nothing new to import", series_key)
                continue

            # Build a human-friendly name for the metadata
            friendly_label = series_key.replace("_", " ").title()
            metadata = StatisticMetaData(
                has_mean=False,
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"Dominion SC {friendly_label}",
                source="recorder",
                statistic_id=statistic_id,
                unit_class=None,
                unit_of_measurement=unit,
            )

            # NOTE: Despite the async-style name, this is not an awaitable coroutine
            # in current HA recorder API. Do not add `await` here.
            async_import_statistics(self.hass, metadata, to_import)

            statistics_state[series_key] = sorted(imported_days)
            _LOGGER.debug(
                "Statistics sync complete: series=%s mode=%s imported=%s tracked_days=%s",
                series_key,
                "rewrite" if rewrite_for_fuel else "append",
                len(to_import),
                len(statistics_state[series_key]),
            )

    async def _ensure_authenticated(self) -> None:
        username = self.config_entry.data.get(CONF_USERNAME)
        password = self.config_entry.data.get(CONF_PASSWORD)
        stored_tfa_token = self.config_entry.data.get(CONF_TFA_TOKEN)
        if not username or not password:
            raise UpdateFailed("Missing username/password in config entry")

        _LOGGER.debug(
            "Authenticating entry=%s with stored_tfa_token=%s",
            self.config_entry.entry_id,
            bool(stored_tfa_token),
        )

        if stored_tfa_token:
            self._client.tfa_token = str(stored_tfa_token)

        try:
            await self.hass.async_add_executor_job(self._client.login, username, password)
        except Exception as err:  # pylint: disable=broad-except
            raise UpdateFailed(f"Authentication failed: {err}") from err

        refreshed_token = self._client.tfa_token
        if refreshed_token and refreshed_token != stored_tfa_token:
            new_data = dict(self.config_entry.data)
            new_data[CONF_TFA_TOKEN] = refreshed_token
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            _LOGGER.info("Updated stored tfa token for entry=%s", self.config_entry.entry_id)

    async def _process_intervals(self, now_dt: datetime) -> None:
        start = now_dt - timedelta(hours=2)

        electric_payload = await self.hass.async_add_executor_job(
            lambda: self._client.get_hourly_usage(
                measurement_type="ELECTRIC",
                day_start=start,
                day_end=now_dt,
                locale="en_US",
            )
        )
        gas_payload = await self.hass.async_add_executor_job(
            lambda: self._client.get_hourly_usage(
                measurement_type="GAS",
                day_start=start,
                day_end=now_dt,
                locale="en_US",
            )
        )

        rows = self._merge_usage_rows(
            electric_rows=self._parse_usage_rows(electric_payload, fuel="electric"),
            gas_rows=self._parse_usage_rows(gas_payload, fuel="gas"),
        )
        self._state["stats"]["raw_interval_rows"] += len(rows)

        for row in rows:
            interval_end = str(row["interval_end"])
            e_key = f"electric|{interval_end}"
            g_key = f"gas|{interval_end}"

            if e_key not in self._state["interval_ledger"]:
                e_usage = float(row["electric_usage_kwh"])
                self._state["interval_ledger"][e_key] = e_usage
                self._state["totals"][TOTAL_ELECTRIC_KWH] += e_usage
                self._state["stats"]["accepted_interval_rows"] += 1

            if g_key not in self._state["interval_ledger"]:
                g_ft3 = float(row["gas_usage_ccf"]) * 100.0
                self._state["interval_ledger"][g_key] = g_ft3
                self._state["totals"][TOTAL_GAS_FT3] += g_ft3

            if e_key not in self._state["interval_cost_ledger"]:
                e_cost = float(row["electric_cost"])
                self._state["interval_cost_ledger"][e_key] = e_cost
                self._state["totals"][TOTAL_ELECTRIC_COST] += e_cost

            if g_key not in self._state["interval_cost_ledger"]:
                g_cost = float(row["gas_cost"])
                self._state["interval_cost_ledger"][g_key] = g_cost
                self._state["totals"][TOTAL_GAS_COST] += g_cost

    @staticmethod
    def _is_future_placeholder(row: dict[str, float | str], today: date) -> bool:
        """Return True if the row is a zero-value placeholder for a future date."""
        try:
            row_date = date.fromisoformat(str(row["date"]))
        except (ValueError, KeyError):
            return False
        if row_date <= today:
            return False
        # Future date: skip only if all usage/cost values are zero
        usage_sum = (
            abs(float(row.get("electric_usage_kwh", 0)))
            + abs(float(row.get("gas_usage_ccf", 0)))
            + abs(float(row.get("electric_cost", 0)))
            + abs(float(row.get("gas_cost", 0)))
        )
        return usage_sum == 0.0

    def _should_skip_daily_row(
        self,
        row: dict[str, float | str],
        today: date,
        *,
        is_backfill: bool = False,
    ) -> bool:
        """Return True if a daily row should be skipped (not upserted into ledger).

        Filtering rules:
        - Today and future dates: always skip (data is never available yet).
        - Recent dates (within lookback window): skip if ALL consumption AND
          cost are null/zero — Dominion data may arrive later via lag-safe
          reconciliation.  Rows with real data for one fuel but zero for the
          other are kept.
        - If a date falls within the lookback window, skip zero values for that date
          regardless of whether all values are zero or not (to avoid importing
          potentially incomplete data).
        - Finalized dates (backfill): trust zero gas (legitimate in summer)
          and zero cost; skip zero electric consumption (true zero essentially
          impossible — HA itself draws power).

        Args:
            row: Merged daily usage row with electric_usage_kwh, gas_usage_ccf,
                 electric_cost, gas_cost, and date fields.
            today: Current date (UTC).
            is_backfill: True if this row comes from a completed billing cycle
                         (finalized data).

        Returns:
            True if the row should be skipped entirely.
        """
        try:
            row_date = date.fromisoformat(str(row["date"]))
        except (ValueError, KeyError):
            return False

        # Always skip today and future dates — data is never ready yet
        if row_date >= today:
            _LOGGER.debug(
                "Skipping row for today/future date: %s", row["date"],
            )
            return True

        e_kwh = float(row.get("electric_usage_kwh", 0) or 0)
        g_ccf = float(row.get("gas_usage_ccf", 0) or 0)
        e_cost = float(row.get("electric_cost", 0) or 0)
        g_cost = float(row.get("gas_cost", 0) or 0)

        all_zero = (e_kwh == 0.0 and g_ccf == 0.0 and e_cost == 0.0 and g_cost == 0.0)

        if not is_backfill:
            # Recent data within lookback window: skip only if everything is
            # null/zero — this means Dominion hasn't delivered data yet.
            # If at least one fuel has data, keep the row (the other fuel
            # may legitimately be zero).
            lookback_days = int(
                self.config_entry.options.get(
                    CONF_DAILY_LOOKBACK_DAYS,
                    self.config_entry.data.get(
                        CONF_DAILY_LOOKBACK_DAYS, DEFAULT_DAILY_LOOKBACK_DAYS,
                    ),
                )
            )
            lookback_cutoff = today - timedelta(days=max(1, lookback_days))
            if row_date >= lookback_cutoff and all_zero:
                _LOGGER.debug(
                    "Skipping recent all-zero row (data may arrive later): %s",
                    row["date"],
                )
                return True
            # NOTE: Per-fuel zero filtering (e.g. skip zero electric but keep
            # gas) is handled at the individual _upsert_daily call sites in
            # _daily_reconcile and _process_backfill, not here.  This method
            # only skips the entire merged row when ALL values are zero.
        else:
            # Backfill / finalized data: trust zero for gas and cost,
            # but skip if ALL values are zero (likely a data gap in API)
            if all_zero:
                _LOGGER.debug(
                    "Skipping finalized all-zero row (likely data gap): %s",
                    row["date"],
                )
                return True

        return False

    async def _daily_reconcile(self, now_date: date) -> None:
        # Historically this routine fetched only a short lookback window.
        # Change: scheduled reconciles should fetch the entire current billing
        # cycle so the integration gets all days for the active cycle.
        # The `current_billing_cycle` prefers `bill_projection`/`current_daily_usage`.
        lookback = int(
            self.config_entry.options.get(
                CONF_DAILY_LOOKBACK_DAYS,
                self.config_entry.data.get(CONF_DAILY_LOOKBACK_DAYS, DEFAULT_DAILY_LOOKBACK_DAYS),
            )
        )
        cycle = self.current_billing_cycle
        start = cycle.start
        end = cycle.end
        _LOGGER.debug(
            "Daily reconcile using billing cycle: start=%s end=%s (configured lookback=%s days)",
            start.isoformat(), end.isoformat(), lookback,
        )
        rows = await self._fetch_daily_rows(start, end)

        lookback_cutoff = now_date - timedelta(days=max(1, lookback))

        for row in rows:
            if self._is_future_placeholder(row, now_date):
                _LOGGER.debug("Skipping future zero-value placeholder row: %s", row.get("date"))
                continue
            if self._should_skip_daily_row(row, now_date, is_backfill=False):
                continue
            day_key = row["date"]

            # --- Per-fuel zero filtering within the lookback window ---
            # Dominion lags ~2 days; zeros within the window are placeholders.
            # Filter per-fuel so real data for one fuel isn't blocked by
            # the other fuel's placeholder zero.
            try:
                row_date = date.fromisoformat(str(day_key))
            except (ValueError, TypeError):
                row_date = None
            in_lookback = row_date is not None and row_date >= lookback_cutoff

            e_kwh = float(row["electric_usage_kwh"])
            g_ft3 = float(row["gas_usage_ccf"]) * 100.0
            e_cost = float(row["electric_cost"])
            g_cost = float(row["gas_cost"])

            # Electric consumption: skip zero within lookback (lag placeholder)
            if in_lookback and e_kwh == 0.0:
                _LOGGER.debug(
                    "Daily reconcile: skipping zero electric consumption for %s (lookback placeholder)",
                    day_key,
                )
            else:
                self._upsert_daily(f"electric|{day_key}", e_kwh, True, TOTAL_ELECTRIC_KWH)

            # Electric cost: skip zero within lookback (lag placeholder)
            if in_lookback and e_cost == 0.0:
                _LOGGER.debug(
                    "Daily reconcile: skipping zero electric cost for %s (lookback placeholder)",
                    day_key,
                )
            else:
                self._upsert_daily_cost(f"electric|{day_key}", e_cost, True, TOTAL_ELECTRIC_COST)

            # Gas consumption: skip zero within lookback only if cost is also zero
            # (zero gas is legitimate in summer when cost is also zero, but within
            # the lookback window it's more likely a placeholder)
            if in_lookback and g_ft3 == 0.0 and g_cost == 0.0:
                _LOGGER.debug(
                    "Daily reconcile: skipping zero gas consumption+cost for %s (lookback placeholder)",
                    day_key,
                )
            else:
                self._upsert_daily(f"gas|{day_key}", g_ft3, True, TOTAL_GAS_FT3)

            # Gas cost: skip zero within lookback only if consumption is also zero
            if in_lookback and g_cost == 0.0 and g_ft3 == 0.0:
                pass  # already logged above
            else:
                self._upsert_daily_cost(f"gas|{day_key}", g_cost, True, TOTAL_GAS_COST)

    async def _process_backfill(
        self,
        overwrite: bool,
        cycle_key: str | None = None,
        allow_initialize_missing: bool = True,
    ) -> None:
        """Process a backfill cycle.

        allow_initialize_missing controls whether the missing_cycles list may be
        lazily repopulated when empty. Scheduled automatic updates should allow
        initialization; manual service/button invocations will pass
        allow_initialize_missing=False so they can early-exit when there are no
        incomplete cycles to process.
        """
        if allow_initialize_missing:
            self._initialize_backfill_cycles()
        else:
            # Manual invocation: if there are no missing cycles, warn and exit
            backfill_state = self._state.get("backfill", {})
            missing = backfill_state.get("missing_cycles", []) if isinstance(backfill_state, dict) else []
            if not missing:
                _LOGGER.warning(
                    "Manual backfill requested but no incomplete backfill cycles to process for entry=%s",
                    self.config_entry.entry_id,
                )
                return

        cycle = self._pick_cycle_for_backfill(overwrite=overwrite, cycle_key=cycle_key)
        if cycle is None:
            _LOGGER.debug("Backfill skipped: no eligible cycle (overwrite=%s)", overwrite)
            return

        _LOGGER.debug("Backfill cycle selected: %s (overwrite=%s)", cycle.key, overwrite)

        rows = await self._fetch_daily_rows(cycle.start, cycle.end)
        if not rows:
            _LOGGER.warning(
                "Backfill cycle %s returned no rows — marking as complete to avoid infinite retries. "
                "Check API or payload availability.",
                cycle.key
            )
            # Even if no rows returned, mark this cycle as complete to prevent stuck retries
            backfill = self._state["backfill"]
            if cycle.key in backfill["missing_cycles"]:
                backfill["missing_cycles"].remove(cycle.key)
            if cycle.key not in backfill["completed_cycles"]:
                backfill["completed_cycles"].append(cycle.key)
                backfill["cycles_completed"] += 1
            return

        today = datetime.now().date()
        _LOGGER.debug("Backfill cycle %s processing %s daily rows", cycle.key, len(rows))

        for row in rows:
            if self._is_future_placeholder(row, today):
                _LOGGER.debug("Backfill skipping future zero-value placeholder row: %s", row.get("date"))
                continue
            if self._should_skip_daily_row(row, today, is_backfill=True):
                continue
            day_key = row["date"]

            e_kwh = float(row["electric_usage_kwh"])
            g_ft3 = float(row["gas_usage_ccf"]) * 100.0
            e_cost = float(row["electric_cost"])
            g_cost = float(row["gas_cost"])

            # --- Per-fuel zero filtering for finalized/backfill cycles ---
            # For finalized data, zero electric consumption is suspect
            # (true zero essentially impossible — HA itself draws power).
            # Trust zero for gas (legitimate in summer) and all cost series.
            if e_kwh == 0.0:
                _LOGGER.debug(
                    "Backfill: skipping zero electric consumption for %s "
                    "(finalized cycle — suspect placeholder)",
                    day_key,
                )
            else:
                self._upsert_daily(f"electric|{day_key}", e_kwh, overwrite, TOTAL_ELECTRIC_KWH)

            # Electric cost: skip if electric consumption was zero (same placeholder)
            if e_kwh == 0.0 and e_cost == 0.0:
                _LOGGER.debug(
                    "Backfill: skipping zero electric cost for %s (consumption also zero)",
                    day_key,
                )
            else:
                self._upsert_daily_cost(f"electric|{day_key}", e_cost, overwrite, TOTAL_ELECTRIC_COST)

            # Gas: trust zero consumption and cost (legitimate in summer)
            self._upsert_daily(f"gas|{day_key}", g_ft3, overwrite, TOTAL_GAS_FT3)
            self._upsert_daily_cost(f"gas|{day_key}", g_cost, overwrite, TOTAL_GAS_COST)

        backfill = self._state["backfill"]
        if cycle.key in backfill["missing_cycles"]:
            backfill["missing_cycles"].remove(cycle.key)
        if cycle.key not in backfill["completed_cycles"]:
            backfill["completed_cycles"].append(cycle.key)
            backfill["cycles_completed"] += 1
        _LOGGER.debug(
            "Backfill cycle complete: %s cycles_completed=%s missing_remaining=%s",
            cycle.key,
            backfill["cycles_completed"],
            len(backfill["missing_cycles"]),
        )

    async def _fetch_daily_rows(self, start: date, end: date) -> list[dict[str, float | str]]:
        start_dt = datetime.combine(start, time.min, tzinfo=UTC)
        end_dt = datetime.combine(end, time.max, tzinfo=UTC)
        # If the requested end date is in the past (strictly before today) we
        # are asking for a completed billing period / historical data. In that
        # case request the "previous"/completed cycle from Bidgely by setting
        # current_cycle=False (which maps to skip_ongoing_cycle=True). For any
        # request that includes today or a future date, keep current_cycle=True
        # so the ongoing active cycle is returned.
        today = datetime.now().date()
        current_cycle_flag = False if end < today else True

        electric_payload = await self.hass.async_add_executor_job(
            lambda: self._client.get_daily_usage(
                measurement_type="ELECTRIC",
                current_cycle=current_cycle_flag,
                start=start_dt,
                end=end_dt,
                locale="en_US",
            )
        )
        gas_payload = await self.hass.async_add_executor_job(
            lambda: self._client.get_daily_usage(
                measurement_type="GAS",
                current_cycle=current_cycle_flag,
                start=start_dt,
                end=end_dt,
                locale="en_US",
            )
        )

        # Temporary debug logging: show whether we requested the current
        # cycle or the completed (previous) cycle and how many usage rows
        # were returned for each fuel. This helps diagnose empty backfill
        # payloads when running historical imports.
        try:
            e_rows = (electric_payload.get("payload") or {}).get("usageChartDataList") or [] if isinstance(electric_payload, dict) else []
            g_rows = (gas_payload.get("payload") or {}).get("usageChartDataList") or [] if isinstance(gas_payload, dict) else []
            _LOGGER.debug(
                "Backfill fetch debug: start=%s end=%s current_cycle=%s electric_rows=%d gas_rows=%d",
                start.isoformat(), end.isoformat(), current_cycle_flag, len(e_rows), len(g_rows),
            )
        except Exception:  # Defensive: logging must not break processing
            _LOGGER.debug(
                "Backfill fetch debug: start=%s end=%s current_cycle=%s (could not compute row counts)",
                start.isoformat(), end.isoformat(), current_cycle_flag,
            )
        return self._merge_usage_rows(
            electric_rows=self._parse_usage_rows(electric_payload, fuel="electric"),
            gas_rows=self._parse_usage_rows(gas_payload, fuel="gas"),
        )

    @staticmethod
    def _parse_usage_rows(payload: Any, fuel: str) -> list[dict[str, float | str]]:
        rows: list[dict[str, float | str]] = []
        chart_rows: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            chart_rows = (payload.get("payload") or {}).get("usageChartDataList") or []

        for row in chart_rows:
            interval_end = row.get("intervalEndDate") or row.get("intervalEnd")
            interval_end_s = str(interval_end)
            day_key = interval_end_s.split(" ")[0]
            consumption = max(float(row.get("consumption") or 0.0), 0.0)
            cost = max(float(row.get("cost") or 0.0), 0.0)

            rows.append(
                {
                    "interval_end": interval_end_s,
                    "date": day_key,
                    "electric_usage_kwh": consumption if fuel == "electric" else 0.0,
                    "gas_usage_ccf": consumption if fuel == "gas" else 0.0,
                    "electric_cost": cost if fuel == "electric" else 0.0,
                    "gas_cost": cost if fuel == "gas" else 0.0,
                }
            )

        return rows

    @staticmethod
    def _merge_usage_rows(
        electric_rows: list[dict[str, float | str]],
        gas_rows: list[dict[str, float | str]],
    ) -> list[dict[str, float | str]]:
        merged: dict[str, dict[str, float | str]] = {}

        def _upsert(row: dict[str, float | str]) -> None:
            key = str(row["interval_end"])
            if key not in merged:
                merged[key] = {
                    "interval_end": row["interval_end"],
                    "date": row["date"],
                    "electric_usage_kwh": 0.0,
                    "gas_usage_ccf": 0.0,
                    "electric_cost": 0.0,
                    "gas_cost": 0.0,
                }
            merged_row = merged[key]
            merged_row["electric_usage_kwh"] = float(merged_row["electric_usage_kwh"]) + float(row["electric_usage_kwh"])
            merged_row["gas_usage_ccf"] = float(merged_row["gas_usage_ccf"]) + float(row["gas_usage_ccf"])
            merged_row["electric_cost"] = float(merged_row["electric_cost"]) + float(row["electric_cost"])
            merged_row["gas_cost"] = float(merged_row["gas_cost"]) + float(row["gas_cost"])

        for row in electric_rows:
            _upsert(row)
        for row in gas_rows:
            _upsert(row)

        return [merged[key] for key in sorted(merged.keys())]

    def _initialize_backfill_cycles(self) -> None:
        backfill = self._state["backfill"]

        target = int(
            self.config_entry.options.get(
                CONF_BACKFILL_CYCLES_TARGET,
                self.config_entry.data.get(CONF_BACKFILL_CYCLES_TARGET, DEFAULT_BACKFILL_CYCLES_TARGET),
            )
        )
        # Always regenerate from current target (don't return early if cycles exist)
        # This ensures that when user updates target or config is refreshed,
        # the cycle list is regenerated and old/stale cycles are replaced
        cycles = self._build_recent_monthly_cycles(now_date=datetime.now().date(), target=target)
        eligible_keys = [cycle.key for cycle in cycles]
        completed = set(backfill.get("completed_cycles", []))

        # Only add cycles that haven't already been completed
        backfill["missing_cycles"] = [k for k in eligible_keys if k not in completed]
        _LOGGER.debug(
            "Initialized backfill cycles: eligible=%d completed=%d missing=%d target=%d",
            len(eligible_keys),
            len(completed),
            len(backfill["missing_cycles"]),
            target,
        )
        if backfill["missing_cycles"]:
            _LOGGER.debug(
                "First 3 missing cycles (oldest to newest): %s",
                backfill["missing_cycles"][:3],
            )

    @staticmethod
    def _build_recent_monthly_cycles(now_date: date, target: int) -> list[BillingCycle]:
        """Build a list of recent complete calendar month cycles.
        
        Backfill targets completed past cycles only. The current month is handled
        by regular polling, so it's excluded from the backfill list.
        """
        cycles: list[BillingCycle] = []
        if target <= 0:
            return cycles

        # Start from the first day of the previous month (exclude current month)
        month_start = date(now_date.year, now_date.month, 1)
        current = month_start - timedelta(days=1)  # Last day of previous month

        while len(cycles) < target:
            cycle_start = date(current.year, current.month, 1)
            cycle_end = date(current.year, current.month, current.day)
            cycles.append(BillingCycle(start=cycle_start, end=cycle_end))
            current = cycle_start - timedelta(days=1)

        cycles.reverse()
        return cycles

    def _pick_cycle_for_backfill(self, overwrite: bool, cycle_key: str | None) -> BillingCycle | None:
        backfill = self._state["backfill"]

        if cycle_key:
            start_s, end_s = cycle_key.split("|")
            return BillingCycle(start=date.fromisoformat(start_s), end=date.fromisoformat(end_s))

        if overwrite and backfill["completed_cycles"]:
            start_s, end_s = backfill["completed_cycles"][0].split("|")
            return BillingCycle(start=date.fromisoformat(start_s), end=date.fromisoformat(end_s))

        if not backfill["missing_cycles"]:
            return None

        start_s, end_s = backfill["missing_cycles"][0].split("|")
        return BillingCycle(start=date.fromisoformat(start_s), end=date.fromisoformat(end_s))

    def _upsert_daily(self, key: str, value: float, overwrite: bool, total_key: str) -> None:
        value = max(float(value), 0.0)
        ledger = self._state["daily_ledger"]
        if key not in ledger:
            ledger[key] = value
            self._state["totals"][total_key] += value
            return
        if overwrite:
            old = float(ledger[key])
            ledger[key] = value
            self._state["totals"][total_key] += value - old

    def _upsert_daily_cost(self, key: str, value: float, overwrite: bool, total_key: str) -> None:
        value = max(float(value), 0.0)
        ledger = self._state["daily_cost_ledger"]
        if key not in ledger:
            ledger[key] = value
            self._state["totals"][total_key] += value
            return
        if overwrite:
            old = float(ledger[key])
            ledger[key] = value
            self._state["totals"][total_key] += value - old

    def _apply_monotonic_guard(self) -> None:
        totals = self._state["totals"]
        last = self._state["last_totals"]
        for key in (TOTAL_ELECTRIC_KWH, TOTAL_GAS_FT3, TOTAL_ELECTRIC_COST, TOTAL_GAS_COST):
            totals[key] = max(float(totals[key]), float(last.get(key, 0.0)))
            last[key] = float(totals[key])

    def _set_last_sync(self) -> None:
        """Set the last_sync timestamp in persistent state to now (ISO 8601)."""
        try:
            # Prefer timezone-aware ISO format
            self._state["last_sync"] = datetime.now().astimezone().isoformat()
        except Exception:
            # Fallback to naive ISO format
            self._state["last_sync"] = datetime.now().isoformat()

    async def _save_state(self) -> None:
        await self._store.async_save(self._state)

    def _default_state(self) -> dict[str, Any]:
        return {
            "interval_ledger": {},
            "interval_cost_ledger": {},
            "daily_ledger": {},
            "daily_cost_ledger": {},
            "backfill": {
                "cycles_completed": 0,
                "missing_cycles": [],
                "completed_cycles": [],
            },
            "totals": {
                TOTAL_ELECTRIC_KWH: 0.0,
                TOTAL_GAS_FT3: 0.0,
                TOTAL_ELECTRIC_COST: 0.0,
                TOTAL_GAS_COST: 0.0,
            },
            "last_totals": {
                TOTAL_ELECTRIC_KWH: 0.0,
                TOTAL_GAS_FT3: 0.0,
                TOTAL_ELECTRIC_COST: 0.0,
                TOTAL_GAS_COST: 0.0,
            },
            "stats": {
                "raw_interval_rows": 0,
                "accepted_interval_rows": 0,
            },
            "statistics_import": {
                "electric": [],
                "gas": [],
            },
            # ISO 8601 timestamp of the last successful sync/update
            "last_sync": None,
            "statistics_rewrite_once_done": False,
            # Latest fetched account/billing payloads (for informational sensors)
            "account_summary": {},
            "bill_projection": {},
            "current_daily_usage": {},
            "current_bill_summary": {},
        }

    def _recompute_totals_from_ledgers(self) -> None:
        """Recompute totals from persisted ledgers.

        Some older or partial stored states may be missing the pre-computed
        `totals` keys. Recomputing from `daily_ledger`, `interval_ledger`,
        and the cost ledgers guarantees the integration exposes monotonic
        cumulative totals immediately after startup.
        """
        totals = {
            TOTAL_ELECTRIC_KWH: 0.0,
            TOTAL_GAS_FT3: 0.0,
            TOTAL_ELECTRIC_COST: 0.0,
            TOTAL_GAS_COST: 0.0,
        }

        # Sum daily and interval ledgers (guarding types)
        for key, val in (self._state.get("daily_ledger", {}) or {}).items():
            try:
                if isinstance(key, str) and key.startswith("electric|"):
                    totals[TOTAL_ELECTRIC_KWH] += float(val or 0.0)
                elif isinstance(key, str) and key.startswith("gas|"):
                    totals[TOTAL_GAS_FT3] += float(val or 0.0)
            except Exception:
                continue

        for key, val in (self._state.get("interval_ledger", {}) or {}).items():
            try:
                if isinstance(key, str) and key.startswith("electric|"):
                    totals[TOTAL_ELECTRIC_KWH] += float(val or 0.0)
                elif isinstance(key, str) and key.startswith("gas|"):
                    totals[TOTAL_GAS_FT3] += float(val or 0.0)
            except Exception:
                continue

        # Sum cost ledgers
        for key, val in (self._state.get("daily_cost_ledger", {}) or {}).items():
            try:
                if isinstance(key, str) and key.startswith("electric|"):
                    totals[TOTAL_ELECTRIC_COST] += float(val or 0.0)
                elif isinstance(key, str) and key.startswith("gas|"):
                    totals[TOTAL_GAS_COST] += float(val or 0.0)
            except Exception:
                continue

        for key, val in (self._state.get("interval_cost_ledger", {}) or {}).items():
            try:
                if isinstance(key, str) and key.startswith("electric|"):
                    totals[TOTAL_ELECTRIC_COST] += float(val or 0.0)
                elif isinstance(key, str) and key.startswith("gas|"):
                    totals[TOTAL_GAS_COST] += float(val or 0.0)
            except Exception:
                continue

        # Respect monotonicity vs any recorded last_totals
        last = self._state.get("last_totals", {}) or {}
        for k in list(totals.keys()):
            totals[k] = max(float(totals[k]), float(last.get(k, 0.0)))

        self._state["totals"] = totals
        # Also set last_totals so the monotonic guard uses this baseline
        self._state["last_totals"] = {k: float(v) for k, v in totals.items()}

    def _merge_state(self, stored: dict[str, Any]) -> dict[str, Any]:
        merged = self._default_state()
        for key, value in stored.items():
            if key in {"interval_ledger", "interval_cost_ledger", "daily_ledger", "daily_cost_ledger", "totals", "last_totals", "stats", "backfill", "statistics_import"} and isinstance(value, dict):
                merged[key].update(value)
            else:
                merged[key] = value
        merged["statistics_rewrite_once_done"] = bool(merged.get("statistics_rewrite_once_done", False))
        return merged
