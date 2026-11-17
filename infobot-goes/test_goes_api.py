import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from goes_api import Index, parse_weather, parse_space, period_date, local_issue, zone_codes

UTC = timezone.utc
# Compact fixture transcribed from Brad's Nov 7 reception, retaining actual
# timestamps, zone IDs, formatting, and representative forecast wording.
WX = '''FPUS53 KDTX 070856

ZFPDTX
Southeast Michigan Zone Forecasts
National Weather Service Detroit/Pontiac MI
356 AM EST Sat Nov 7 2026

'''
for zone, county, today in [
    ('049', 'Huron', 'Cloudy. A slight chance of rain until late afternoon. Highs around 50.'),
    ('054', 'Tuscola', 'Cloudy. Highs in the lower 50s.'),
    ('055', 'Sanilac', 'Cloudy with a 40 percent chance of rain. Highs around 50.')]:
    WX += f'''MIZ{zone}-072115-
{county}-
356 AM EST Sat Nov 7 2026

.TODAY...{today}

.TONIGHT...Mostly clear. Lows in the lower 30s.

.SUNDAY...Partly cloudy. Highs in the mid 50s. West winds 10 to

20 mph with gusts up to 35 mph.

.SUNDAY NIGHT...Mostly cloudy. Lows in the upper 30s.

.MONDAY...Partly cloudy. Highs in the lower 50s.

$$
'''
ALERT = '''WOXX11 KWNP 071951

ALTK05

Space Weather Message Code: ALTK05
Serial Number: 2061
Issue Time: 2026 Nov 07 1945 UTC

ALERT: Geomagnetic K-index of 5
Threshold Reached: 2026 Nov 07 1945 UTC
Synoptic Period: 1800-2100
Active Warning: YES
Noaa Scale: G1 - Minor
'''
DISCUSSION = '''FXXX12 KWNP 071231
DAYDIS
:Product: Forecast Discussion
:Issued: 2026 Nov 07 1230 UTC
Solar Wind
.24 hr Summary...
Total magnetic field strength reached 15 nT, and the Bz
component reached as far south as -11 nT.
.Forecast...
The solar wind environment is expected to remain enhanced.
Geospace
.24 hr Summary...
The geomagnetic field was at quiet to unsettled levels.
'''

class Tests(unittest.TestCase):
    def index(self):
        issued, data = parse_weather(WX)
        i = Index('/nonexistent')
        i.records['ZFPDTX'] = dict(data, issued_at=issued.isoformat(), delivery_at=issued.isoformat(), filename='sample.TXT')
        return i

    def test_three_counties_and_wrapped_text(self):
        issued, data = parse_weather(WX)
        self.assertEqual(issued.astimezone(UTC).hour, 8)
        self.assertEqual(set(data['counties']), {'huron', 'tuscola', 'sanilac'})
        sunday = data['counties']['huron']['periods'][2]
        self.assertEqual(sunday['date'], '2026-11-08')
        self.assertIn('10 to 20 mph', sunday['text'])

    def test_only_today_tomorrow(self):
        _, data = self.index().respond('/v1/weather', {}, datetime(2026, 11, 7, 20, tzinfo=UTC))
        self.assertEqual(len(data['counties']['huron']['periods']), 4)
        self.assertFalse(data['stale'])

    def test_local_midnight_rollover(self):
        _, data = self.index().respond('/v1/weather', {}, datetime(2026, 11, 8, 5, tzinfo=UTC))
        self.assertEqual(data['requested_dates'], ['2026-11-08', '2026-11-09'])
        self.assertEqual(data['counties']['huron']['periods'][0]['label'], 'SUNDAY')
        self.assertTrue(data['stale'])

    def test_expired_does_not_relabel(self):
        _, data = self.index().respond('/v1/weather', {}, datetime(2026, 11, 20, tzinfo=UTC))
        self.assertEqual(data['counties']['huron']['periods'], [])
        self.assertEqual(len(data['counties']['huron']['missing_dates']), 2)

    def test_county_filter_and_error(self):
        i = self.index()
        _, data = i.respond('/v1/weather', {'county': ['huron']})
        self.assertEqual(list(data['counties']), ['huron'])
        self.assertEqual(i.respond('/v1/weather', {'county': ['bogus']})[0], 400)

    def test_space_threshold_not_live_kp(self):
        issued, data = parse_space(ALERT)
        self.assertEqual(data['k_threshold'], 5)
        self.assertEqual(data['kind'], 'threshold_alert')
        self.assertEqual(issued.hour, 19)
        self.assertNotIn('current_kp', data)

    def test_bz_context(self):
        _, data = parse_space(DISCUSSION)
        self.assertIn('-11 nT', data['solar_wind']['past_24h_summary'])
        self.assertIsNone(data['solar_wind']['current_bz_nt'])

    def test_combined_zones(self):
        self.assertEqual(zone_codes('MIZ049-054>055-\n072115-\n'), {'MIZ049', 'MIZ054', 'MIZ055'})

    def test_unknown_period_not_guessed(self):
        issue = local_issue(WX)
        self.assertIsNone(period_date('SUNDAY THROUGH TUESDAY', issue, issue.date()))

    def test_early_morning_rest_of_tonight(self):
        issue = local_issue('1230 AM EST Sun Nov 8 2026')
        self.assertEqual(str(period_date('REST OF TONIGHT', issue, issue.date())), '2026-11-07')

    def test_missing_malformed_and_unknown(self):
        with self.assertRaises(ValueError):
            parse_weather(WX.split('$$')[0])
        with self.assertRaises(ValueError):
            parse_space('DAYDIS broken')
        i = Index('/missing')
        self.assertEqual(i.respond('/v1/weather', {})[0], 503)
        self.assertEqual(i.respond('/v1/spaceweather', {})[0], 503)
        self.assertEqual(i.respond('/wrong', {})[0], 404)

    def test_scan_caches_ignores_malformed_and_old_replay(self):
        with tempfile.TemporaryDirectory() as d:
            def write(name, text):
                p = Path(d) / name
                p.write_text(text)
                os.utime(p, (1, 1))
                return p
            write('A_FPUS53KDTX070856_C_KWIN_20261107085642_068333-3-ZFPDTXMI.TXT', WX)
            write('A_WOXX11KWNP071951_C_KWIN_20261107195130_099613-1-ALTK05US.txt', ALERT)
            write('A_WOXX11KWNP072051_C_KWIN_20261107205130_100613-1-ALTK05US.txt', ALERT.replace('1945 UTC', '1845 UTC'))
            broken = write('A_WOXX11KWNP072151_C_KWIN_20261107215130_101613-1-ALTK05US.txt', 'bad')
            i = Index(d)
            i.scan()
            self.assertEqual(len(i.records), 2)
            self.assertIn('19:45', i.records['ALTK05']['issued_at'])
            self.assertEqual(i.status['parse_error_count'], 1)
            i.scan()
            self.assertEqual(len(i.records), 2)
            broken.unlink()
            i.scan()
            self.assertEqual(i.status['parse_error_count'], 0)

if __name__ == '__main__':
    unittest.main()
