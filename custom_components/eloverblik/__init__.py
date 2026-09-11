"""The Eloverblik integration."""
import asyncio
import logging
import sys
import json
import math
from datetime import timedelta, datetime, timezone
import requests
import voluptuous as vol
from homeassistant.util import Throttle
from homeassistant.util import dt as dt_util
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from pyeloverblik.models import TimeSeries
from pyeloverblik.eloverblik import Eloverblik

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({})}, extra=vol.ALLOW_EXTRA)

PLATFORMS = ["sensor"]

MIN_TIME_BETWEEN_UPDATES = timedelta(minutes=60)


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Eloverblik component."""
    hass.data[DOMAIN] = {}
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Eloverblik from a config entry."""
    refresh_token = entry.data['refresh_token']
    metering_point = entry.data['metering_point']
    
    hass.data[DOMAIN][entry.entry_id] = HassEloverblik(refresh_token, metering_point)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_options_updated))

    return True


async def async_options_updated(hass: HomeAssistant, entry: ConfigEntry):
    """Reload only this entry when optional spot pricing is changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, component)
                for component in PLATFORMS
            ]
        )
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok

class HassEloverblik:
    def __init__(self, refresh_token, metering_point):
        self._client = Eloverblik(refresh_token)
        self._metering_point = metering_point

        self._day_data = None
        self._year_data = None
        self._tariff_data = None
        self._meter_reading_data = None

    def get_total_day(self):
        if self._day_data != None:
            return round(self._day_data.get_total_metering_data(), 3)
        else:
            return None
    
    def get_total_year(self):
        if self._year_data != None:
            return round(self._year_data.get_total_metering_data(), 3)
        else:
            return None

    def get_usage_hour(self, hour):
        if self._day_data != None:
            try:
                return round(self._day_data.get_metering_data(hour), 3)
            except IndexError:
                _LOGGER.debug("No metering data for hour %s", hour)
                return None
        else:
            return None

    def get_hourly_data(self, from_date: datetime, to_date: datetime) -> dict[datetime, TimeSeries]:
        """Fetch one bounded batch; the sensor throttles the complete update."""

        try:
            days = (to_date.date() - from_date.date()).days
            if not 0 < days <= 365:
                raise ValueError("Invalid statistics request interval")
            lower = dt_util.as_utc(dt_util.start_of_local_day(from_date))
            upper = dt_util.as_utc(dt_util.start_of_local_day(to_date))
            raw_data = self._client.get_time_series(self._metering_point, from_date, to_date)
            if raw_data.status == 200:
                json_response = json.loads(raw_data.body)
                # Preserve point positions: a partial day must not be shifted
                # backwards from the period's end by the dependency's parser.
                parsed = {}
                for result in json_response.get("result") or []:
                    document = result.get("MyEnergyData_MarketDocument") or {}
                    for series in document.get("TimeSeries") or []:
                        for period in series.get("Period") or []:
                            if period["resolution"] != "PT1H":
                                raise ValueError("Expected hourly statistics from Eloverblik")
                            start = datetime.fromisoformat(period["timeInterval"]["start"])
                            end = datetime.fromisoformat(period["timeInterval"]["end"])
                            if start.tzinfo is None or end.tzinfo is None:
                                raise ValueError("Missing time zone in statistics")
                            start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
                            if not lower <= start < end <= upper or any(
                                value.minute or value.second or value.microsecond for value in (start, end)
                            ):
                                raise ValueError("Statistics period outside requested hourly interval")
                            hours = int((end - start).total_seconds() // 3600)
                            for point in period.get("Point") or []:
                                position = int(point["position"])
                                if not 1 <= position <= hours:
                                    raise ValueError("Invalid statistics point position")
                                quantity = point.get("out_Quantity.quantity")
                                if point.get("out_Quantity.quality") in ("A02", "A05"):
                                    quantity = None
                                if quantity is not None:
                                    quantity = float(quantity)
                                    if not math.isfinite(quantity):
                                        raise ValueError("Non-finite statistics quantity")
                                hour_end = start + timedelta(hours=position)
                                if hour_end in parsed:
                                    raise ValueError("Duplicate statistics point")
                                parsed[hour_end] = TimeSeries(
                                    200, hour_end, [quantity],
                                )
                return parsed
            else:
                _LOGGER.warning("Eloverblik statistics request failed with status %s", raw_data.status)
        except (requests.exceptions.RequestException, ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
            # Do not include response bodies or exception text containing private data.
            _LOGGER.warning("Unable to retrieve Eloverblik statistics (%s)", type(error).__name__)
        return None

    def get_data_date(self):
        if self._day_data != None:
            return self._day_data.data_date.date().strftime('%Y-%m-%d')
        else:
            return None

    def get_metering_point(self):
        return self._metering_point

    def get_tariff_sum_hour(self, hour):
        if self._tariff_data != None:
            sum = 0.0
            for tariff in self._tariff_data.charges.values():
                if isinstance(tariff, list):
                    if len(tariff) == 24:
                        sum += tariff[hour - 1]
                    else:
                        _LOGGER.warning(f"Unexpected length of tariff array ({len(tariff)}), expected 24 entries.")
                else:
                    sum += float(tariff)

            return sum

        else:
            return None
        
    def meter_reading_date(self):
        if self._meter_reading_data != None:
            return self._meter_reading_data.reading_date
        else:
            return None
    
    def meter_reading(self):
        if self._meter_reading_data != None:
            return self._meter_reading_data.reading
        else:
            return None

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    def update_energy(self):
        _LOGGER.debug("Fetching energy data from Eloverblik")

        try: 
            day_data = self._client.get_latest(self._metering_point)
            if day_data.status == 200:
                self._day_data = day_data
            else:
                _LOGGER.warn(f"Error from eloverblik when getting day data: {day_data.status} - {day_data.detailed_status}")

            year_data = self._client.get_per_month(self._metering_point)
            if year_data.status == 200:
                self._year_data = year_data
            else:
                _LOGGER.warn(f"Error from eloverblik when getting year data: {year_data.status} - {year_data.detailed_status}")
        except requests.exceptions.HTTPError as he:
            message = None
            if he.response.status_code == 401:
                message = f"Unauthorized error while accessing eloverblik.dk. Wrong or expired refresh token?"
            else:
                e = sys.exc_info()[1]
                message = f"Exception: {e}"

            _LOGGER.warn(message)
        except: 
            e = sys.exc_info()[1]
            _LOGGER.warn(f"Exception: {e}")

        _LOGGER.debug("Done fetching energy data from Eloverblik")

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    def update_tariffs(self):
        _LOGGER.debug("Fetching tariff data from Eloverblik")

        try: 
            tariff_data = self._client.get_tariffs(self._metering_point)
            if tariff_data.status == 200:
                self._tariff_data = tariff_data
            else:
                _LOGGER.warn(f"Error from eloverblik when getting tariff data: {tariff_data.status} - {tariff_data.detailed_status}")
        except requests.exceptions.HTTPError as he:
            message = None
            if he.response.status_code == 401:
                message = f"Unauthorized error while accessing eloverblik.dk. Wrong or expired refresh token?"
            else:
                e = sys.exc_info()[1]
                message = f"Exception: {e}"

            _LOGGER.warn(message)
        except: 
            e = sys.exc_info()[1]
            _LOGGER.warn(f"Exception: {e}")

        _LOGGER.debug("Done fetching tariff data from Eloverblik")

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    def update_meter_reading(self):
        _LOGGER.debug("Fetching meter reading data from Eloverblik")

        try: 
            meter_reading_data = self._client.get_meter_reading_latest(self._metering_point)
            if meter_reading_data.status == 200:
                self._meter_reading_data = meter_reading_data
            else:
                _LOGGER.info(f"Error from eloverblik when getting meter reading data: {meter_reading_data.status} - {meter_reading_data.detailed_status}. This is not a bug. Just an indication that data is not available in Eloverblik.dk.")
        except requests.exceptions.HTTPError as he:
            message = None
            if he.response.status_code == 401:
                message = f"Unauthorized error while accessing eloverblik.dk. Wrong or expired refresh token?"
            else:
                e = sys.exc_info()[1]
                message = f"Exception: {e}"

            _LOGGER.warn(message)
        except: 
            e = sys.exc_info()[1]
            _LOGGER.warn(f"Exception: {e}")

        _LOGGER.debug("Done fetching meter reading data from Eloverblik")
