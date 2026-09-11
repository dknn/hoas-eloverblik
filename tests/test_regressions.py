"""Review regressions using production code and stateful boundary doubles."""
import ast
import copy
import json
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, AsyncMock

from test_statistics import ROOT, NOW, UTC, environment, load_class, TimeSeries


def load_function(name, ns):
    tree = ast.parse((ROOT / 'config_flow.py').read_text())
    node = next(n for n in tree.body if getattr(n, 'name', None) == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'config_flow.py', 'exec'), ns)
    return ns[name]


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = environment()
        self.cls = load_class('sensor.py', 'EloverblikStatistic', self.ns)
        self.client = Mock()
        self.sensor = self.cls(self.client)
        self.sensor.entity_id = 'sensor.test'
        self.rows = {}
        self.writes = []
        self.start = NOW.replace(hour=6, minute=0)
        self.window = datetime(2024,12,31,22,tzinfo=UTC)

        def write(hass, metadata, rows):
            self.writes.append(rows)
            self.rows.update({r['start']: r['sum'] for r in rows})

        async def read(*args):
            count = args[2]
            return {'sensor.test': self.existing()[-count:]}

        async def execute(method, *args):
            return method(*args)

        self.ns['async_import_statistics'] = write
        self.ns['get_last_statistics'] = Mock()
        self.ns['get_instance'] = lambda hass: SimpleNamespace(async_add_executor_job=read)
        self.sensor.hass = SimpleNamespace(async_add_executor_job=execute)

    def existing(self):
        return [{'start': k.timestamp(), 'sum': v} for k,v in sorted(self.rows.items())]

    def data(self, values):
        return {self.start+timedelta(hours=i+1): TimeSeries(200,self.start+timedelta(hours=i+1),[v])
                for i,v in enumerate(values)}

    async def insert(self, data):
        await self.sensor._insert_statistics(data, self.existing(), self.window)

    async def test_corrections_up_and_down_recalculate_all_later_sums(self):
        await self.insert(self.data([2,3,4]))
        await self.insert(self.data([5,3,4]))
        self.assertEqual(list(self.rows.values()), [5,8,12])
        await self.insert(self.data([1,3,4]))
        self.assertEqual(list(self.rows.values()), [1,4,8])

    async def test_permanent_gap_allows_progress_then_can_be_filled(self):
        await self.insert(self.data([1,None,3]))
        await self.insert(self.data([1,None,3,2]))
        self.assertNotIn(self.start+timedelta(hours=1), self.rows)
        self.assertEqual(self.rows[self.start+timedelta(hours=3)], 6)
        await self.insert(self.data([1,2,3,2]))
        self.assertEqual(self.rows[self.start+timedelta(hours=3)], 8)

    async def test_whole_omitted_period_can_be_filled(self):
        data = self.data([1,2,3])
        del data[self.start+timedelta(hours=2)]
        await self.insert(data)
        await self.insert(self.data([1,2,3]))
        self.assertEqual(self.rows[self.start+timedelta(hours=2)], 6)

    async def test_partial_response_retains_old_values_and_propagates_correction(self):
        await self.insert(self.data([1,2,3]))
        await self.insert(self.data([4,None]))
        self.assertEqual(list(self.rows.values()), [4,6,9])
        self.writes.clear()
        await self.insert({})
        self.assertEqual(self.writes, [])

    async def test_restart_preserves_baseline_and_backfills_gap(self):
        anchor = self.window-timedelta(hours=24)
        self.rows[anchor] = 100
        self.client.get_hourly_data.return_value = self.data([1,None,3])
        await self.sensor._update_data()
        hass = self.sensor.hass
        self.sensor = self.cls(self.client)
        self.sensor.hass = hass
        self.sensor.entity_id = 'sensor.test'
        self.client.get_hourly_data.return_value = self.data([1,2,3])
        await self.sensor._update_data()
        self.assertEqual(self.rows[anchor], 100)
        self.assertEqual(self.rows[self.start+timedelta(hours=2)], 106)
        self.writes.clear()
        await self.sensor._update_data()
        self.assertEqual(self.writes, [])

    async def test_failed_batch_does_not_write_partial_results(self):
        self.rows[self.start] = 5
        self.client.get_hourly_data.side_effect = [self.data([8]), None]
        await self.sensor._update_data()
        self.assertEqual(self.rows, {self.start: 5})
        self.assertEqual(self.writes, [])

    async def test_batches_are_contiguous_and_bounded_including_leap_year(self):
        self.ns['dt_util'].now = lambda: datetime(2025,12,31,tzinfo=UTC)
        self.client.get_hourly_data.return_value = {}
        await self.sensor._update_data()
        calls = self.client.get_hourly_data.call_args_list
        self.assertEqual(calls[0].args[0], datetime(2024,1,1))
        self.assertEqual(calls[-1].args[1], datetime(2026,1,1))
        for left,right in zip(calls,calls[1:]):
            self.assertEqual(left.args[1],right.args[0])
        for call in calls:
            self.assertTrue(0 < (call.args[1]-call.args[0]).days <= 365)

    async def test_invalid_values_and_overflow_never_reach_recorder(self):
        for values in ([1,float('nan')], [1,float('inf')], [1,float('-inf')], [1e308,1e308]):
            with self.subTest(values=values):
                await self.insert(self.data(values))
                self.assertEqual(self.writes, [])
        self.rows[self.start] = float('nan')
        await self.insert(self.data([2]))
        self.assertEqual(self.writes, [])

    async def test_repeated_partial_and_corrected_responses_match_reference_totals(self):
        self.start = NOW.replace(hour=0,minute=0)-timedelta(days=1)
        self.rows[self.window-timedelta(hours=1)] = 100
        known = {}
        rng = random.Random(17)
        for _ in range(20):
            response = {}
            for hour in range(24):
                if rng.randrange(3) == 0:
                    continue
                value = None if rng.randrange(4) == 0 else rng.randrange(0,40)/10
                start = self.start+timedelta(hours=hour)
                response[start+timedelta(hours=1)] = TimeSeries(200,start+timedelta(hours=1),[value])
                if value is not None:
                    known[start] = value
            await self.insert(response)
            total = 100
            for start,value in sorted(known.items()):
                total += value
                self.assertAlmostEqual(self.rows[start],total)
            self.assertEqual(len(self.rows),len(known)+1)

    async def test_entity_removal_leaves_history_to_host(self):
        class HostEntity:
            async def async_will_remove_from_hass(self):
                pass
        self.ns['SensorEntity'] = HostEntity
        cls = load_class('sensor.py','EloverblikStatistic',self.ns)
        sensor = cls(self.client)
        sensor.hass = Mock()
        await sensor.async_will_remove_from_hass()
        sensor.hass.assert_not_called()
        self.assertNotIn('async_will_remove_from_hass', cls.__dict__)


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.ns = environment(); self.ns['Eloverblik'] = Mock()
        self.client = load_class('__init__.py','HassEloverblik',self.ns)('unused','test')
        self.lower,self.upper = datetime(2026,9,11),datetime(2026,9,12)
        self.period = {'resolution':'PT1H','timeInterval':{
            'start':'2026-09-10T22:00:00Z','end':'2026-09-11T22:00:00Z'},
            'Point':[{'position':'1','out_Quantity.quantity':'0.5'}]}

    def parse(self, period):
        self.client._client.get_time_series.return_value = SimpleNamespace(status=200,body=json.dumps(
            {'result':[{'MyEnergyData_MarketDocument':{'TimeSeries':[{'Period':[period]}]}}]}))
        return self.client.get_hourly_data(self.lower,self.upper)

    def test_invalid_quantities_rejected(self):
        for value in ('NaN','Infinity','-Infinity','1e999','invalid'):
            with self.subTest(value=value):
                self.period['Point'][0]['out_Quantity.quantity'] = value
                self.assertIsNone(self.parse(self.period))

    def test_missing_quality_and_zero_differ(self):
        self.period['Point'][0]['out_Quantity.quantity'] = '0'
        self.assertEqual(next(iter(self.parse(self.period).values())).get_metering_data(1),0)
        for quality in ('A02','A05'):
            self.period['Point'][0]['out_Quantity.quality'] = quality
            self.assertIsNone(next(iter(self.parse(self.period).values())).get_metering_data(1))

    def test_invalid_period_dates_are_rejected_before_expansion(self):
        for start,end in [('1900-01-01T00:00:00Z','2200-01-01T00:00:00Z'),
                          ('2026-09-10T22:00:00','2026-09-11T22:00:00'),
                          ('2026-09-11T22:00:00Z','2026-09-10T22:00:00Z'),
                          ('2026-09-10T22:30:00Z','2026-09-11T22:00:00Z')]:
            with self.subTest(start=start):
                self.period['timeInterval'] = {'start':start,'end':end}
                self.assertIsNone(self.parse(self.period))

    def test_duplicate_and_out_of_range_positions_rejected(self):
        for pos in ('0','25','-1','x'):
            self.period['Point'][0]['position'] = pos
            self.assertIsNone(self.parse(self.period))
        self.period['Point'][0]['position'] = '1'
        self.period['Point'].append(copy.deepcopy(self.period['Point'][0]))
        self.assertIsNone(self.parse(self.period))

    def test_non_hourly_resolution_rejected(self):
        self.period['resolution'] = 'PT15M'
        self.assertIsNone(self.parse(self.period))

    def test_dst_days_have_23_and_25_unique_hours(self):
        for start,end,hours,lower,upper in [
            ('2026-03-29T00:00:00+01:00','2026-03-30T00:00:00+02:00',23,datetime(2026,3,29),datetime(2026,3,31)),
            ('2026-10-25T00:00:00+02:00','2026-10-26T00:00:00+01:00',25,datetime(2026,10,25),datetime(2026,10,27))]:
            with self.subTest(hours=hours):
                self.lower,self.upper = lower,upper
                self.period['timeInterval'] = {'start':start,'end':end}
                self.period['Point'] = [{'position':str(i),'out_Quantity.quantity':'1'} for i in range(1,hours+1)]
                result = self.parse(self.period)
                self.assertEqual(len(result),hours)
                self.assertEqual(max(result),datetime.fromisoformat(end).astimezone(UTC))

    def test_invalid_request_range_never_calls_network(self):
        self.assertIsNone(self.client.get_hourly_data(self.lower,self.lower))
        self.assertIsNone(self.client.get_hourly_data(self.lower,self.lower+timedelta(days=366)))
        self.client._client.get_time_series.assert_not_called()

    def test_empty_responses_are_safe(self):
        for body in ('{}','{"result":null}','{"result":[{"MyEnergyData_MarketDocument":null}]}'):
            self.client._client.get_time_series.return_value = SimpleNamespace(status=200,body=body)
            self.assertEqual(self.client.get_hourly_data(self.lower,self.upper),{})

    def test_errors_do_not_log_payloads(self):
        self.client._client.get_time_series.return_value = SimpleNamespace(status=403,body='private')
        self.assertIsNone(self.client.get_hourly_data(self.lower,self.upper))
        self.client._client.get_time_series.side_effect = OSError('private')
        self.assertIsNone(self.client.get_hourly_data(self.lower,self.upper))
        self.assertNotIn('private',str(self.ns['_LOGGER'].mock_calls))

    def test_short_and_empty_days_return_unknown(self):
        for count in (0,12,23):
            self.client._day_data = TimeSeries(200,NOW,[1]*count)
            self.assertIsNone(self.client.get_usage_hour(24))
        self.client._day_data = TimeSeries(200,NOW,[0,1])
        self.assertEqual(self.client.get_usage_hour(1),0)
        self.assertEqual(self.client.get_usage_hour(2),1)

    def test_tariff_uses_configured_hour(self):
        self.ns['Entity'] = object
        cls = load_class('sensor.py','EloverblikTariff',self.ns)
        data = Mock(); data.get_tariff_sum_hour.side_effect = lambda h:h-1
        sensor = cls('Tariff',data); sensor.update()
        self.assertEqual(sensor.state,12)  # 10:30 UTC is 12:30 configured local time.


class ConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        class RequestException(Exception):
            pass
        class HTTPError(RequestException):
            def __init__(self,status):
                self.response = SimpleNamespace(status_code=status) if status else None
        self.ns = {'core':SimpleNamespace(HomeAssistant=object, callback=lambda f:f), 'Eloverblik':Mock(),
                   'RequestException':RequestException,'HTTPError':HTTPError,
                   'InvalidAuth':type('InvalidAuth',(Exception,),{}),
                   'CannotConnect':type('CannotConnect',(Exception,),{})}
        self.validate = load_function('validate_input',self.ns)
        self.hass = SimpleNamespace(async_add_executor_job=AsyncMock())
        self.input = {'refresh_token':'unused','metering_point':'test'}

    async def test_success(self):
        self.hass.async_add_executor_job.return_value = SimpleNamespace(status=200)
        self.assertEqual(await self.validate(self.hass,self.input),{'title':'Eloverblik test'})

    async def test_returned_error_statuses_are_rejected(self):
        for status in (400,401,403,404,429,500):
            self.hass.async_add_executor_job.return_value = SimpleNamespace(status=status)
            error = self.ns['InvalidAuth' if status in (401,403) else 'CannotConnect']
            with self.subTest(status=status), self.assertRaises(error):
                await self.validate(self.hass,self.input)

    async def test_http_and_transport_errors_classified(self):
        for status in (None,401,403,500):
            self.hass.async_add_executor_job.side_effect = self.ns['HTTPError'](status)
            error = self.ns['InvalidAuth' if status in (401,403) else 'CannotConnect']
            with self.subTest(status=status), self.assertRaises(error):
                await self.validate(self.hass,self.input)
        self.hass.async_add_executor_job.side_effect = self.ns['RequestException']()
        with self.assertRaises(self.ns['CannotConnect']):
            await self.validate(self.hass,self.input)

    async def test_duplicate_aborts_before_validation_without_swallowing_abort(self):
        class Abort(Exception):
            pass
        class HostFlow:
            def __init_subclass__(cls, **kwargs):
                pass
            async def async_set_unique_id(self,value):
                self.unique_id = value
            def _abort_if_unique_id_configured(self):
                raise Abort()
        self.ns.update({'config_entries':SimpleNamespace(ConfigFlow=HostFlow,CONN_CLASS_CLOUD_POLL='cloud'),
                        'DOMAIN':'eloverblik','validate_input':AsyncMock()})
        flow = load_class('config_flow.py','ConfigFlow',self.ns)()
        flow.hass = self.hass
        with self.assertRaises(Abort):
            await flow.async_step_user(self.input)
        self.ns['validate_input'].assert_not_awaited()
        self.assertEqual(flow.unique_id,'test')
