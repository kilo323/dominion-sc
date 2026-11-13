"""Constants for Dominion SC Energy integration."""
import os
from typing import Mapping

DOMAIN = "dominionsc"

PLATFORMS = ["sensor", "button"]

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_VERIFY_SSL = "verify_ssl"
CONF_POLL_MINUTES = "poll_minutes"
CONF_BACKFILL_CYCLES_TARGET = "backfill_cycles_target"
CONF_DAILY_LOOKBACK_DAYS = "daily_lookback_days"
CONF_TFA_TOKEN = "tfa_token"

DEFAULT_VERIFY_SSL = True
DEFAULT_POLL_MINUTES = 15
DEFAULT_BACKFILL_CYCLES_TARGET = 6
DEFAULT_DAILY_LOOKBACK_DAYS = 5

# Environment variable used to drive the test-only gas-only Fusion path.
# Set it (to a truthy value) before starting Home Assistant so the very
# first sync skips Bidgely and reads Fusion monthly totals instead. It is
# the single source of truth for this flag and is read live on every sync.
ENV_FORCE_FUSION_GAS_ONLY = "DOMINIONSC_FORCE_FUSION_GAS_ONLY"

# Recognized truthy values for ENV_FORCE_FUSION_GAS_ONLY (case-insensitive).
_FORCE_TRUTHY = {"1", "true", "yes", "on"}


def read_force_fusion_gas_only_from_env(
    env: "Mapping[str, str] | None" = None,
) -> bool:
    """Whether the force-fusion-gas-only flag is set via the environment.

    Reads ``DOMINIONSC_FORCE_FUSION_GAS_ONLY`` from ``env`` (defaults to
    ``os.environ``). Returns ``True`` for recognized truthy values (case-
    insensitive ``1``, ``true``, ``yes``, ``on``); ``False`` otherwise
    (missing variable, ``""``, ``0``, ``false``, ``off``, or any other
    value).

    This is the single source of truth for the test-only Fusion gas-only
    path: the coordinator evaluates it live on every sync, so setting or
    clearing the variable and restarting the integration flips the path
    without editing HACS options. It reads a plain env var, so it never
    leaks into a published integration and has no effect on existing users.
    ``env`` is injectable for testing.
    """
    env = env if env is not None else os.environ
    raw = env.get(ENV_FORCE_FUSION_GAS_ONLY)
    return bool(raw) and raw.strip().lower() in _FORCE_TRUTHY

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
