"""Sensor platform for Dominion SC Energy."""

from __future__ import annotations

import logging
import math
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CURRENCY_DOLLAR, EntityCategory, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    COORDINATOR,
    DOMAIN,
    TOTAL_ELECTRIC_COST,
    TOTAL_ELECTRIC_KWH,
    TOTAL_GAS_COST,
    TOTAL_GAS_FT3,
)
from .coordinator import DominionSCCoordinator

_LOGGER = logging.getLogger(__name__)


def _format_cycle_label(cycle_key: str) -> str:
    """Format a billing cycle key as YYYY-MMM (e.g., 2026-Jan).

    Accepts cycle keys in 'YYYY-MM-DD|YYYY-MM-DD' or ISO 'YYYY-MM-DD' format.
    Uses the start date of the cycle. Falls back to raw string on parse failure.
    """
    try:
        date_str = cycle_key.split("|")[0] if "|" in cycle_key else cycle_key
        dt = datetime.fromisoformat(date_str)
        return dt.strftime("%Y-%b")  # e.g., "2026-Jan"
    except (ValueError, TypeError, IndexError):
        _LOGGER.warning("Could not parse cycle key '%s', using raw value", cycle_key)
        return str(cycle_key)


def _format_billing_date(d: date) -> str:
    """Format a date as 'MMM D' (e.g., 'Mar 9'), no zero-padding."""
    return f"{d.strftime('%b')} {d.day}"


# ---------------------------------------------------------------------------
# Energy sensor descriptions (required for Energy Dashboard)
# ---------------------------------------------------------------------------

SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key=TOTAL_ELECTRIC_KWH,
        name="Electric Cumulative Consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:flash",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key=TOTAL_GAS_FT3,
        name="Gas Cumulative Consumption",
        native_unit_of_measurement=UnitOfVolume.CUBIC_FEET,
        icon="mdi:fire",
        device_class=SensorDeviceClass.GAS,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key=TOTAL_ELECTRIC_COST,
        name="Electric Cumulative Cost",
        native_unit_of_measurement=CURRENCY_DOLLAR,
        icon="mdi:currency-usd",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
    ),
    SensorEntityDescription(
        key=TOTAL_GAS_COST,
        name="Gas Cumulative Cost",
        native_unit_of_measurement=CURRENCY_DOLLAR,
        icon="mdi:currency-usd",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    coordinator: DominionSCCoordinator = runtime[COORDINATOR]
    entities: list[SensorEntity] = [DominionSCTotalSensor(coordinator, entry, desc) for desc in SENSORS]
    entities.extend(
        [
            DominionSCBackfillCyclesSensor(coordinator, entry),
            DominionSCBackfillRemainingSensor(coordinator, entry),
            DominionSCCurrentBillingCycleSensor(coordinator, entry),
            DominionSCLastSyncSensor(coordinator, entry),
        ]
    )
    # informational account/billing sensors
    entities.extend(
        [
            DominionSCBillDueDateSensor(coordinator, entry),
            DominionSCLastPaymentSensor(coordinator, entry),
            DominionSCAccountBalanceSensor(coordinator, entry),
            DominionSCCurrentCostSensor(coordinator, entry),
            DominionSCProjectedPriceSensor(coordinator, entry),
            DominionSCDaysLeftSensor(coordinator, entry),
            DominionSCElectricChargesSensor(coordinator, entry),
            DominionSCGasChargesSensor(coordinator, entry),
            DominionSCElectricOtherChargesSensor(coordinator, entry),
            DominionSCGasOtherChargesSensor(coordinator, entry),
            DominionSCTotalChargesSensor(coordinator, entry),
        ]
    )
    async_add_entities(entities)


class DominionSCTotalSensor(
    CoordinatorEntity[DominionSCCoordinator],
    RestoreEntity,
    SensorEntity,
):
    """Representation of a Dominion SC cumulative total sensor."""

    _attr_has_entity_name = True
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        coordinator: DominionSCCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._key = description.key
        self._attr_name = description.name
        self._attr_unique_id = f"{entry.entry_id}_{self._key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Restore previous state and clamp totals to monotonic baseline on reboot."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if not last_state or last_state.state in {None, "unknown", "unavailable"}:
            return

        try:
            restored_value = float(last_state.state)
        except (TypeError, ValueError):
            _LOGGER.debug(
                "Skipping restore for %s: non-numeric last state=%s",
                self.entity_id,
                last_state.state,
            )
            return

        if not math.isfinite(restored_value) or restored_value < 0.0:
            _LOGGER.debug(
                "Skipping restore for %s: invalid restored value=%s",
                self.entity_id,
                restored_value,
            )
            return

        self._apply_restored_native_value(restored_value)

    def _apply_restored_native_value(self, restored_value: float) -> None:
        """Apply restored value as lower-bound to preserve monotonic totals."""
        current = float(self.coordinator.totals.get(self._key, 0.0))
        if restored_value <= current:
            return

        self.coordinator.totals[self._key] = restored_value

        # Keep coordinator last_totals in sync when available so subsequent
        # refresh cycles continue monotonic progression.
        state_obj = getattr(self.coordinator, "_state", None)
        if isinstance(state_obj, dict):
            last_totals = state_obj.get("last_totals")
            if isinstance(last_totals, dict):
                last_totals[self._key] = max(
                    float(last_totals.get(self._key, 0.0)),
                    restored_value,
                )

        _LOGGER.warning(
            "Monotonic restore clamp applied for %s after startup: loaded_total=%.3f restored_state=%.3f",
            self._key,
            current,
            restored_value,
        )
        self._schedule_startup_statistics_rewrite()

    def _schedule_startup_statistics_rewrite(self) -> None:
        """Schedule a one-time startup statistics rewrite after clamp detection.

        If a reboot-time dip already produced bad recorder statistics for today,
        rewriting historical statistics helps self-heal dashboard totals.
        """
        if bool(getattr(self.coordinator, "_startup_statistics_rewrite_scheduled", False)):
            return

        hass = getattr(self.coordinator, "hass", None)
        if hass is None or not hasattr(hass, "async_create_task"):
            return

        setattr(self.coordinator, "_startup_statistics_rewrite_scheduled", True)
        _LOGGER.warning(
            "Scheduling one-time statistics rewrite after startup clamp for %s",
            self._key,
        )
        hass.async_create_task(self.coordinator.async_rewrite_statistics())

    @property
    def native_value(self) -> float:
        return round(float(self.coordinator.totals.get(self._key, 0.0)), 3)


class DominionSCBackfillCyclesSensor(CoordinatorEntity[DominionSCCoordinator], SensorEntity):
    """Total backfill cycle count with completed/incomplete detail in attributes.

    State = newly configured backfill cycles target.
    Attributes:
      - completed_count: number of cycles that have been successfully backfilled
        (across all time, including before and after configure/reconfigure).
      - completed_cycles: persistent list of all completed cycle labels (YYYY-MMM).
      - incomplete_count: number of eligible cycles not yet completed.
      - incomplete_cycles: eligible cycles that are not in completed_cycles.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:pound"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Backfill Cycles"
        self._attr_unique_id = f"{entry.entry_id}_backfill_cycles"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> int:
        """Return the configured backfill cycles target value."""
        return self.coordinator.backfill_cycles_target

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return completed/incomplete counts and cycle lists as YYYY-MMM."""
        summary = self.coordinator.backfill_summary
        completed_labels = [_format_cycle_label(c) for c in summary["completed_cycles"]]
        incomplete_labels = [_format_cycle_label(c) for c in summary["incomplete_cycles"]]
        return {
            "completed_count": len(summary["completed_cycles"]),
            "incomplete_count": len(summary["incomplete_cycles"]),
            "completed_cycles": completed_labels,
            "incomplete_cycles": incomplete_labels,
        }


class DominionSCBackfillRemainingSensor(CoordinatorEntity[DominionSCCoordinator], SensorEntity):
    """Sensor exposing the number of backfill cycles remaining (incomplete_count).

    This sensor's state is an integer count so it appears directly on the
    integration's device overview page.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:counter"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Backfill Cycles Remaining"
        self._attr_unique_id = f"{entry.entry_id}_backfill_cycles_remaining"
        # unit is a simple count of cycles (no unit_of_measurement for counts)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> int | None:
        """Return count of incomplete (missing) backfill cycles or None if unavailable."""
        summary = self.coordinator.backfill_summary
        return len(summary.get("incomplete_cycles", [])) if summary is not None else None

class DominionSCCurrentBillingCycleSensor(CoordinatorEntity[DominionSCCoordinator], SensorEntity):
    """Current billing cycle formatted as 'MMM D - MMM D' (e.g., 'Mar 9 - Apr 4')."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-range"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Current Billing Cycle"
        self._attr_unique_id = f"{entry.entry_id}_current_billing_cycle"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> str | None:
        """Return current billing cycle as 'MMM D - MMM D' (e.g., 'Mar 9 - Apr 4')."""
        cycle = self.coordinator.current_billing_cycle
        if cycle and cycle.start and cycle.end:
            return f"{_format_billing_date(cycle.start)} - {_format_billing_date(cycle.end)}"
        return None

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Return raw start/end ISO dates as attributes."""
        cycle = self.coordinator.current_billing_cycle
        return {
            "start": cycle.start.isoformat() if cycle else None,
            "end": cycle.end.isoformat() if cycle else None,
        }


class DominionSCLastSyncSensor(CoordinatorEntity[DominionSCCoordinator], SensorEntity):
    """Sensor exposing the last successful sync time for the integration."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-sync"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Last Sync"
        self._attr_unique_id = f"{entry.entry_id}_last_sync"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> datetime | None:
        """Return ISO 8601 timestamp of last successful sync, or None."""
        # Coordinator stores the last_sync as ISO8601 string (or None)
        return datetime.fromisoformat(self.coordinator.last_sync) if self.coordinator.last_sync else None


# --------------------------- Informational sensors ------------------------


def _parse_money(value: str | float | None) -> float | None:
    """Parse a money string like "$123.45" or pass-through numeric values.

    Returns a float or None if the input is empty/unparseable.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        s = str(value).strip()
        if not s:
            return None
        # textual indicators of zero/paid
        if any(term in s.lower() for term in ("paid", "bill paid")):
            return 0.0
        s = s.replace("$", "").replace(",", "")
        return float(s)
    except Exception:
        _LOGGER.warning("Could not parse money value: %s", value)
        return None


def _parse_due_date(value: str | None) -> date | None:
    """Try to parse a due date expressed as common strings.

    Accepts formats like 'Mar 9, 2026' or '2026-03-09'. Returns a date or None.
    """
    if not value:
        return None
    s = str(value).strip()
    # Try ISO first
    try:
        return date.fromisoformat(s)
    except Exception:
        pass
    try:
        return datetime.strptime(s, "%b %d, %Y").date()
    except Exception:
        pass
    _LOGGER.warning("Could not parse due date: %s", s)
    return None





class DominionSCBillDueDateSensor(CoordinatorEntity[DominionSCCoordinator], SensorEntity):
    """Bill due date (date only)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Bill Due Date"
        self._attr_unique_id = f"{entry.entry_id}_bill_due_date"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> str | None:
        acct = self.coordinator.account_summary
        raw = acct.get("raw_data", {}) or {}
        due = acct.get("due_date") or raw.get("account", {}).get("dueDate")
        parsed = _parse_due_date(due)
        return parsed.isoformat() if parsed else None


class DominionSCLastPaymentSensor(CoordinatorEntity[DominionSCCoordinator], SensorEntity):
    """Last payment amount (monetary)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:currency-usd"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY_DOLLAR
    _attr_state_class = SensorStateClass.TOTAL
    # Display exactly two decimal places in the UI
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Last Payment"
        self._attr_unique_id = f"{entry.entry_id}_last_payment"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> float | None:
        acct = self.coordinator.account_summary
        val = acct.get("last_payment_amount")
        parsed = _parse_money(val)
        return round(parsed, 2) if parsed is not None else None


class DominionSCAccountBalanceSensor(CoordinatorEntity[DominionSCCoordinator], SensorEntity):
    """Account balance (monetary). Treat 'Bill Paid' as $0.00."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:wallet"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY_DOLLAR
    _attr_state_class = SensorStateClass.TOTAL
    # Display exactly two decimal places in the UI
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Account Balance"
        self._attr_unique_id = f"{entry.entry_id}_account_balance"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> float | None:
        acct = self.coordinator.account_summary
        val = acct.get("account_balance")
        parsed = _parse_money(val)
        return round(parsed, 2) if parsed is not None else None


class DominionSCCurrentCostSensor(CoordinatorEntity[DominionSCCoordinator], SensorEntity):
    """Current cost from bill projection (monetary)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:currency-usd"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = CURRENCY_DOLLAR
    # Display exactly two decimal places in the UI
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Current Cost"
        self._attr_unique_id = f"{entry.entry_id}_current_cost"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> float | None:
        proj = self.coordinator.bill_projection or {}
        val = proj.get("currentPrice")
        return round(float(val), 2) if val is not None else None


class DominionSCProjectedPriceSensor(DominionSCCurrentCostSensor):
    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Projected Price"
        self._attr_unique_id = f"{entry.entry_id}_projected_price"

    # ensure display precision is preserved for projected price as well
    _attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        proj = self.coordinator.bill_projection or {}
        val = proj.get("projectionPrice")
        return round(float(val), 2) if val is not None else None


class DominionSCDaysLeftSensor(CoordinatorEntity[DominionSCCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-clock"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Days Left"
        self._attr_unique_id = f"{entry.entry_id}_days_left"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> int | None:
        proj = self.coordinator.bill_projection or {}
        val = proj.get("daysLeft")
        return int(val) if val is not None else None


class DominionSCElectricChargesSensor(CoordinatorEntity[DominionSCCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:flash"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY_DOLLAR
    _attr_state_class = SensorStateClass.TOTAL
    # Display exactly two decimal places in the UI
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Electric Charges"
        self._attr_unique_id = f"{entry.entry_id}_electric_charges"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> float | None:
        summary = self.coordinator.current_bill_summary or {}
        val = summary.get("electric_total") or summary.get("electric_total_amount")
        return round(float(val), 2) if val is not None else None


class DominionSCGasChargesSensor(DominionSCElectricChargesSensor):
    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Gas Charges"
        self._attr_unique_id = f"{entry.entry_id}_gas_charges"

    # inherit suggested precision from parent, but set explicitly for clarity
    _attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        summary = self.coordinator.current_bill_summary or {}
        val = summary.get("gas_total")
        return round(float(val), 2) if val is not None else None


class DominionSCElectricOtherChargesSensor(DominionSCElectricChargesSensor):
    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Electric Other Charges"
        self._attr_unique_id = f"{entry.entry_id}_electric_other_charges"

    _attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        summary = self.coordinator.current_bill_summary or {}
        val = summary.get("electric_other_charges")
        return round(float(val), 2) if val is not None else None


class DominionSCGasOtherChargesSensor(DominionSCElectricOtherChargesSensor):
    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Gas Other Charges"
        self._attr_unique_id = f"{entry.entry_id}_gas_other_charges"

    _attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        summary = self.coordinator.current_bill_summary or {}
        val = summary.get("gas_other_charges")
        return round(float(val), 2) if val is not None else None


class DominionSCTotalChargesSensor(DominionSCElectricChargesSensor):
    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Total Charges"
        self._attr_unique_id = f"{entry.entry_id}_total_charges"

    _attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        summary = self.coordinator.current_bill_summary or {}
        val = summary.get("total_bill_amount") or summary.get("total_usage_charges")
        return round(float(val), 2) if val is not None else None
