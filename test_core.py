import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from core import *
from bot import admitted, select_channel, EXPECTED_KEY

NOW=datetime(2026,11,7,20,30,tzinfo=timezone.utc).timestamp()
LOCATION={'lat':43.96476,'lon':-83.10631,'name':'Preview repeater','county':'huron','county_boundary':False}
ISSUE='2026-11-07T08:56:00+00:00'
WX={'last_scan_at':'2026-11-07T20:29:00+00:00','counties':{'huron':{'issued_at':ISSUE,'stale':False,'periods':[
    {'date':'2026-11-07','night':False,'text':'Cloudy. Highs around 50. Chance of rain 20 percent.'},
    {'date':'2026-11-07','night':True,'text':'Mostly clear. Lows in the lower 30s.'},
    {'date':'2026-11-08','night':False,'text':'Partly cloudy. Highs in the mid 50s. West winds 10 to 20 mph with gusts up to 35 mph.'},
    {'date':'2026-11-09','night':False,'text':'SHOULD NOT APPEAR'}]}}}

class Tests(unittest.TestCase):
    def test_aliases(self):
        for cmd,target in [('planes','adsb'),('boats','ais'),('spacewx','aurora')]:self.assertEqual(parse_command('!'+cmd)[0],target)
        self.assertIsNone(parse_command('I wonder what wx is'))
        self.assertIsNone(parse_command('pingpong'))
        self.assertEqual(parse_command('infobot: wx huron'),('wx',['huron']))
    def test_ping_and_help(self):
        e=Engine(Config())
        self.assertEqual(e.execute('ping',[]),'Pong!')
        self.assertIn('wx',e.execute('help',[]))
        self.assertIn('DAYDIS',e.execute('help',['spacewx']))
        self.assertEqual(parse_command('about'),('about',[]))
        self.assertEqual(e.execute('about',[]),f'Infobot v{VERSION}\nThe homebrew bot at tarratt.net HQ.\nNetwork commands: internet, get, sat refresh, ping <target>.')
    def test_weather_dates_probabilities(self):
        text=weather(WX,'huron',NOW,LOCATION)
        for value in ['20%','50','Tonight','Tomorrow','35 mph']:self.assertIn(value,text)
        self.assertNotIn('SHOULD NOT',text)
    def test_stale_weather(self):
        self.assertIn('unavailable',weather(WX,'huron',NOW+86400,LOCATION))
    def test_future_weather(self):
        data=json.loads(json.dumps(WX));data['counties']['huron']['issued_at']='2026-11-08T08:56:00+00:00'
        self.assertIn('stale/unavailable',weather(data,'huron',NOW,LOCATION))
    def test_space_event_not_current(self):
        forecast={'issued_at':'2026-11-07T12:30:00+00:00','text':'FXXX12 KWNP 071231\nDAYDIS\nSolar Activity\n.24 hr Summary...\nObserved solar activity.\n\n.Forecast...\nSolar forecast wording.\nEnergetic Particle\n.24 hr Summary...\nObserved particles.\n\n.Forecast...\nParticle forecast wording.\nSolar Wind\n.24 hr Summary...\nObserved wind.\n\n.Forecast...\nWind forecast wording.\nGeospace\n.24 hr Summary...\nObserved geospace.\n\n.Forecast...\nGeospace forecast wording.'}
        text=spaceweather({'last_scan_at':WX['last_scan_at'],'products':{'DAYDIS':forecast}},NOW)
        self.assertIn('11/07 1230Z',text)
        for phrase in ('Solar forecast wording.','Particle forecast wording.','Wind forecast wording.','Geospace forecast wording.'):
            self.assertIn(phrase,text)
        for phrase in ('Observed solar activity.','K-index','Bz'):
            self.assertNotIn(phrase,text)
    def test_aircraft_region_age_and_ground(self):
        good={'hex':'a12345','flight':'TEST1','lat':43.8,'lon':-83.1,'seen_pos':10,'alt_baro':10000,'gs':200}
        outside=dict(good,flight='OUT',lat=46)
        old=dict(good,flight='OLD',seen_pos=61)
        ground=dict(good,flight='GROUND',alt_baro='ground')
        text=aircraft({'now':NOW,'aircraft':[good,outside,old,ground]},Config(),NOW,LOCATION)
        self.assertIn('TEST1',text)
        self.assertIn('OUT:',text)
        self.assertLess(text.index('TEST1:'),text.index('OUT:'))
        for absent in ('OLD:','GROUND:'):self.assertNotIn(absent,text)
    def test_stale_aircraft_file(self):
        self.assertIn('unavailable',aircraft({'now':NOW-130,'aircraft':[]},Config(),NOW,LOCATION))
    def test_ais_age_and_nonships(self):
        ships=[{'mmsi':367123456,'shipname':'FREIGHTER','last_signal':70,'speed':12.5},
               {'mmsi':367123457,'shipname':'OLD','last_signal':1900},
               {'mmsi':993671234,'shipname':'BUOY','last_signal':1}]
        text=vessels({'ships':ships},Config(),NOW,LOCATION)
        self.assertIn('FREIGHTER 12.5kt',text);self.assertNotIn('OLD',text);self.assertNotIn('BUOY',text)
    def test_invalid_ais_schema(self):
        with self.assertRaises(ValueError):vessels({'ships':[[123,4,5]]},Config(),NOW,LOCATION)
    def test_missing_data_never_invented(self):
        self.assertIn('No fresh',aircraft({'now':NOW,'aircraft':[{'hex':'bad','lat':43.8,'lon':-83}]},Config(),NOW,LOCATION))
        self.assertIn('No vessels',vessels({'ships':[{'mmsi':367123456}]},Config(),NOW,LOCATION))
    def test_path_two_bytes_and_ambiguity(self):
        contacts={'aabb'+'00'*30:{'adv_name':'Greenleaf','type':2},'ccdd'+'00'*30:{'adv_name':'One','type':2},'ccdd'+'11'*30:{'adv_name':'Two','type':2}}
        text=path_reply([],{'path':'aabbccdd','path_len':2,'path_hash_mode':1},contacts)
        self.assertIn('Greenleaf/aabb',text);self.assertIn('ambiguous/ccdd',text)
    def test_path_explicit_and_missing(self):
        decoded=path_reply(['aabbccdd'],{},{})
        self.assertIn('❶ ?/aabb',decoded);self.assertIn('❷ ?/ccdd',decoded)
        self.assertIn('hashes unavailable',path_reply([],{'path_len':3},{}))
        self.assertIn('direct',path_reply([],{'path':'','path_len':0},{}))
        self.assertIn('Use path',path_reply(['zxcv'],{},{}))
    def test_utf8_byte_limits(self):
        text='🌞'*200+' hello '+'a'*200
        for part in chunks(text,105):self.assertLessEqual(len(part.encode()),105)
        self.assertEqual(''.join(chunks('🌞'*200,105)),'🌞'*200)
    def test_admission_channel_sender_age(self):
        p={'channel_idx':2,'text':'Brad: ping','sender_timestamp':NOW}
        self.assertEqual(admitted(p,2,NOW),('Brad',('ping',[])))
        self.assertIsNone(admitted(p,1,NOW));self.assertIsNone(admitted(p,2,NOW+121))
        self.assertIsNone(admitted(dict(p,text='infobot: ping'),2,NOW))
        self.assertIsNone(admitted(dict(p,text='Brad: ordinary chat'),2,NOW))
    def test_state_dedupe_persistence_and_cooldown(self):
        with tempfile.TemporaryDirectory() as d:
            path=d+'/s.db';s=State(path)
            p={'channel_idx':2,'text':'Brad: ping','sender_timestamp':time.time()}
            now=time.time()
            self.assertTrue(s.accept(p,'Brad',15,now))
            self.assertFalse(s.accept(dict(p,text='Brad: help'),'Brad',15,now+1))
            s.db.close();s=State(path)
            self.assertFalse(s.accept(p,'Brad',15,now+20))
            s.db.close()
    def test_more_pagination_expiry(self):
        s=State(':memory:');c=Config(parts=2)
        first=s.page('Brad','word '*400,c,NOW)
        self.assertEqual(len(first),2);self.assertTrue(first[-1].endswith('more'))
        for part in first:self.assertLessEqual(len(part.encode()),130)
        self.assertNotEqual(s.page('Brad',None,c,NOW+5),['No more saved text.'])
        self.assertEqual(s.page('Brad',None,c,NOW+601),['No more saved text.']);s.db.close()
    def test_channel_key_and_duplicates(self):
        c={'channel_idx':3,'channel_name':'#infobot','channel_secret':EXPECTED_KEY}
        self.assertEqual(select_channel([c]),3)
        with self.assertRaises(RuntimeError):select_channel([c,c])
        with self.assertRaises(RuntimeError):select_channel([dict(c,channel_secret=b'wrong')])
    def test_public_source_rejected(self):
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',80))]):
            with self.assertRaises(ValueError):local_addresses('bad',80)
    def test_http_source_limits_and_errors(self):
        with self.assertRaises(ValueError):Sources().get('bad','file:///etc/passwd')
    def test_bad_config(self):
        with patch.dict(os.environ,{'REPLY_BYTES':'500'}):
            with self.assertRaises(ValueError):Config.env()

if __name__=='__main__':unittest.main()
