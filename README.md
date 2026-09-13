# HOAS Eloverblik

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)

HOAS Eloverblik is a community-maintained continuation of the original [homeassistant-eloverblik](https://github.com/JonasPed/homeassistant-eloverblik) integration. It is a Home Assistant custom component for monitoring electricity data from [eloverblik.dk](https://eloverblik.dk).

The integration keeps the Home Assistant domain `eloverblik` so existing installations have the best possible migration path. It is not affiliated with or endorsed by the original author or Eloverblik.dk.

**Important information**

This repository is a fork of the original project. The upstream project is no longer actively developed, so this repository is used for ongoing maintenance and improvements. Please report issues and submit pull requests here.

## Installation

### Manual installation

  1. Copy `eloverblik` folder into your `custom_components` folder in your HASS configuration directory.
  2. Restart Home Assistant (Settings → ⋮ (the top-right 3-dot menu) → Restart Home Assistant → Restart Home Assistant → Restart).
  3. [Configure](#configuration) Eloverblik through Settings → Devices & Services → Add Integration.
     * Or use this shortcut  
     [![Open your Home Assistant instance and start setting up a Eloverblik](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=eloverblik)

### Installation with HACS (Home Assistant Community Store)

  1. Ensure that [HACS](https://hacs.xyz/) is installed.
  2. Search for and install the `eloverblik` integration through HACS.
     * Or use this shortcut  
     [![Open your Home Assistant instance and open the HOAS Eloverblik repository inside the Home Assistant Community Store](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=dknn&repository=hoas-eloverblik&category=integration)
  3. Restart Home Assistant (Settings → ⋮ (the top-right 3-dot menu) → Restart Home Assistant → Restart Home Assistant → Restart).
  4. [Configure](#configuration) Eloverblik through Settings → Devices & Services → Add Integration.
     * Or use this shortcut  
     [![Open your Home Assistant instance and start setting up a Eloverblik](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=eloverblik)

## Configuration

### Refresh token and metering point

Get the refresh token and the metering point from [eloverblik.dk](https://eloverblik.dk/customer/).

  1. Log in to [Eloverblik](https://eloverblik.dk/customer/overview/).
  2. Metering point ID is used for `ID` in Home Assistant.
  3. Create a refresh token.
     1. Click your user.
     2. Chose **Data Sharing**.
     3. Click **Create token** and go trough the steps setting your preferences.

## State and attributes
---
A sensor for each over hour in the past 24 hours is created with the syntax:
 * `sensor.eloverblik_energy_0_1`
 * `sensor.eloverblik_energy_1_2`
 * etc.

A sensor which sum up the total energy usage is added as well:
 * `sensor.eloverblik_energy_total`

All sensors show their value in kWh.

## Debugging
It is possible to debug log the raw response from eloverblik.dk API. This is done by setting up logging like below in configuration.yaml in Home Assistant. It is also possible to set the log level through a service call in UI.  
```yaml
logger: 
  default: info
  logs: 
    pyeloverblik.eloverblik: debug
```

## Examples

### Daily average and gauge bar indicating high usage
Below example is an example how to display daily average and a guage indicating high usage. 

![daily average and a guage indicating high usage](images/example1.png "Gauge Example")


**Requirements**

* Recorder component holding minimum the number of days the average display should cover.
* Lovelace Config Template Card (https://github.com/iantrich/config-template-card)

**Average sensor**

Below statistics sensor shows the daily average calculated over the last 30 days. 
```yaml
sensor:
  - platform: statistics
    entity_id: sensor.eloverblik_energy_total
    name: Eloverblik Monthly Statistics
    sampling_size: 50
    state_characteristic: mean
    max_age:
        days: 30

```

**Lovelace**

```yaml
type: vertical-stack
cards:
  - card:
      entity: sensor.eloverblik_energy_total
      max: 20
      min: 0
      name: >-
        ${'Strømforbrug d. ' +
        states['sensor.eloverblik_energy_total'].attributes.metering_date }
      severity:
        green: 0
        red: '${states[''sensor.eloverblik_monthly_statistics''].state * 1.25}'
        yellow: '${states[''sensor.eloverblik_monthly_statistics''].state * 1.10}'
      type: gauge
    entities:
      - sensor.eloverblik_energy_total
      - sensor.eloverblik_monthly_statistics
    type: 'custom:config-template-card'
  - type: entity
    entity: sensor.eloverblik_monthly_statistics
    name: Daglig gennemsnit

```

### Forecast total kWh price with Nordpool integration

If you have the [Nordpool](https://github.com/custom-components/nordpool) installed you can calculate the current electricity price and forecast the price for today and tomorrow by the hour. These prices will including any tarrifs that apply, which will adjust according to peak times and season as they are fetched from Eloverblik. This way you will get the actual price you pay per kWh. You can plot this on a dashboard, or use it in the Energy dashboard.

To combine the the nordpool and eloverblik sensors, create below template sensor. Please note that the template assumes that your nordpool integration is configuerd to NOT include VAT.

```yaml
template:
  - sensor:
    - name: "Electricity Cost"
      unique_id: electricity_cost
      device_class: monetary
      unit_of_measurement: "kr/kWh"
      state: >
        {{ 1.25 * (float(states('sensor.eloverblik_tariff_sum')) + float(states('sensor.nordpool'))) }}
      attributes:
        today: >
          {% if state_attr('sensor.eloverblik_tariff_sum', 'hourly') and state_attr('sensor.nordpool', 'today') %}
            {% set ns = namespace (prices=[]) %}
            {% for h in range(24) %}
              {% set ns.prices = ns.prices + [(1.25 * (float(state_attr('sensor.eloverblik_tariff_sum', 'hourly')[h]) + float(state_attr('sensor.nordpool', 'today')[h]))) | round(5)] %}
            {% endfor %}
            {{ ns.prices }}
          {% endif %}
        tomorrow: >
          {% if state_attr('sensor.eloverblik_tariff_sum', 'hourly') and state_attr('sensor.nordpool', 'tomorrow') %}
            {% set ns = namespace (prices=[]) %}
            {% for h in range(24) %}
              {% set ns.prices = ns.prices + [(1.25 * (float(state_attr('sensor.eloverblik_tariff_sum', 'hourly')[h]) + float(state_attr('sensor.nordpool', 'tomorrow')[h]))) | round(5)] %}
            {% endfor %}
            {{ ns.prices }}
          {% endif %}
```

Replace `nordpool` with the name of your Nordpool sensor.


## Long term statistics and Energy dashboard

The integration **now supports long-term statistics** and energy dashboard.

The integration will pull current and last years data from Eloverblik and insert 
it into the long-term statistics in HomeAssistant.

An entity with the id `sensor.eloverblik_energy_statistic` is created, 
this entity will **always** have an `unknown` value, since if a current value is set, 
the recorder will try to write it to the statistics. 

> **_NOTE:_**  This is not the ideal setup, but it does enable owners that do not have live access to their measurements to get the data into home-assitant.

But the entity will have a valid long-term statistic.

Statistics are checked at most once per hour. Each update rechecks the current
and previous calendar year in requests of at most 365 days, including completed
hours of today when Eloverblik makes them available. This also repairs late or
corrected measurements within that period; older statistics are retained but
are not automatically rechecked.

Missing measurements do not block later hours and are not inserted as zero.
Previously imported values are retained when a response omits them. The totals
therefore represent the known consumption and may change when gaps are filled.
Reloading or removing the integration does not delete its long-term statistics.

> **_NOTE:_**  The data will be delayed between 1 and 3 days, depending on your local grid operator (DSO).

### Optional historical spot costs

In **Settings → Devices & services → Eloverblik → Configure**, choose DK1
(west of the Great Belt) or DK2 (east of the Great Belt). The default is Disabled.
This creates **Eloverblik Spot Cost DK1/DK2 (estimated, excl. VAT)** in the same
integration. No additional Home Assistant integration is needed.

The statistic is **net spot energy cost only**. It excludes VAT, grid tariffs,
electricity taxes, supplier supplements, subscriptions and fees. It is not an
electricity bill. The integration still imports consumption independently if
the price service is unavailable or this option is disabled.

Costs use the historical price for each consumption hour. Before October 2025
the source is Energinet's `Elspotprices` dataset; thereafter it is
`DayAheadPrices`. Prices in DKK/MWh are divided by 1000. With four quarter-hour
prices and only hourly consumption, the four prices are averaged. This assumes
even consumption within the hour, so the result is labelled **estimated**.
Negative spot prices are preserved. Actual quarter-hour consumption is not
supported by this version.

Once its `status` attribute says `ready`, edit the grid consumption source in
**Settings → Dashboards → Energy**, choose the option to use an entity tracking
total costs, and select the new spot-cost statistic. Do not select the current
tariff or current spot price to price delayed historical measurements.
The plugin does not modify your energy dashboard configuration automatically.
The statistic entity itself has no current numeric state: the historical sums
are imported directly into recorder, just like the existing energy statistic.

The current and previous calendar year are recalculated, including corrections
to consumption. Existing older cost history is retained. A zero baseline is
inserted immediately before the first priced hour so its cost is included too.
Missing or invalid prices prevent that update from writing any new cost sums;
existing sums remain untouched and can be stale until recovery. The `status`
attribute is `missing_prices`, `rate_limited`, `update_failed`, or `waiting_for_consumption`
until a complete calculation succeeds. This cannot turn a day without
consumption data into a known bill or force the standard dashboard to display
an explicit missing-data warning.

Prices are fetched from `https://api.energidataservice.dk` over verified HTTPS,
without sending Eloverblik tokens or meter identifiers. Requests are limited to
one calendar month (up to 4000 records) and have timeouts. A first backfill may
need about 24 requests. Per-entry/per-area caches in Home Assistant `.storage`
survive reloads; recent prices are refreshed daily and older months every
30 days. Missing required intervals may be retried after one hour. Successful
cost calculations run at most hourly; failures are retried after five minutes.
HTTP 429 pauses according to `Retry-After` (at least 30 seconds), keeping the
months already fetched so the next attempt continues instead of starting over.
Changing DK1/DK2 creates a separate cost statistic, preventing the two areas'
histories from being mixed. Disabling the option retains historical statistics;
remove or change its selection in the energy dashboard if you no longer use it.

Sources: [Eloverblik API](https://api.eloverblik.dk/CustomerApi/index.html),
[Energi Data Service API guide](https://www.energidataservice.dk/guides/api-guides).

### Local regression tests

Run `python -m unittest discover -s tests`. The tests exercise the production
classes with simulated Home Assistant and API boundaries, including a stateful
recorder double. They do not require credentials or network access. They do not
replace testing with Home Assistant's real recorder, lifecycle and polling.

Below are two examples of UI yaml configuration to display the values.

### Yesterdays consumption example:

```yaml
type: statistic
name: Elforbrug i går
entity: sensor.eloverblik_energy_statistic
period:
  calendar:
    period: day
    offset: -1
stat_type: change
icon: mdi:lightning-bolt
```
![Example in apexcharts](images/usage-example.png)

#### Last weeks consumption 
This is created with the help of [apexcharts](https://github.com/RomRider/apexcharts-card)
```yaml
type: custom:apexcharts-card
graph_span: 7d
header:
  show: true
  title: Sidste 7 dages elforbrug
span:
  end: day
  offset: '-1d'
series:
  - entity: sensor.eloverblik_energy_statistic
    type: column
    statistics:
      type: sum
      period: hour
    group_by:
      func: diff
      start_with_last: true
      duration: 1d
```
![Example in apexcharts](images/apex-example.png)
