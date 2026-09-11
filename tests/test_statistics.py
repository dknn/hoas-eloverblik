"""Focused regression tests, without installing the Home Assistant runtime.

Compile the production classes with boundary doubles for HA and the API client.
These tests exercise date selection, parsing and import; not HA lifecycle wiring.
"""
import ast
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "eloverblik"
UTC = timezone.utc
# Use a fixed offset to keep these tests independent of the OS timezone database.
LOCAL = timezone(timedelta(hours=2))
NOW = datetime(2026, 9, 11, 10, 30, tzinfo=UTC)


class TimeSeries:
    def __init__(self, status, data_date, metering_data):
        self.status = status
        self.data_date = data_date
        self._metering_data = metering_data

    def get_metering_data(self, index):
        return self._metering_data[index - 1]


def load_class(filename, name, namespace):
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), filename, "exec"), namespace)
    return namespace[name]


def environment():
    return {
        "datetime": datetime, "timedelta": timedelta, "timezone": timezone,
        "TimeSeries": TimeSeries, "json": json, "math": math, "_LOGGER": Mock(),
        "requests": SimpleNamespace(exceptions=SimpleNamespace(RequestException=OSError)),
        "Throttle": lambda interval: lambda method: method,
        "MIN_TIME_BETWEEN_UPDATES": timedelta(hours=1),
        "SensorEntity": object, "HassEloverblik": object, "HomeAssistant": object,
        "StatisticData": dict, "StatisticMetaData": dict,
        "StatisticMeanType": SimpleNamespace(NONE=0),
        "SensorDeviceClass": SimpleNamespace(ENERGY="energy"),
        "SensorStateClass": SimpleNamespace(TOTAL="total"),
        "UnitOfEnergy": SimpleNamespace(KILO_WATT_HOUR="kWh"),
        "EnergyConverter": SimpleNamespace(UNIT_CLASS="energy"),
        "RECORDER_DOMAIN": "recorder", "async_import_statistics": Mock(),
        "dt_util": SimpleNamespace(
            now=lambda: NOW.astimezone(LOCAL), utcnow=lambda: NOW,
            as_local=lambda value: value.astimezone(LOCAL),
            utc_from_timestamp=lambda value: datetime.fromtimestamp(value, UTC),
            as_utc=lambda value: value.astimezone(UTC),
            start_of_local_day=lambda value: datetime.combine(value.date(), datetime.min.time(), LOCAL),
        ),
    }


class StatisticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = environment()
        cls = load_class("sensor.py", "EloverblikStatistic", self.ns)
        self.sensor = cls(Mock())
        self.sensor.entity_id = "sensor.test"
        self.sensor.hass = SimpleNamespace(async_add_executor_job=AsyncMock(return_value={}))
        self.recorder = SimpleNamespace(async_add_executor_job=AsyncMock(return_value={}))
        self.ns['get_instance'] = lambda hass: self.recorder
        self.ns['get_last_statistics'] = Mock()
        self.importer = self.ns["async_import_statistics"]

    async def test_recent_statistic_does_not_block_fetch(self):
        self.sensor._update_data = AsyncMock()
        await self.sensor.async_update()
        self.sensor._update_data.assert_awaited_once_with()

    async def test_fetch_rechecks_history_and_includes_today(self):
        await self.sensor._update_data()
        calls = self.sensor.hass.async_add_executor_job.call_args_list
        self.assertEqual(calls[0].args[1].strftime("%Y-%m-%d"), "2025-01-01")
        _, start, end = calls[-1].args
        self.assertEqual(end.strftime("%Y-%m-%d"), "2026-09-12")

    async def test_initial_history_is_preserved(self):
        await self.sensor._update_data()
        calls = self.sensor.hass.async_add_executor_job.call_args_list
        start, end = calls[0].args[1], calls[-1].args[2]
        self.assertEqual(start, datetime(2025, 1, 1))
        self.assertEqual(end, datetime(2026, 9, 12))

    async def test_overlap_not_counted_twice(self):
        start = datetime(2026, 9, 11, 7, tzinfo=UTC)
        series = TimeSeries(200, start + timedelta(hours=3), [2, 3, 4])
        last = {"start": (start-timedelta(hours=1)).timestamp(), "sum": 18}
        await self.sensor._insert_statistics({series.data_date: series}, [last], start)
        rows = self.importer.call_args.args[2]
        self.assertEqual([r["sum"] for r in rows], [20, 23, 27])
        self.importer.reset_mock()
        await self.sensor._insert_statistics(
            {series.data_date: series}, [last] + [{"start": r['start'].timestamp(), "sum": r['sum']} for r in rows], start
        )
        self.importer.assert_not_called()

    async def test_current_and_future_hours_not_imported(self):
        start = NOW.replace(hour=9, minute=0)
        series = TimeSeries(200, start + timedelta(hours=3), [1, 2, 3])
        await self.sensor._insert_statistics({series.data_date: series}, [], start)
        self.assertEqual(self.importer.call_args.args[2], [{"start": start, "sum": 1}])

    async def test_missing_hour_does_not_block_later_measurements(self):
        start = NOW.replace(hour=6, minute=0)
        data = {start + timedelta(hours=i+1): TimeSeries(
            200, start + timedelta(hours=i+1), [value]
        ) for i, value in enumerate([1, None, 3])}
        await self.sensor._insert_statistics(data, [], start)
        self.assertEqual(self.importer.call_args.args[2], [{"start": start, "sum": 1}, {"start": start+timedelta(hours=2), "sum": 4}])

    async def test_no_data_does_not_import(self):
        await self.sensor._insert_statistics({}, [], NOW)
        self.importer.assert_not_called()

    def test_parser_preserves_partial_day_positions_and_missing_quality(self):
        ns = environment()
        ns["Eloverblik"] = Mock()
        client = load_class("__init__.py", "HassEloverblik", ns)("unused", "test")
        period = {
            "resolution": "PT1H",
            "timeInterval": {"start": "2026-09-10T22:00:00Z", "end": "2026-09-11T22:00:00Z"},
            "Point": [
                {"position": "2", "out_Quantity.quantity": "0.4"},
                {"position": "1", "out_Quantity.quantity": "0.3"},
                {"position": "3", "out_Quantity.quantity": "0", "out_Quantity.quality": "A02"},
            ],
        }
        client._client.get_time_series.return_value = SimpleNamespace(status=200, body=json.dumps(
            {"result": [{"MyEnergyData_MarketDocument": {"TimeSeries": [{"Period": [period]}]}}]}
        ))
        data = client.get_hourly_data(datetime(2026,9,11), datetime(2026,9,12))
        self.assertEqual(len(data), 3)
        first = datetime(2026, 9, 10, 23, tzinfo=UTC)
        self.assertEqual(data[first].get_metering_data(1), 0.3)
        self.assertEqual(data[first + timedelta(hours=1)].get_metering_data(1), 0.4)
        self.assertIsNone(data[first + timedelta(hours=2)].get_metering_data(1))
        self.assertNotIn(first + timedelta(hours=3), data)


if __name__ == "__main__":
    unittest.main()
