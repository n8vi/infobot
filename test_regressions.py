import asyncio
from collections import deque
import multiprocessing
import os
from pathlib import Path
import socket
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bot import Runner
from command_worker import execute_bounded
from core import Config, Engine, Sources, State, county_for_location, weather_counties, parse_command, spaceweather, validate_source
from internet_features import fetch_one_packet
from test_core import WX, NOW, LOCATION


class RegressionTests(unittest.TestCase):
    def test_old_environment_and_new_config(self):
        old=dict(line.split('=',1) for line in Path('.env.example').read_text().splitlines()
                 if line and not line.startswith('#') and line.split('=',1)[0] in {
                 'MESH_HOST','MESH_PORT','GOES_URL','AIS_URL','ADSB_URL','AIRCRAFT_MAX_AGE','SHIP_MAX_AGE',
                 'REGION_NAMES','REPLY_BYTES','REPLY_PARTS','TX_GAP','USER_COOLDOWN','SAT_ALT_M','SAT_TLE_REFRESH_HOURS'})
        with patch.dict(os.environ,old,clear=True):
            c=Config.env()
            self.assertEqual(c.channels,(('#infobot','bare'),))
            self.assertEqual((c.gap,c.byte_limit,c.parts),(6,130,3))
            self.assertEqual(c.adsb,'http://44.31.120.1/tar1090/data/aircraft.json')
        with patch.dict(os.environ,{'BOT_CHANNELS':'#infobot=bare,#test=mention','DM_ENABLED':'false'},clear=True):
            self.assertEqual(Config.env().channels[1],('#test','mention'))
            self.assertFalse(Config.env().dm_enabled)
        for key in ('TX_GAP','USER_COOLDOWN','SAT_ALT_M','SAT_TLE_REFRESH_HOURS','DM_ACK_TIMEOUT','COMMAND_TIMEOUT'):
            for value in ('nan','inf','-inf'):
                with self.subTest(key=key,value=value),patch.dict(os.environ,{key:value},clear=True):
                    with self.assertRaises(ValueError): Config.env()

    def test_channel_activation_policies(self):
        cases={'bare':('ping','!ping','@[infobot] ping','@[INFOBOT]: ping','infobot: ping'),
               'bang':('!ping',), 'mention':('@[infobot] ping','@[INFOBOT]: ping'),
               'either':('!ping','@[infobot] ping','@[INFOBOT]: ping')}
        for policy,accepted in cases.items():
            for text in ('ping','!ping','@[infobot] ping','@[INFOBOT]: ping','@infobot ping','@[other] ping','infobot: ping'):
                parsed=parse_command(text,policy)
                self.assertEqual(parsed is not None,text in accepted,(policy,text))
        self.assertIsNone(parse_command('@[infobot]ping'))

    def test_metadata_requires_identity_and_rejects_conflicts(self):
        r=object.__new__(Runner);r.channel_names={'#infobot':2}
        target={'channel_idx':2,'text':'Brad: wx','sender_timestamp':NOW}
        good={'chan_name':'#infobot','message':'Brad: wx','sender_timestamp':NOW,'path':'aabb','path_len':1}
        for changes in ({'message':'Other: wx'},{'chan_name':'#other','channel_idx':9},
                        {'sender_timestamp':NOW+1},{'message':None,'sender_timestamp':None}):
            log=dict(good,**changes);r.rx_logs=deque([(time.monotonic(),log)])
            self.assertNotIn('path',r.attach_rx_metadata(dict(target)))
        r.rx_logs=deque([(time.monotonic(),good)])
        self.assertEqual(r.attach_rx_metadata(dict(target))['path'],'aabb')
        r.rx_logs=deque([(time.monotonic(),dict(good,pkt_hash=2))])
        self.assertNotIn('path',r.attach_rx_metadata(dict(target,pkt_hash=1)))

    def test_explicit_county_needs_no_location(self):
        source=SimpleNamespace(get=lambda *args,**kwargs: WX)
        reply=Engine(Config(),source).execute('wx',['huron'],{'path':'aabb','path_len':1,'path_hash_mode':1},{},NOW)
        self.assertIn('Huron WX',reply)

    def test_stale_spaceweather_and_index_errors(self):
        record={'issued_at':'2026-11-07T12:30:00+00:00','text':'Solar Activity\n.Forecast...\nForecast prose.'}
        data={'last_scan_at':WX['last_scan_at'],'products':{'DAYDIS':record}}
        self.assertIn('Forecast prose',spaceweather(data,NOW))
        for change in ({'stale':True},{'future_timestamp':True},{'issued_at':'2000-01-01T00:00:00+00:00'}):
            self.assertIn('unavailable',spaceweather(dict(data,products={'DAYDIS':dict(record,**change)}),NOW))
        self.assertIn('unavailable',spaceweather(dict(data,index_error='disk failure'),NOW))
        self.assertIn('unavailable',spaceweather(dict(data,last_scan_at='2000-01-01T00:00:00+00:00'),NOW))

    def test_county_geometry_and_fallback(self):
        self.assertEqual(county_for_location(LOCATION['lat'],LOCATION['lon']),("huron",False))
        self.assertEqual(weather_counties(LOCATION)[0][0],'huron')
        self.assertEqual(weather_counties({'lat':0,'lon':0,'county':None}),([],True))

    def test_scoped_state_and_repeat_keeps_latest_batch(self):
        s=State(':memory:');c=Config(parts=2)
        try:
            a=s.page('channel1:Brad','alpha '*200,c,NOW)
            s.page('channel2:Brad','beta '*200,c,NOW)
            saved=list(s.pages['channel1:Brad'][1])
            self.assertEqual(s.repeat_page('channel1:Brad',1,NOW),a[0])
            self.assertEqual(s.pages['channel1:Brad'][1],saved)
            b=s.page('channel1:Brad',None,c,NOW)
            self.assertEqual(s.repeat_page('channel1:Brad',1,NOW),b[0])
            self.assertIn('beta',s.repeat_page('channel2:Brad',1,NOW))
            p={'channel_idx':1,'text':'Brad: ping','sender_timestamp':NOW}
            self.assertTrue(s.accept(dict(p,_conversation='a'),'a',15,NOW))
            self.assertTrue(s.accept(dict(p,_conversation='b'),'b',15,NOW))
        finally:s.db.close()

    def test_source_checks_use_structure(self):
        validate_source('GOES',WX,Config(),NOW)
        with self.assertRaises(ValueError):validate_source('GOES',dict(WX,counties={}),Config(),NOW)
        with self.assertRaises(ValueError):validate_source('ADSB',{'aircraft':[]},Config(),NOW)
        validate_source('AIS',{'ships':[]},Config(),NOW)

    def test_lan_http_redirects_limits_and_dns_pinning(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path=='/redirect':
                    self.send_response(302);self.send_header('Location','http://8.8.8.8/');self.end_headers()
                else:
                    body=b'{"value": 1}'
                    self.send_response(200);self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        try:
            port=server.server_address[1]
            def resolve(host,*args,**kwargs):
                self.assertEqual(host,'local.test')
                return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',port))]
            with patch('socket.getaddrinfo',side_effect=resolve) as dns:
                self.assertEqual(Sources().get('test',f'http://local.test:{port}/'),{'value':1})
                self.assertEqual(dns.call_count,1)
                with self.assertRaises(ValueError):Sources().get('test',f'http://local.test:{port}/redirect')
        finally:server.shutdown();server.server_close();thread.join()

    def test_public_get_blocks_private_addresses(self):
        self.assertIn('only public',fetch_one_packet('http://127.0.0.1/secret',130))
        self.assertIn('only public',fetch_one_packet('http://44.31.120.1/',130))


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_deadline_reaps_worker(self):
        engine=Engine(Config())
        before={p.pid for p in multiprocessing.active_children()}
        with self.assertRaises(TimeoutError):
            await execute_bounded(engine,'ping',[],{},{},.001,asyncio.Semaphore(1))
        self.assertEqual({p.pid for p in multiprocessing.active_children()},before)
        self.assertEqual(await execute_bounded(engine,'ping',[],{},{},5,asyncio.Semaphore(1)),'Pong!')

    async def test_cancellation_stops_an_active_http_command(self):
        entered=threading.Event();release=threading.Event()
        class SlowHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                entered.set();release.wait(5)
                self.send_response(200);self.end_headers()
                try:self.wfile.write(b'{"ships":[]}')
                except BrokenPipeError:pass
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),SlowHandler)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        before={p.pid for p in multiprocessing.active_children()}
        engine=Engine(Config(ais=f'http://127.0.0.1:{server.server_address[1]}/'))
        slots=asyncio.Semaphore(1)
        task=asyncio.create_task(execute_bounded(engine,'ais',[],{},{},10,slots))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait,5))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual({p.pid for p in multiprocessing.active_children()},before)
            self.assertEqual(await execute_bounded(engine,'ping',[],{},{},5,slots),'Pong!')
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            release.set();server.shutdown();server.server_close();thread.join()
