"""Historical spot prices from Energinet, in DKK/kWh excluding VAT."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import math

import requests


class PriceRateLimit(ValueError):
    """The service asks us to resume the cached backfill later."""

    def __init__(self, retry_after):
        seconds = 300
        try:
            seconds = float(retry_after)
        except (ValueError, TypeError):
            try:
                seconds = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, AttributeError, OverflowError):
                pass
        if not math.isfinite(seconds) or seconds < 0:
            seconds = 300
        # Keep a minimum pause and reject impractically large, malformed values.
        self.retry_after = max(30, seconds) if seconds <= 31536000 else 31536000
        super().__init__("Spot price API rate limit")


def parse_prices(payload, year, month, area):
    """Validate one calendar month; keep missing intervals missing."""
    hourly = (year, month) < (2025, 10)
    time_key = "HourUTC" if hourly else "TimeUTC"
    price_key = "SpotPriceDKK" if hourly else "DayAheadPriceDKK"
    records = payload["records"]
    if not isinstance(records, list) or len(records) >= 4000:
        raise ValueError("Invalid or truncated price response")
    prices = {}
    for row in records:
        if row["PriceArea"] != area:
            raise ValueError("Unexpected price area")
        # Energi Data Service explicitly names this field UTC but omits its offset.
        start = datetime.fromisoformat(row[time_key])
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        start = start.astimezone(timezone.utc)
        if start.second or start.microsecond or start.minute % (60 if hourly else 15):
            raise ValueError("Invalid price interval")
        raw_price = row[price_key]
        if raw_price is None or isinstance(raw_price, bool):
            raise ValueError("Missing price")
        price = float(raw_price) / 1000
        if not math.isfinite(price) or start.isoformat() in prices:
            raise ValueError("Invalid or duplicate price")
        prices[start.isoformat()] = price
    return prices


def fetch_month(year, month, area):
    """Fetch a Danish calendar month in one bounded request, without credentials."""
    if area not in ("DK1", "DK2"):
        raise ValueError("Invalid price area")
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    dataset = "Elspotprices" if (year, month) < (2025, 10) else "DayAheadPrices"
    time_key = "HourUTC" if dataset == "Elspotprices" else "TimeUTC"
    response = requests.get(
        f"https://api.energidataservice.dk/dataset/{dataset}",
        params={
            "start": f"{year:04}-{month:02}-01",
            "end": f"{next_year:04}-{next_month:02}-01",
            "filter": '{"PriceArea":["' + area + '"]}',
            "sort": f"{time_key} asc", "limit": 4000,
        },
        timeout=(10, 30), allow_redirects=False,
    )
    if response.status_code == 429:
        raise PriceRateLimit(response.headers.get("Retry-After"))
    if response.status_code != 200:
        raise ValueError(f"Price API status {response.status_code}")
    return parse_prices(response.json(), year, month, area)


def hourly_price(start, prices, hourly):
    """An hourly mean is an estimate when the usage within the hour is unknown."""
    intervals = 1 if hourly else 4
    values = [prices.get((start + timedelta(minutes=15 * i)).isoformat())
              for i in range(intervals)]
    if any(value is None or isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) for value in values):
        return None
    mean = math.fsum(value / intervals for value in values)
    return mean if math.isfinite(mean) else None
