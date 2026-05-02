"""Dominion SC Energy integration init."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

try:
    import voluptuous as vol

    from homeassistant.const import ATTR_CONFIG_ENTRY_ID
    from homeassistant.helpers import config_validation as cv
except ModuleNotFoundError:  # pragma: no cover - allows standalone scripts to import package modules
    vol = None
    cv = None
    ATTR_CONFIG_ENTRY_ID = "config_entry_id"

from .const import (
    CONF_BACKFILL_CYCLES_TARGET,
    CONF_POLL_MINUTES,
    COORDINATOR,
    DEFAULT_BACKFILL_CYCLES_TARGET,
    DEFAULT_POLL_MINUTES,
    DOMAIN,
    PLATFORMS,
    SERVICE_REWRITE_STATISTICS,
    SERVICE_RUN_BACKFILL,
)

_LOGGER = logging.getLogger(__name__)


SERVICE_SCHEMA = (
    vol.Schema(
        {
            vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
            vol.Optional("overwrite", default=False): cv.boolean,
            vol.Optional("allow_initialize_missing", default=False): cv.boolean,
        }
    )
    if vol is not None and cv is not None
    else None
)

REWRITE_SERVICE_SCHEMA = (
    vol.Schema(
        {
            vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        }
    )
    if vol is not None and cv is not None
    else None
)


async def async_setup(hass, config: dict[str, Any]) -> bool:
    """Set up via YAML is not supported; config entries only."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass, entry) -> bool:
    """Set up Dominion SC from a config entry."""
    from .coordinator import DominionSCCoordinator

    hass.data.setdefault(DOMAIN, {})

    coordinator = DominionSCCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    hass.data[DOMAIN][entry.entry_id] = {COORDINATOR: coordinator}
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, SERVICE_RUN_BACKFILL):

        async def _handle_service(call) -> None:
            await _async_handle_backfill_service(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_RUN_BACKFILL,
            _handle_service,
            schema=SERVICE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_REWRITE_STATISTICS):

        async def _handle_rewrite_service(call) -> None:
            await _async_handle_rewrite_statistics_service(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_REWRITE_STATISTICS,
            _handle_rewrite_service,
            schema=REWRITE_SERVICE_SCHEMA,
        )

    return True


async def async_unload_entry(hass, entry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    if not hass.data.get(DOMAIN):
        if hass.services.has_service(DOMAIN, SERVICE_RUN_BACKFILL):
            hass.services.async_remove(DOMAIN, SERVICE_RUN_BACKFILL)
        if hass.services.has_service(DOMAIN, SERVICE_REWRITE_STATISTICS):
            hass.services.async_remove(DOMAIN, SERVICE_REWRITE_STATISTICS)

    return unload_ok


async def _async_options_updated(hass, entry) -> None:
    """Handle options update — adjust poll interval, recalculate backfill if target changed."""
    from .coordinator import DominionSCCoordinator

    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator: DominionSCCoordinator | None = runtime.get(COORDINATOR)
    if coordinator is None:
        _LOGGER.warning("Options updated but coordinator not found for %s", entry.entry_id)
        return

    # --- Poll interval ---
    new_poll_minutes = int(
        entry.options.get(
            CONF_POLL_MINUTES,
            entry.data.get(CONF_POLL_MINUTES, DEFAULT_POLL_MINUTES),
        )
    )
    new_interval = timedelta(minutes=max(1, new_poll_minutes))
    if coordinator.update_interval != new_interval:
        _LOGGER.info(
            "Poll interval changed: %s -> %s", coordinator.update_interval, new_interval
        )
        coordinator.update_interval = new_interval

    # --- Backfill target ---
    new_backfill_target = int(
        entry.options.get(
            CONF_BACKFILL_CYCLES_TARGET,
            entry.data.get(CONF_BACKFILL_CYCLES_TARGET, DEFAULT_BACKFILL_CYCLES_TARGET),
        )
    )
    await coordinator.async_recalculate_backfill_target(new_backfill_target)

    # Trigger a refresh so new settings take effect immediately
    await coordinator.async_request_refresh()


async def _async_handle_backfill_service(hass, call) -> None:
    """Handle run_backfill service call for one or all config entries."""
    from .coordinator import DominionSCCoordinator

    target_entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
    overwrite = bool(call.data.get("overwrite", False))
    allow_initialize_missing = bool(call.data.get("allow_initialize_missing", False))

    entries = hass.data.get(DOMAIN, {})
    for entry_id, runtime in entries.items():
        if target_entry_id and entry_id != target_entry_id:
            continue
        coordinator: DominionSCCoordinator = runtime[COORDINATOR]
        await coordinator.async_run_backfill(overwrite=overwrite, allow_initialize_missing=allow_initialize_missing)


async def _async_handle_rewrite_statistics_service(hass, call) -> None:
    """Handle rewrite_statistics service call for one or all config entries."""
    from .coordinator import DominionSCCoordinator

    target_entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)

    entries = hass.data.get(DOMAIN, {})
    for entry_id, runtime in entries.items():
        if target_entry_id and entry_id != target_entry_id:
            continue
        coordinator: DominionSCCoordinator = runtime[COORDINATOR]
        await coordinator.async_rewrite_statistics()
