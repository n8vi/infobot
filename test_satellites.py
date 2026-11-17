from datetime import datetime, timezone
import unittest
from core import Config
from internet_features import SatellitePasses, compass
from skyfield.api import EarthSatellite, load, wgs84


class SatelliteTests(unittest.TestCase):
    def test_compass_boundaries(self):
        for azimuth,label in [(0,'N'),(11.24,'N'),(11.25,'NNE'),(45,'NE'),(180,'S'),(225,'SW'),(348.75,'N'),(360,'N')]:
            self.assertEqual(compass(azimuth),label)

    def test_complete_pass_and_already_underway(self):
        # Offline Skyfield documentation fixture; no element downloads.
        ts=load.timescale(builtin=True)
        sat=EarthSatellite('1 25544U 98067A   14020.93268519  .00009878  00000-0  18200-3 0  5082',
                           '2 25544  51.6498 109.4756 0003572  58.1232  36.7439 15.49815350868473','ISS',ts)
        observer=wgs84.latlon(43.96,-83.10,elevation_m=200)
        now=sat.epoch.utc_datetime().timestamp()
        result=SatellitePasses._pass(sat,observer,ts,now)
        # Move just under one hour before rise. The full peak and set must be
        # calculated even though they fall beyond the one-hour selection window.
        before=result['rise']-3590
        full=SatellitePasses._pass(sat,observer,ts,before)
        self.assertGreater(full['set'],before+3600)
        self.assertIsNotNone(full['peak'])
        times,events=sat.find_events(observer,
            ts.from_datetime(datetime.fromtimestamp(full['rise']+1,timezone.utc)),
            ts.from_datetime(datetime.fromtimestamp(full['set'],timezone.utc)),altitude_degrees=0)
        peak_time=next(t.utc_datetime().timestamp() for t,e in zip(times,events) if int(e)==1)
        expected=float((sat-observer).at(ts.from_datetime(datetime.fromtimestamp(peak_time,timezone.utc))).altaz()[0].degrees)
        self.assertAlmostEqual(full['peak'],expected,places=2)
        underway=SatellitePasses._pass(sat,observer,ts,peak_time+1)
        self.assertTrue(underway['up'])
        self.assertAlmostEqual(underway['rise'],full['rise'],delta=1)
        self.assertAlmostEqual(underway['peak'],full['peak'],places=2)
        reply=SatellitePasses(Config())._visible_next_hour(ts,[('ISS',25544,sat)],observer,before)
        for phrase in ('rises','peak','sets',full['rise_direction'],full['set_direction']):self.assertIn(phrase,reply)
