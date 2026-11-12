"""Constants for Dominion SC Energy integration."""
import os

DOMAIN = "dominionsc"

PLATFORMS = ["sensor", "button"]

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_VERIFY_SSL = "verify_ssl"
CONF_POLL_MINUTES = "poll_minutes"
CONF_BACKFILL_CYCLES_TARGET = "backfill_cycles_target"
CONF_DAILY_LOOKBACK_DAYS = "daily_lookback_days"
CONF_TFA_TOKEN = "tfa_token"
# Test-only toggle: force the coordinator down the Fusion monthly-total path
# (as for a gas-only account) and skip Bidgely. Lives in the HACS options
# flow; defaults to False so existing behavior is unchanged.
CONF_FORCE_FUSION_GAS_ONLY = "force_fusion_gas_only"

DEFAULT_VERIFY_SSL = True
DEFAULT_POLL_MINUTES = 15
DEFAULT_BACKFILL_CYCLES_TARGET = 6
DEFAULT_DAILY_LOOKBACK_DAYS = 5
DEFAULT_FORCE_FUSION_GAS_ONLY = False

# Environment variable name used to seed the force flag into a freshly
# created entry so a fresh install can validate the gas-only Fusion path
# during first sync.
ENV_FORCE_FUSION_GAS_ONLY = "DOMINION_SC_FORCE_FUSION_GAS_ONLY"

# Recognized truthy values for ENV_FORCE_FUSION_GAS_ONLY (case-insensitive).
_FORCE_TRUTHY = {"1", "true", "yes", "on"}


def read_force_fusion_gas_only_from_env(
    env: "dict[str, str] | None" = None,
) -> bool:
    """Whether the force-fusion-gas-only flag is set via the environment.

    Reads ``DOMINION_SC_FORCE_FUSION_GAS_ONLY`` from ``env`` (defaults to
    ``os.environ``). Recognized truthy values are, case-insensitively,
    ``1``, ``true``, ``yes`` and ``on``; anything else, including a missing
    variable, is ``False``.

    This lets a fresh install seed the flag into the entry's ``data`` before
    first sync, so the coordinator's Fusion-only path is exercised during
    setup without editing HACS options. Because it reads a plain env var it
    never leaks into a published integration and has no effect on existing
    users. ``env`` is injectable for testing.
    """
    env = env if env is not None else os.environ
    return env.get(ENV_FORCE_FUSION_GAS_ONLY, "").strip().lower() in _FORCE_TRUTHY

SERVICE_RUN_BACKFILL = "run_backfill"
SERVICE_RUN_BACKFILL_ALL = "run_backfill_all"
SERVICE_REWRITE_STATISTICS = "rewrite_statistics"

STORE_VERSION = 1
STORE_KEY_PREFIX = f"{DOMAIN}_state_"

COORDINATOR = "coordinator"

TOTAL_ELECTRIC_KWH = "electric_kwh_total"
TOTAL_GAS_FT3 = "gas_ft3_total"
TOTAL_ELECTRIC_COST = "electric_cost_total"
TOTAL_GAS_COST = "gas_cost_total"
