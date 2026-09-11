"""Optional historical spot-cost statistics, independent of energy ingestion."""
from datetime import datetime, timedelta
import logging
import math

import requests
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_last_statistics, async_import_statistics
from homeassistant.components.recorder.models import StatisticMetaData, StatisticMeanType
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .spot_prices import PriceRateLimit, fetch_month, hourly_price

_LOGGER = logging.getLogger(__name__)


class EloverblikSpotCost(SensorEntity):
    """Import revised hourly sums; never apply today's price to old consumption."""

    _attr_native_unit_of_measurement = "DKK"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    # Historical statistics are imported explicitly. Publishing a current state
    # would let recorder generate a second, conflicting series of hourly sums.
    _attr_native_value = None

    def __init__(self, energy_sensor, metering_point, area, entry_id):
        self._energy_sensor = energy_sensor
        self._area = area
        self._entry_id = entry_id
        self._attr_name = f"Eloverblik Spot Cost {area} (estimated, excl. VAT)"
        self._attr_unique_id = f"{metering_point}-spot-cost-{area}-ex-vat"
        self._cache = None
        self._store = None
        self._next_update = None
        self._status = "waiting_for_consumption"

    @property
    def extra_state_attributes(self):
        return {
            "status": self._status, "price_area": self._area,
            "calculation": "estimated_hourly_spot_cost",
            "includes_vat": False, "includes_tariffs_and_supplier_fees": False,
        }

    async def async_update(self):
        now = dt_util.utcnow()
        if self._next_update is not None and now < self._next_update:
            return
        # Failed or incomplete requests must not be hammered by 30-second polling.
        self._next_update = now + timedelta(minutes=5)
        try:
            await self._update_cost(now)
        except PriceRateLimit as error:
            self._status = "rate_limited"
            self._next_update = dt_util.utcnow() + timedelta(seconds=error.retry_after)
            _LOGGER.info("Energinet price rate limit; retry in %s seconds", error.retry_after)
        except (requests.exceptions.RequestException, ValueError, TypeError, KeyError,
                AttributeError, OverflowError, OSError) as error:
            self._status = "update_failed"
            _LOGGER.warning("Unable to update Eloverblik spot costs (%s)", type(error).__name__)

    async def _update_cost(self, now):
        local_now = dt_util.as_local(now)
        window_start = dt_util.as_utc(dt_util.start_of_local_day(datetime(local_now.year - 1, 1, 1)))
        count = int((now - window_start).total_seconds() // 3600) + 2
        energy_id = self._energy_sensor.entity_id
        if not energy_id:
            self._status = "waiting_for_consumption"
            return
        recorder = get_instance(self.hass)
        energy = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, count, energy_id, True, {"sum"}
        )
        quantities = {}
        previous = 0.0
        for row in sorted(energy.get(energy_id, []), key=lambda row: row["start"]):
            start = dt_util.utc_from_timestamp(row["start"])
            value = row["sum"]
            if value is None or not math.isfinite(value):
                raise ValueError("Invalid energy statistic")
            if window_start <= start and start + timedelta(hours=1) <= now:
                quantities[start] = value - previous
            previous = value
        if not quantities:
            self._status = "waiting_for_consumption"
            return

        if self._store is None:
            self._store = Store(self.hass, 1, f"eloverblik_spot_prices_{self._entry_id}_{self._area}")
            stored = await self._store.async_load()
            self._cache = stored if isinstance(stored, dict) else {}
        costs = {}
        # The price API interprets date-only parameters as Danish dates, even
        # when Home Assistant itself is configured with a different time zone.
        price_zone = dt_util.get_time_zone("Europe/Copenhagen")
        by_month = {}
        for start, quantity in quantities.items():
            danish_start = start.astimezone(price_zone)
            by_month.setdefault((danish_start.year, danish_start.month), {})[start] = quantity
        changed = False
        try:
            for year, month in sorted(by_month):
                month_quantities = by_month[year, month]
                key = f"{year:04}-{month:02}"
                cached = self._cache.get(key)
                # Recent prices are revisited daily; older months every 30 days.
                age_limit = 86400 if (year, month) == (local_now.year, local_now.month) else 30 * 86400
                fresh = (isinstance(cached, dict) and isinstance(cached.get("prices"), dict)
                        and isinstance(cached.get("fetched_at"), (int, float))
                        and 0 <= now.timestamp() - cached["fetched_at"] < age_limit)
                if fresh and now.timestamp() - cached["fetched_at"] >= 3600:
                    # Newly published intervals should not wait for the daily refresh.
                    fresh = all(hourly_price(start, cached["prices"], (year, month) < (2025, 10))
                                is not None for start in month_quantities)
                if not fresh:
                    prices = await self.hass.async_add_executor_job(fetch_month, year, month, self._area)
                    cached = {"fetched_at": now.timestamp(), "prices": prices}
                    self._cache[key] = cached
                    changed = True
                for start, quantity in month_quantities.items():
                    price = hourly_price(start, cached["prices"], (year, month) < (2025, 10))
                    if price is None:
                        self._status = "missing_prices"
                        return
                    costs[start] = quantity * price
        finally:
            if changed:
                # Limit disk usage to the same years that energy ingestion rechecks.
                needed = {f"{year:04}-{month:02}" for year, month in by_month}
                self._cache = {key: value for key, value in self._cache.items() if key in needed}
                await self._store.async_save(self._cache)

        existing = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, count, self.entity_id, True, {"sum"}
        )
        rows = existing.get(self.entity_id, [])
        total = 0.0
        old_sums = {}
        # Do not erase old cost hours if energy history is temporarily incomplete.
        previous = 0.0
        for row in sorted(rows, key=lambda row: row["start"]):
            start = dt_util.utc_from_timestamp(row["start"])
            value = row["sum"]
            if value is None or not math.isfinite(value):
                raise ValueError("Invalid cost statistic")
            if start < window_start:
                total = value
            else:
                costs.setdefault(start, value - previous)
                old_sums[start] = value
            previous = value
        statistics = []
        if not rows:
            # Anchor the first hour so the dashboard can compute its change too.
            statistics.append({"start": min(costs) - timedelta(hours=1), "sum": 0.0})
        for start, cost in sorted(costs.items()):
            total += cost
            if not math.isfinite(total):
                raise ValueError("Non-finite cost")
            if start not in old_sums or abs(total - old_sums[start]) > 1e-9:
                statistics.append({"start": start, "sum": total})
        if statistics:
            metadata = StatisticMetaData(
                name=self._attr_name, source="recorder", statistic_id=self.entity_id,
                unit_of_measurement="DKK", unit_class=None,
                mean_type=StatisticMeanType.NONE, has_sum=True,
            )
            async_import_statistics(self.hass, metadata, statistics)
        self._status = "ready"
        self._next_update = now + timedelta(hours=1)
