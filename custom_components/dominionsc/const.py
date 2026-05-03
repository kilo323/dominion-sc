"""Constants for Dominion SC Energy integration."""
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
