"""Platform for Eloverblik sensor integration."""
from datetime import datetime, timedelta
import logging
import math
from homeassistant.util import dt as dt_util
from homeassistant.const import UnitOfEnergy
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    DOMAIN as RECORDER_DOMAIN,
    async_import_statistics,
    get_last_statistics,
)
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.util import Throttle
from homeassistant.util.unit_conversion import EnergyConverter
from homeassistant.helpers.entity import Entity
from pyeloverblik.models import TimeSeries
from . import HassEloverblik, MIN_TIME_BETWEEN_UPDATES
from .const import DOMAIN, CURRENCY_KRONER_PER_KILO_WATT_HOUR
from .spot_cost import EloverblikSpotCost

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, config: ConfigEntry, async_add_entities):
    """Set up the sensor platform."""
    eloverblik = hass.data[DOMAIN][config.entry_id]

    sensors = []
    sensors.append(EloverblikEnergy("Eloverblik Energy Total", 'total', eloverblik))
    sensors.append(EloverblikEnergy("Eloverblik Energy Total (Year)", 'year_total', eloverblik))
    sensors.append(MeterReading("Eloverblik Meter Reading", eloverblik))
    for hour in range(1, 25):
        sensors.append(EloverblikEnergy(f"Eloverblik Energy {hour-1}-{hour}", 'hour', eloverblik, hour))
    sensors.append(EloverblikTariff("Eloverblik Tariff Sum", eloverblik))
    energy_statistic = EloverblikStatistic(eloverblik)
    sensors.append(energy_statistic)
    area = config.options.get("spot_price_area", "disabled")
    if area in ("DK1", "DK2"):
        sensors.append(EloverblikSpotCost(
            energy_statistic, eloverblik.get_metering_point(), area, config.entry_id
        ))

    async_add_entities(sensors)

class EloverblikEnergy(Entity):
    """Representation of an energy sensor."""

    def __init__(self, name, sensor_type, client, hour=None):
        """Initialize the sensor."""
        self._state = None
        self._data_date = None
        self._data = client
        self._hour = hour
        self._name = name
        self._sensor_type = sensor_type

        if sensor_type == 'hour':
            self._unique_id = f"{self._data.get_metering_point()}-{hour}"
        elif sensor_type == 'total':
            self._unique_id = f"{self._data.get_metering_point()}-total"
        elif sensor_type == 'year_total':
            self._unique_id = f"{self._data.get_metering_point()}-year-total"
        else:
            raise ValueError(f"Unexpected sensor_type: {sensor_type}.")

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def unique_id(self):
        """The unique id of the sensor."""
        return self._unique_id

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def extra_state_attributes(self):
        """Return state attributes."""
        attributes = dict()
        attributes['Metering date'] = self._data_date
        attributes['metering_date'] = self._data_date

        return attributes

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return UnitOfEnergy.KILO_WATT_HOUR

    def update(self):
        """Fetch new state data for the sensor.
        This is the only method that should fetch new data for Home Assistant.
        """
        self._data.update_energy()

        self._data_date = self._data.get_data_date()

        if self._sensor_type == 'hour':
            self._state = self._data.get_usage_hour(self._hour)
        elif self._sensor_type == 'total':
            self._state = self._data.get_total_day()
        elif self._sensor_type == 'year_total':
            self._state = self._data.get_total_year()
        else:
            raise ValueError(f"Unexpected sensor_type: {self._sensor_type}.")

class MeterReading(Entity):
    """Representation of a meter reading sensor."""

    def __init__(self, name, client):
        """Initialize the sensor."""
        self._state = None
        self._data_date = None
        self._data = client
        self._name = name

        self._unique_id = f"{self._data.get_metering_point()}-meter-reading"

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def unique_id(self):
        """The unique id of the sensor."""
        return self._unique_id

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def extra_state_attributes(self):
        """Return state attributes."""
        attributes = dict()
        attributes['meter_reading_date'] = self._data_date
        
        return attributes

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return UnitOfEnergy.KILO_WATT_HOUR

    def update(self):
        """Fetch new state data for the sensor.
        This is the only method that should fetch new data for Home Assistant.
        """
        self._data.update_meter_reading()       

        self._data_date = self._data.meter_reading_date()
        self._state = self._data.meter_reading()

