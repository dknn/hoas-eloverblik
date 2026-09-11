"""Price contracts and stateful cost reconciliation using HA/API boundary doubles."""
import ast
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import math
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from test_statistics import ROOT, NOW, UTC, LOCAL, environment, load_class


def price_module():
    # Execute every production function. Only the HTTP boundary is substituted.
    ns = {"datetime": datetime, "timedelta": timedelta, "timezone": timezone,
          "math": math, "parsedate_to_datetime": parsedate_to_datetime,
          "requests": SimpleNamespace(get=Mock())}
    tree = ast.parse((ROOT / "spot_prices.py").read_text())
    exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))],
                            type_ignores=[]), "spot_prices.py", "exec"), ns)
    return ns


class PriceTests(unittest.TestCase):
    def setUp(self):
        self.ns = price_module()
        self.start = datetime(2026, 9, 10, tzinfo=UTC)

    def payload(self, values=(100, 200, -300, 400)):
        return {"records": [{"TimeUTC": (self.start + timedelta(minutes=15*i)).isoformat(),
                             "PriceArea": "DK2", "DayAheadPriceDKK": value}
                            for i, value in enumerate(values)]}

    def test_quarter_prices_convert_mwh_to_kwh_and_keep_negative_prices(self):
        prices = self.ns['parse_prices'](self.payload(), 2026, 9, "DK2")
        self.assertAlmostEqual(self.ns['hourly_price'](self.start, prices, False), 0.1)
        self.assertEqual(prices[(self.start+timedelta(minutes=30)).isoformat()], -0.3)

    def test_naive_api_timestamp_means_utc(self):
        p = self.payload((100,))
        p['records'][0]['TimeUTC'] = '2026-09-10T00:00:00'
        self.assertEqual(self.ns['parse_prices'](p, 2026, 9, 'DK2'), {self.start.isoformat(): 0.1})

    def test_missing_quarter_is_not_treated_as_zero(self):
        prices = self.ns['parse_prices'](self.payload((100,200,300)), 2026, 9, 'DK2')
        self.assertIsNone(self.ns['hourly_price'](self.start, prices, False))

    def test_zero_is_a_real_price(self):
        prices = self.ns['parse_prices'](self.payload((0,0,0,0)), 2026, 9, 'DK2')
        self.assertEqual(self.ns['hourly_price'](self.start, prices, False), 0)

    def test_invalid_prices_are_rejected(self):
        for value in (None, True, 'NaN', 'Infinity', 'broken'):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                self.ns['parse_prices'](self.payload((value,)), 2026, 9, 'DK2')

    def test_duplicate_wrong_area_bad_interval_and_truncation(self):
        for change in ('duplicate', 'area', 'interval', 'truncated'):
            p = self.payload()
            if change == 'duplicate': p['records'].append(p['records'][0])
            if change == 'area': p['records'][0]['PriceArea'] = 'DK1'
            if change == 'interval': p['records'][0]['TimeUTC'] = '2026-09-10T00:01:00'
            if change == 'truncated': p['records'] *= 1000
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.ns['parse_prices'](p, 2026, 9, 'DK2')

    def test_historical_hourly_dataset(self):
        p = {"records": [{"HourUTC": "2025-09-01T00:00:00", "PriceArea": "DK1", "SpotPriceDKK": 250}]}
        prices = self.ns['parse_prices'](p, 2025, 9, 'DK1')
        self.assertEqual(self.ns['hourly_price'](datetime(2025,9,1,tzinfo=UTC), prices, True), .25)

    def test_requests_are_bounded_and_dataset_switches(self):
        response = Mock(status_code=200)
        response.json.return_value = {'records': []}
        self.ns['requests'].get.return_value = response
        for year,month,dataset,end in [(2025,9,'Elspotprices','2025-10-01'),
                                       (2025,10,'DayAheadPrices','2025-11-01'),
                                       (2026,12,'DayAheadPrices','2027-01-01')]:
            self.ns['fetch_month'](year,month,'DK2')
            args,kwargs = self.ns['requests'].get.call_args
            self.assertTrue(args[0].endswith('/'+dataset))
            self.assertEqual(kwargs['params']['end'],end)
            self.assertEqual(kwargs['params']['filter'],'{"PriceArea":["DK2"]}')
            self.assertEqual(kwargs['params']['limit'],4000)
            self.assertEqual(kwargs['timeout'],(10,30))
            self.assertFalse(kwargs['allow_redirects'])
        with self.assertRaises(ValueError): self.ns['fetch_month'](2026,9,'invalid')

    def test_http_error_is_not_cached_as_an_empty_success(self):
        self.ns['requests'].get.return_value = Mock(status_code=429,headers={'Retry-After':'60'})
        with self.assertRaises(ValueError): self.ns['fetch_month'](2026,9,'DK2')

    def test_rate_limit_respects_retry_after(self):
        self.ns['requests'].get.return_value = Mock(status_code=429,headers={'Retry-After':'120'})
        with self.assertRaises(self.ns['PriceRateLimit']) as context:
            self.ns['fetch_month'](2026,9,'DK2')
        self.assertEqual(context.exception.retry_after,120)
        for invalid in (None,'NaN','Infinity','nonsense','-1'):
            self.assertEqual(self.ns['PriceRateLimit'](invalid).retry_after,300)

    def test_dst_repeated_hours_are_distinct(self):
        # The two local 02:00 hours during the autumn switch have distinct UTC keys.
        first = datetime(2026,10,25,0,tzinfo=UTC)
        second = first+timedelta(hours=1)
        prices = {(start+timedelta(minutes=i*15)).isoformat(): value
                  for start,value in [(first,.1),(second,.9)] for i in range(4)}
        self.assertEqual(self.ns['hourly_price'](first,prices,False),.1)
        self.assertEqual(self.ns['hourly_price'](second,prices,False),.9)


class CostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = environment()
        self.ns['dt_util'].get_time_zone = lambda name: LOCAL
        self.ns['SensorDeviceClass'].MONETARY = 'monetary'
        self.ns['hourly_price'] = price_module()['hourly_price']
        self.ns['PriceRateLimit'] = price_module()['PriceRateLimit']
        self.ns['fetch_month'] = Mock()
        self.store = SimpleNamespace(async_load=AsyncMock(return_value={}), async_save=AsyncMock())
        self.ns['Store'] = Mock(return_value=self.store)
        self.start = NOW.replace(day=10,hour=0,minute=0)
        self.prices = {(self.start+timedelta(minutes=i*15)).isoformat(): .1*(1+i//4)
                       for i in range(12)}
        self.ns['fetch_month'].return_value = self.prices
        cls = load_class('spot_cost.py','EloverblikSpotCost',self.ns)
        self.sensor = cls(SimpleNamespace(entity_id='sensor.energy'),'test','DK2','entry')
        self.sensor.entity_id = 'sensor.cost'
        self.energy = [{'start':(self.start-timedelta(hours=1)).timestamp(),'sum':100},
                       {'start':self.start.timestamp(),'sum':102},
                       {'start':(self.start+timedelta(hours=1)).timestamp(),'sum':105},
                       {'start':(self.start+timedelta(hours=2)).timestamp(),'sum':109}]
        # The initial energy baseline must precede the reconciliation window.
        self.energy[0]['start'] = datetime(2024,12,31,tzinfo=UTC).timestamp()
        self.costs = []
        async def executor(func,*args): return func(*args)
        def query(hass,count,entity,*args):
            return {entity:self.energy if entity=='sensor.energy' else self.costs}
        self.sensor.hass = SimpleNamespace(async_add_executor_job=executor)
        self.ns['get_instance'] = lambda hass: SimpleNamespace(async_add_executor_job=executor)
        self.ns['get_last_statistics'] = query
        def record(hass,meta,rows):
            merged = {row['start']:row['sum'] for row in self.costs}
            merged.update({row['start'].timestamp():row['sum'] for row in rows})
            self.costs = [{'start':start,'sum':total} for start,total in sorted(merged.items())]
        self.importer = Mock(side_effect=record)
        self.ns['async_import_statistics'] = self.importer

    async def test_costs_are_historical_and_first_hour_has_zero_anchor(self):
        await self.sensor.async_update()
        self.assertEqual(self.sensor._status,'ready')
        self.assertEqual(len(self.costs),4)
        for got,expected in zip(self.costs,[0,.2,.8,2]): self.assertAlmostEqual(got['sum'],expected)
        self.assertEqual(self.importer.call_args.args[1]['unit_of_measurement'],'DKK')
        self.assertFalse(self.sensor.extra_state_attributes['includes_vat'])

    async def test_replay_does_not_double_count_and_cache_survives_reload(self):
        await self.sensor.async_update()
        saved = self.store.async_save.call_args.args[0]
        self.sensor._cache = self.sensor._store = None
        self.store.async_load.return_value = saved
        self.sensor._next_update = None
        self.importer.reset_mock()
        await self.sensor.async_update()
        self.importer.assert_not_called()
        self.ns['fetch_month'].assert_called_once()

    async def test_revised_energy_recalculates_later_sums(self):
        await self.sensor.async_update()
        self.energy[1]['sum'] = 103  # quantities change 2,3,4 -> 3,2,4
        self.sensor._next_update = None
        await self.sensor.async_update()
        self.assertAlmostEqual(self.costs[-1]['sum'],1.9)
        self.assertAlmostEqual(self.costs[1]['sum'],.3)

    async def test_missing_prices_dont_create_zero_cost_or_partial_import(self):
        self.prices.pop(self.start.isoformat())
        await self.sensor.async_update()
        self.assertEqual(self.sensor._status,'missing_prices')
        self.importer.assert_not_called()
        self.store.async_save.assert_awaited_once()

    async def test_negative_prices_reduce_cost_without_reset(self):
        for k in self.prices: self.prices[k] = -.2
        await self.sensor.async_update()
        self.assertAlmostEqual(self.costs[-1]['sum'],-1.8)

    async def test_api_failure_preserves_history_and_retries_are_throttled(self):
        self.costs = [{'start':self.start.timestamp(),'sum':12}]
        self.ns['fetch_month'].side_effect = OSError('private upstream body')
        await self.sensor.async_update()
        await self.sensor.async_update()
        self.assertEqual(self.sensor._status,'update_failed')
        self.importer.assert_not_called()
        self.ns['fetch_month'].assert_called_once()
        self.assertNotIn('private',str(self.ns['_LOGGER'].mock_calls))

    async def test_no_consumption_does_not_call_price_api(self):
        self.energy = []
        await self.sensor.async_update()
        self.assertEqual(self.sensor._status,'waiting_for_consumption')
        self.ns['fetch_month'].assert_not_called()

    async def test_rate_limit_pauses_without_importing_incomplete_costs(self):
        self.ns['fetch_month'].side_effect = self.ns['PriceRateLimit']('120')
        await self.sensor.async_update()
        self.assertEqual(self.sensor._status,'rate_limited')
        self.assertEqual(self.sensor._next_update,NOW+timedelta(seconds=120))
        self.importer.assert_not_called()
        await self.sensor.async_update()
        self.ns['fetch_month'].assert_called_once()

    async def test_nonfinite_energy_does_not_write_statistics(self):
        self.energy[1]['sum'] = float('nan')
        await self.sensor.async_update()
        self.assertEqual(self.sensor._status,'update_failed')
        self.importer.assert_not_called()

    async def test_prior_year_cost_anchor_is_preserved(self):
        self.costs = [{'start':datetime(2024,12,31,tzinfo=UTC).timestamp(),'sum':50}]
        await self.sensor.async_update()
        self.assertAlmostEqual(self.costs[-1]['sum'],52)

    async def test_failed_month_does_not_import_partial_results(self):
        self.energy.append({'start':datetime(2026,10,1,tzinfo=UTC).timestamp(),'sum':110})
        future = datetime(2026,10,2,tzinfo=UTC)
        self.ns['dt_util'].utcnow = lambda: future
        self.ns['fetch_month'].side_effect = [self.prices,ValueError('unavailable')]
        await self.sensor.async_update()
        self.assertEqual(self.sensor._status,'update_failed')
        self.importer.assert_not_called()
        self.store.async_save.assert_awaited_once()

    async def test_stale_cache_is_refreshed(self):
        self.store.async_load.return_value = {'2026-09':{'prices':self.prices,'fetched_at':0}}
        await self.sensor.async_update()
        self.ns['fetch_month'].assert_called_once()

    async def test_newly_published_prices_refetch_after_one_hour(self):
        self.store.async_load.return_value = {'2026-09':{'prices':{},'fetched_at':NOW.timestamp()-3601}}
        await self.sensor.async_update()
        self.ns['fetch_month'].assert_called_once()
        self.assertEqual(self.sensor._status,'ready')

    async def test_missing_cached_prices_are_not_repeatedly_requested(self):
        self.store.async_load.return_value = {'2026-09':{'prices':{},'fetched_at':NOW.timestamp()-300}}
        await self.sensor.async_update()
        self.ns['fetch_month'].assert_not_called()
        self.assertEqual(self.sensor._status,'missing_prices')

    async def test_price_month_uses_copenhagen_even_with_utc_home_assistant(self):
        self.ns['dt_util'].as_local = lambda value: value
        self.start = datetime(2026,8,31,22,tzinfo=UTC)  # September in Denmark
        self.energy = [{'start':self.start.timestamp(),'sum':2}]
        self.ns['fetch_month'].return_value = {
            (self.start+timedelta(minutes=15*i)).isoformat(): .5 for i in range(4)}
        await self.sensor.async_update()
        self.ns['fetch_month'].assert_called_once_with(2026,9,'DK2')
        self.assertAlmostEqual(self.costs[-1]['sum'],1)

    async def test_cost_overflow_is_rejected_atomically(self):
        self.energy[1]['sum'] = 1e308
        for key in self.prices: self.prices[key] = 1e308
        await self.sensor.async_update()
        self.assertEqual(self.sensor._status,'update_failed')
        self.importer.assert_not_called()


class OptionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = {'config_entries':SimpleNamespace(OptionsFlow=object),
                   'vol':SimpleNamespace(Schema=lambda s:s,Required=lambda k,**kw:k,In=lambda v:v)}
        cls = load_class('config_flow.py','OptionsFlow',self.ns)
        self.flow = cls()
        self.flow.config_entry = SimpleNamespace(options={'unrelated':'preserved'})
        self.flow.async_create_entry = Mock(side_effect=lambda **kw:kw)
        self.flow.async_show_form = Mock(side_effect=lambda **kw:kw)

    async def test_default_is_opt_in_and_off(self):
        result = await self.flow.async_step_init()
        self.assertEqual(result['step_id'],'init')
        self.flow.async_create_entry.assert_not_called()

    async def test_area_and_disabling_preserve_other_options(self):
        for area in ('DK1','DK2','disabled'):
            result = await self.flow.async_step_init({'spot_price_area':area})
            self.assertEqual(result['data'],{'unrelated':'preserved','spot_price_area':area})

    async def test_invalid_area_cannot_be_saved(self):
        result = await self.flow.async_step_init({'spot_price_area':'DE'})
        self.assertEqual(result['errors'],{'base':'invalid_price_area'})
        self.flow.async_create_entry.assert_not_called()

    async def test_sensor_is_only_created_when_enabled(self):
        ns = {'HomeAssistant':object,'ConfigEntry':object,'DOMAIN':'eloverblik',
              'EloverblikEnergy':Mock(),'MeterReading':Mock(),'EloverblikTariff':Mock(),
              'EloverblikStatistic':Mock(),'EloverblikSpotCost':Mock()}
        node = next(n for n in ast.parse((ROOT/'sensor.py').read_text()).body
                    if isinstance(n,ast.AsyncFunctionDef) and n.name=='async_setup_entry')
        exec(compile(ast.Module(body=[node],type_ignores=[]),'sensor.py','exec'),ns)
        client=Mock(); client.get_metering_point.return_value='test'
        hass=SimpleNamespace(data={'eloverblik':{'entry':client}})
        add=Mock()
        for options in ({},{'spot_price_area':'disabled'}):
            await ns['async_setup_entry'](hass,SimpleNamespace(options=options,entry_id='entry'),add)
            ns['EloverblikSpotCost'].assert_not_called()
        await ns['async_setup_entry'](hass,SimpleNamespace(options={'spot_price_area':'DK2'},entry_id='entry'),add)
        ns['EloverblikSpotCost'].assert_called_once_with(ns['EloverblikStatistic'].return_value,'test','DK2','entry')


if __name__ == '__main__':
    unittest.main()
