import datetime
from custom_components.dominionsc import sensor as sc_sensor
from custom_components.dominionsc import const


class DummyEntry:
    def __init__(self, entry_id: str = "test_entry"):
        self.entry_id = entry_id
        self.options = {}
        self.data = {}


class DummyCycle:
    def __init__(self, start, end):
        self.start = start
        self.end = end


class DummyCoordinator:
    def __init__(self):
        # minimal state used by sensors
        self._state = {
            "account_summary": {},
            "bill_projection": {},
            "current_daily_usage": {},
            "current_bill_summary": {},
        }
        self.totals = {
            const.TOTAL_ELECTRIC_KWH: 0.0,
            const.TOTAL_GAS_FT3: 0.0,
            const.TOTAL_ELECTRIC_COST: 0.0,
            const.TOTAL_GAS_COST: 0.0,
        }
        self.backfill_cycles_target = 6
        self.backfill_summary = {
            "completed_cycles": [],
            "incomplete_cycles": [],
        }

        self.last_sync = None
        # allow setting current_billing_cycle in tests
        self.current_billing_cycle = None

    # Provide properties matching the real coordinator API used by sensors
    @property
    def account_summary(self):
        return self._state.get("account_summary", {}) or {}

    @property
    def bill_projection(self):
        return self._state.get("bill_projection", {}) or {}

    @property
    def current_bill_summary(self):
        return self._state.get("current_bill_summary", {}) or {}

    @property
    def current_daily_usage(self):
        return self._state.get("current_daily_usage", {}) or {}

    async def async_rewrite_statistics(self):
        return None


def test_format_cycle_label_with_pipe():
    key = "2026-03-01|2026-03-31"
    assert sc_sensor._format_cycle_label(key) == "2026-Mar"


def test_format_cycle_label_invalid():
    bad = "not-a-date"
    assert sc_sensor._format_cycle_label(bad) == bad


def test_format_billing_date():
    d = datetime.date(2026, 3, 9)
    assert sc_sensor._format_billing_date(d) == "Mar 9"


def test_parse_money_strings_and_numbers():
    assert sc_sensor._parse_money(None) is None
    assert sc_sensor._parse_money(123.45) == 123.45
    assert sc_sensor._parse_money("$1,234.56") == 1234.56
    assert sc_sensor._parse_money("Bill Paid") == 0.0
    # empty/whitespace
    assert sc_sensor._parse_money("") is None


def test_parse_due_date_variants():
    assert sc_sensor._parse_due_date(None) is None
    d1 = sc_sensor._parse_due_date("2026-03-09")
    assert isinstance(d1, datetime.date) and d1 == datetime.date(2026, 3, 9)
    d2 = sc_sensor._parse_due_date("Mar 9, 2026")
    assert isinstance(d2, datetime.date) and d2 == datetime.date(2026, 3, 9)


def test_total_sensor_native_value_rounding():
    coord = DummyCoordinator()
    coord.totals[const.TOTAL_ELECTRIC_KWH] = 12.34567
    entry = DummyEntry("abc123")
    desc = sc_sensor.SENSORS[0]
    s = sc_sensor.DominionSCTotalSensor(coord, entry, desc)
    assert s.native_value == round(12.34567, 3)


def test_total_sensor_apply_restored_native_value_raises_baseline():
    coord = DummyCoordinator()
    coord.totals[const.TOTAL_ELECTRIC_KWH] = 50.0
    coord._state["last_totals"] = {const.TOTAL_ELECTRIC_KWH: 49.0}

    entry = DummyEntry("abc123")
    desc = sc_sensor.SENSORS[0]
    s = sc_sensor.DominionSCTotalSensor(coord, entry, desc)

    s._apply_restored_native_value(75.0)

    assert coord.totals[const.TOTAL_ELECTRIC_KWH] == 75.0
    assert coord._state["last_totals"][const.TOTAL_ELECTRIC_KWH] == 75.0


def test_total_sensor_apply_restored_native_value_ignores_lower_value():
    coord = DummyCoordinator()
    coord.totals[const.TOTAL_ELECTRIC_KWH] = 80.0
    coord._state["last_totals"] = {const.TOTAL_ELECTRIC_KWH: 80.0}

    entry = DummyEntry("abc123")
    desc = sc_sensor.SENSORS[0]
    s = sc_sensor.DominionSCTotalSensor(coord, entry, desc)

    s._apply_restored_native_value(70.0)

    assert coord.totals[const.TOTAL_ELECTRIC_KWH] == 80.0
    assert coord._state["last_totals"][const.TOTAL_ELECTRIC_KWH] == 80.0