class EloverblikTariff(Entity):
    """Representation of an energy sensor."""

    def __init__(self, name, client):
        """Initialize the sensor."""
        self._state = None
        self._data = client
        self._data_hourly_tariff_sums = [0] * 24
        self._name = name
        self._unique_id = f"{self._data.get_metering_point()}-tariff-sum"

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def unique_id(self):
        """The unique id of the sensor."""
        return self._unique_id

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def extra_state_attributes(self):
        """Return state attributes."""
        attributes = {
            "hourly": [self._data_hourly_tariff_sums[i] for i in range(24)]
        }

        return attributes

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return CURRENCY_KRONER_PER_KILO_WATT_HOUR

    def update(self):
        """Fetch new state data for the sensor.
        This is the only method that should fetch new data for Home Assistant.
        """
        self._data.update_tariffs()

        self._data_hourly_tariff_sums = [self._data.get_tariff_sum_hour(h) for h in range(1, 25)]
        self._state = self._data_hourly_tariff_sums[dt_util.now().hour]


class EloverblikStatistic(SensorEntity):
    """This class handles the total energy of the meter,
    and imports it as long term statistics from Eloverblik."""

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, hass_eloverblik: HassEloverblik):
        self._attr_name = "Eloverblik Energy Statistic"
        self._attr_unique_id = f"{hass_eloverblik.get_metering_point()}-statistic"
        self._hass_eloverblik = hass_eloverblik

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    async def async_update(self):
        """Continually update history"""
        await self._update_data()

    async def _update_data(self):
        today = dt_util.now().date()
        from_date = datetime(today.year-1, 1, 1)
        to_date = datetime.combine(today + timedelta(days=1), datetime.min.time())
        window_start = dt_util.as_utc(dt_util.start_of_local_day(from_date))
        # At most one recorder row per hour, plus one older row to anchor the
        # cumulative sum. This preserves history before the reconciliation window.
        count = int((dt_util.utcnow() - window_start).total_seconds() // 3600) + 2
        existing = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, count, self.entity_id, True, {"sum"}
        )
        data = {}
        batch_start = from_date
        while batch_start < to_date:
            batch_end = min(batch_start + timedelta(days=365), to_date)
            batch = await self.hass.async_add_executor_job(
                self._hass_eloverblik.get_hourly_data, batch_start, batch_end
            )
            if batch is None:
                # Keep the complete previous history on a failed request.
                return
            data.update(batch)
            batch_start = batch_end
        await self._insert_statistics(data, existing.get(self.entity_id, []), window_start)

    async def _insert_statistics(
        self,
        data: dict[datetime, TimeSeries],
        existing: list[StatisticData],
        window_start: datetime):

        statistics : list[StatisticData] = []
        now = dt_util.utcnow()
        total = previous_sum = 0.0
        quantities = {}
        old_sums = {}
        for row in sorted(existing, key=lambda row: row["start"]):
            start = dt_util.utc_from_timestamp(row["start"])
            value = row["sum"]
            if value is None or not math.isfinite(value):
                _LOGGER.warning("Cannot reconcile invalid existing statistics")
                return
            if start < window_start:
                total = value
            else:
                quantities[start] = value - previous_sum
                old_sums[start] = value
            previous_sum = value

        for series in data.values():
            values = series._metering_data
            if values is None:
                continue
            first = series.data_date - timedelta(hours=len(values))
            for hour, quantity in enumerate(values):
                start = first + timedelta(hours=hour)
                if start < window_start or start + timedelta(hours=1) > now:
                    continue
                if quantity is not None:
                    if not math.isfinite(quantity):
                        _LOGGER.warning("Cannot reconcile non-finite statistics")
                        return
                    quantities[start] = quantity

        # Missing API points retain known consumption. Unknown gaps get no row;
        # later hours can progress, and the full window is retried on every poll.
        for start, quantity in sorted(quantities.items()):
            total += quantity
            if not math.isfinite(total):
                _LOGGER.warning("Cannot reconcile non-finite statistics sum")
                return
            if start not in old_sums or abs(total - old_sums[start]) > 1e-9:
                statistics.append(StatisticData(start=start, sum=total))

        metadata = StatisticMetaData(
            name=self._attr_name,
            source=RECORDER_DOMAIN,
            statistic_id=self.entity_id,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            unit_class=EnergyConverter.UNIT_CLASS,
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
        )

        if len(statistics) > 0:
            async_import_statistics(self.hass, metadata, statistics)