def test_total_sensor_apply_restored_native_value_schedules_one_time_rewrite():
    coord = DummyCoordinator()
    coord.totals[const.TOTAL_ELECTRIC_KWH] = 50.0
    coord._state["last_totals"] = {const.TOTAL_ELECTRIC_KWH: 49.0}

    class HassStub:
        def __init__(self):
            self.tasks = []

        def async_create_task(self, coro):
            self.tasks.append(coro)
            # close to avoid un-awaited coroutine warnings in tests
            coro.close()

    coord.hass = HassStub()

    entry = DummyEntry("abc123")
    desc = sc_sensor.SENSORS[0]
    s = sc_sensor.DominionSCTotalSensor(coord, entry, desc)

    s._apply_restored_native_value(75.0)
    s._apply_restored_native_value(76.0)

    assert len(coord.hass.tasks) == 1
    assert getattr(coord, "_startup_statistics_rewrite_scheduled", False) is True


def test_backfill_cycles_sensor_attributes_and_value():
    coord = DummyCoordinator()
    coord.backfill_cycles_target = 3
    coord.backfill_summary = {
        "completed_cycles": ["2026-01-01|2026-01-31"],
        "incomplete_cycles": ["2026-02-01|2026-02-28", "2026-03-01|2026-03-31"],
    }
    entry = DummyEntry("entry1")
    s = sc_sensor.DominionSCBackfillCyclesSensor(coord, entry)
    assert s.native_value == 3
    attrs = s.extra_state_attributes
    assert attrs["completed_count"] == 1
    assert attrs["incomplete_count"] == 2
    assert attrs["completed_cycles"][0] == "2026-Jan"


def test_backfill_remaining_sensor_with_summary():
    coord = DummyCoordinator()
    coord.backfill_summary = {"incomplete_cycles": ["a", "b", "c"]}
    entry = DummyEntry("entry2")
    s = sc_sensor.DominionSCBackfillRemainingSensor(coord, entry)
    assert s.native_value == 3


def test_current_billing_cycle_sensor_and_attrs():
    coord = DummyCoordinator()
    coord.current_billing_cycle = DummyCycle(
        start=datetime.date(2026, 3, 9), end=datetime.date(2026, 4, 4)
    )
    entry = DummyEntry("entry3")
    s = sc_sensor.DominionSCCurrentBillingCycleSensor(coord, entry)
    assert s.native_value == "Mar 9 - Apr 4"
    attrs = s.extra_state_attributes
    assert attrs["start"] == "2026-03-09"
    assert attrs["end"] == "2026-04-04"


def test_last_sync_sensor_returns_datetime():
    coord = DummyCoordinator()
    coord.last_sync = datetime.datetime.now().astimezone().isoformat()
    entry = DummyEntry("entry4")
    s = sc_sensor.DominionSCLastSyncSensor(coord, entry)
    assert isinstance(s.native_value, datetime.datetime)


def test_informational_sensors_parsing():
    coord = DummyCoordinator()
    coord._state["account_summary"] = {
        "due_date": "Mar 9, 2026",
        "last_payment_amount": "$333.19",
        "account_balance": "Bill Paid",
    }
    coord._state["bill_projection"] = {"currentPrice": 12.3456, "projectionPrice": 7.891}
    coord._state["current_bill_summary"] = {
        "electric_total": "11.11",
        "gas_total": "22.22",
        "electric_other_charges": "3.00",
        "gas_other_charges": "4.00",
        "total_bill_amount": "40.33",
    }
    entry = DummyEntry("entry5")
    due = sc_sensor.DominionSCBillDueDateSensor(coord, entry)
    assert due.native_value == "2026-03-09"
    last_payment = sc_sensor.DominionSCLastPaymentSensor(coord, entry)
    assert last_payment.native_value == 333.19
    balance = sc_sensor.DominionSCAccountBalanceSensor(coord, entry)
    assert balance.native_value == 0.0
    current = sc_sensor.DominionSCCurrentCostSensor(coord, entry)
    assert current.native_value == round(12.3456, 2)
    projected = sc_sensor.DominionSCProjectedPriceSensor(coord, entry)
    assert projected.native_value == round(7.891, 2)
    days = sc_sensor.DominionSCDaysLeftSensor(coord, entry)
    # daysLeft not set -> None
    assert days.native_value is None
    e_charges = sc_sensor.DominionSCElectricChargesSensor(coord, entry)
    assert e_charges.native_value == round(11.11, 2)
    g_charges = sc_sensor.DominionSCGasChargesSensor(coord, entry)
    assert g_charges.native_value == round(22.22, 2)
    e_other = sc_sensor.DominionSCElectricOtherChargesSensor(coord, entry)
    assert e_other.native_value == round(3.00, 2)
    g_other = sc_sensor.DominionSCGasOtherChargesSensor(coord, entry)
    assert g_other.native_value == round(4.00, 2)
    total = sc_sensor.DominionSCTotalChargesSensor(coord, entry)
    assert total.native_value == round(40.33, 2)
