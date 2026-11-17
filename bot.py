#!/usr/bin/env python3
"""MeshCore infobot radio adapter. Run --check-sources before taking over radio."""
import argparse
import asyncio
from collections import deque
from contextlib import suppress
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import time

from core import VERSION, Config, Engine, State, parse_command, clean, local_addresses, number, packet_metadata_from_raw

NAME = 'infobot'
CHANNEL = '#infobot'
EXPECTED_KEY = hashlib.sha256(CHANNEL.encode()).digest()[:16]


def good(event, expected=None):
    from meshcore import EventType
    return event is not None and event.type != EventType.ERROR and (expected is None or event.type==expected)


async def radio_channels(mc):
    from meshcore import EventType
    info = await mc.commands.send_device_query()
    if not good(info, EventType.DEVICE_INFO): raise RuntimeError('Radio device query failed')
    maximum = int(info.payload.get('max_channels', 8))
    channels=[]
    for idx in range(min(maximum, 64)):
        result=await mc.commands.get_channel(idx)
        if good(result, EventType.CHANNEL_INFO):channels.append(result.payload)
        else:raise RuntimeError(f'Cannot read channel slot {idx}; stopping to avoid a wrong-channel reply')
    return info.payload, channels


def select_channel(channels):
    matches=[c for c in channels if c.get('channel_name')==CHANNEL]
    if len(matches)!=1:raise RuntimeError('Expected exactly one #infobot channel; run --setup-radio or resolve duplicate slots')
    selected=matches[0]
    if selected.get('channel_secret')!=EXPECTED_KEY:
        raise RuntimeError('#infobot key differs from standard hashtag key; correct it in the app')
    return selected['channel_idx']


async def prepare(mc, setup=False, inspect=False):
    from meshcore import EventType
    info, channels = await radio_channels(mc)
    if inspect:
        print(json.dumps({'name':mc.self_info.get('name'), 'firmware':info.get('ver'),
                          'path_hash_mode':info.get('path_hash_mode'),
                          'channels':[{'index':c['channel_idx'],'name':c['channel_name']} for c in channels]},indent=2))
        return None
    if setup:
        matches=[c for c in channels if c.get('channel_name')==CHANNEL]
        if not matches:
            empty=next((c for c in channels if c['channel_idx']!=0 and not c.get('channel_name')),None)
            if empty is None: raise RuntimeError('No empty channel slot; add #infobot in the app first')
            if not good(await mc.commands.set_channel(empty['channel_idx'], CHANNEL),EventType.OK):
                raise RuntimeError('Creating #infobot failed')
            _,channels=await radio_channels(mc)
        select_channel(channels)
        if str(mc.self_info.get('name', '')).strip().casefold()!=NAME.casefold():
            logging.info('Changing companion name from %r to %s',mc.self_info.get('name'),NAME)
            if not good(await mc.commands.set_name(NAME),EventType.OK):raise RuntimeError('Radio rename failed')
        if info.get('path_hash_mode')!=1:
            if not good(await mc.commands.set_path_hash_mode(1),EventType.OK):raise RuntimeError('Two-byte path mode unsupported/failed; update companion firmware')
        if not good(await mc.commands.set_default_flood_scope('mi'),EventType.OK):
            raise RuntimeError('Setting region mi failed; check companion firmware')
        logging.info('Configured name infobot, #infobot, 2-byte paths, default region mi. No chat message sent.')
        return select_channel(channels)
    if str(mc.self_info.get('name', '')).strip().casefold()!=NAME.casefold():
        raise RuntimeError(f'Radio name {mc.self_info.get("name")!r} is not infobot. Run --setup-radio first')
    if info.get('path_hash_mode')!=1:raise RuntimeError('Radio is not in 2-byte path mode. Run --setup-radio first')
    return select_channel(channels)


def admitted(payload, channel, now):
    result,_=inspect_admission(payload,channel,now)
    return result


def inspect_admission(payload, channel, now):
    if payload.get('channel_idx')!=channel:return None,'other channel'
    text=payload.get('text','')
    if not isinstance(text,str):return None,'missing or invalid text'
    if len(text.encode())>512:return None,'message exceeds 512-byte limit'
    sender, separator, message=text.partition(': ')
    if not separator or not sender:return None,'missing sender prefix'
    if sender.casefold()==NAME:return None,'message from infobot itself'
    parsed=parse_command(message)
    if parsed is None:return None,'not a supported command'
    timestamp=number(payload.get('sender_timestamp'))
    if timestamp is None:return None,'missing or invalid sender timestamp'
    age=now-timestamp
    if age < -30:return None,f'sender timestamp is {abs(age):.1f}s in the future'
    if age > 120:return None,f'message is {age:.1f}s old (limit 120s)'
    return (clean(sender),parsed),None


class Runner:
    def __init__(self,config):
        self.config=config
        Path(config.state).parent.mkdir(parents=True,exist_ok=True)
        self.state=State(config.state)
        self.engine=Engine(config)
        self.rx_logs=deque(maxlen=128)
        self.last_tx=0
        self.stop=asyncio.Event()
        self.health_path=Path(config.state).with_name('health.json')

    def receive_rx_log(self,event):
        log=event.payload
        if not isinstance(log,dict):return
        raw=log.get('payload') or log.get('raw_hex') or log.get('raw')
        fields=dict(log)
        # Prefer decoded event fields supplied by meshcore_py; raw parsing fills
        # only fields absent from that decoded record.
        for key,value in packet_metadata_from_raw(raw).items():
            if fields.get(key) is None:
                fields[key]=value
        self.rx_logs.append((time.monotonic(),fields))

    def attach_rx_metadata(self,payload):
        now=time.monotonic()
        target_ts=number(payload.get('sender_timestamp'))
        target_chan=payload.get('channel_idx')
        target_text=payload.get('text')
        target_hash=payload.get('pkt_hash')
        for received_at,log in reversed(self.rx_logs):
            age=now-received_at
            if age>10:break
            log_hash=log.get('pkt_hash')
            match=(target_hash is not None and log_hash is not None
                   and str(target_hash)==str(log_hash))
            if not match:
                log_chan=log.get('channel_idx')
                if target_chan is not None and log_chan is not None and target_chan!=log_chan:continue
                log_ts=number(log.get('sender_timestamp',log.get('timestamp',log.get('recv_time'))))
                log_text=log.get('text') or log.get('message')
                if target_text and isinstance(log_text,str) and target_text==log_text:
                    match=True
                elif target_ts is not None and log_ts is not None and abs(target_ts-log_ts)<=2:
                    match=True
            if not match:continue
            for key in ('route_type','route_typename','transport_code','transport_codes','pkt_payload',
                        'payload_type','path_hash_size','path','path_len','path_hash_mode','pkt_hash'):
                if payload.get(key) is None and log.get(key) is not None:payload[key]=log[key]
            payload['_rxlog_matched']=True
            return payload
        # RX_LOG_DATA normally arrives immediately before the decrypted channel
        # message. Its packet type identifies channel text, allowing a safe
        # order-based fallback when the log has no message hash/timestamp.
        for received_at,log in reversed(self.rx_logs):
            if now-received_at>2:break
            if log.get('payload_type')!=5:continue  # PAYLOAD_TYPE_GRP_TXT
            for key in ('route_type','route_typename','transport_code','transport_codes','pkt_payload',
                        'payload_type','path_hash_size','path','path_len','path_hash_mode','pkt_hash'):
                if payload.get(key) is None and log.get(key) is not None:payload[key]=log[key]
            payload['_rxlog_matched']=True
            break
        return payload

    def health(self,connected,error=None):
        tmp=self.health_path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'at':time.time(),'connected':connected,'error':error}))
        tmp.replace(self.health_path)

    async def worker(self,mc,channel,queue,dead):
        from meshcore import EventType
        while not dead.is_set():
            sender,cmd,args,payload,queued=await queue.get()
            age=time.monotonic()-queued
            if age>90:
                logging.warning('DROP queued command from %s (%s): waited %.1fs, over 90s limit',sender,cmd,age)
                continue
            try:
                if cmd=='repeat':
                    page_number=int(args[0]) if len(args)==1 and args[0].isdigit() else 0
                    repeated=self.state.repeat_page(sender,page_number,time.time()) if page_number else None
                    parts=[repeated if repeated is not None else 'Use repeat <page number>.' if page_number==0 else f'No saved page {page_number} to repeat.']
                else:
                    if cmd=='more':text=None
                    else:
                        contacts={**mc.contacts,**mc.pending_contacts}
                        try:text=await asyncio.to_thread(self.engine.execute,cmd,args,payload,contacts)
                        except Exception as error:
                            logging.warning('%s source failed: %s',cmd,error)
                            text=f'{cmd}: local data source unavailable or invalid. Try again later.'
                    parts=self.state.page(sender,text,self.config,time.time())
                for part in parts:
                    if dead.is_set():return
                    await asyncio.sleep(max(0,self.config.gap-(time.monotonic()-self.last_tx)))
                    if dead.is_set():return
                    result=await mc.commands.send_chan_msg(channel,part)
                    self.last_tx=time.monotonic()
                    if not good(result,EventType.OK):raise ConnectionError('Radio did not accept reply; not retrying to avoid duplicates')
                    logging.info('TX accepted by radio on channel #%s (delivery unconfirmed): %r',channel,part)
            except Exception:
                logging.exception('Reply worker failed')
                dead.set()
                return

    async def session(self):
        from meshcore import MeshCore,EventType
        host=os.getenv('MESH_HOST','10.0.0.10');port=int(os.getenv('MESH_PORT','5051'))
        await asyncio.to_thread(local_addresses,host,port)
        mc=None;worker=None
        try:
            mc=await asyncio.wait_for(MeshCore.create_tcp(host,port,auto_reconnect=False,default_timeout=8),20)
            if mc is None:raise ConnectionError('Radio handshake failed')
            channel=await prepare(mc)
            mc.set_decrypt_channel_logs(True)  # This is a method, not a property.
            await mc.commands.get_channel(channel)  # Load channel key into RX-log decoder.
            await mc.ensure_contacts()
            dead=asyncio.Event();queue=asyncio.Queue(maxsize=8)
            mc.subscribe(EventType.DISCONNECTED,lambda event:dead.set())
            def receive(event):
                now=time.time()
                payload=dict(event.payload)
                self.attach_rx_metadata(payload)
                parsed,reason=inspect_admission(payload,channel,now)
                if parsed is None:
                    if reason!='other channel' and reason!='message from infobot itself':
                        body=payload.get('text','')
                        logging.info('DROP channel message: %s; text=%r',reason,body[:160])
                    return
                sender,(cmd,args)=parsed
                if cmd=='hashsize':
                    logging.info('hashsize RX metadata: rx_log=%s route=%r transport=%r path=%r/%r',
                                 payload.get('_rxlog_matched',False),payload.get('route_type'),
                                 payload.get('transport_code',payload.get('transport_codes')),
                                 payload.get('path_hash_size'),payload.get('path_len'))
                if queue.full():
                    logging.warning('DROP command from %s (%s): work queue full (%s waiting)',sender,cmd,queue.qsize())
                    return
                rejection=self.state.accept_reason(payload,sender,self.config.cooldown,now)
                if rejection:
                    reason,remaining=rejection
                    if reason=='cooldown':
                        logging.info('DROP command from %s (%s): user cooldown, %.1fs remaining',sender,cmd,remaining)
                    else:
                        logging.info('DROP command from %s (%s): duplicate packet',sender,cmd)
                    return
                queue.put_nowait((sender,cmd,args,dict(payload),time.monotonic()))
                logging.info('RX %s: %s args=%r',sender,cmd,args)
            mc.subscribe(EventType.CHANNEL_MSG_RECV,receive)
            mc.subscribe(EventType.RX_LOG_DATA,self.receive_rx_log)
            worker=asyncio.create_task(self.worker(mc,channel,queue,dead))
            await mc.start_auto_message_fetching()
            logging.info('Listening as infobot v%s ONLY on #infobot (slot %s); user cooldown %.1fs, TX gap %.1fs',
                         VERSION,channel,self.config.cooldown,self.config.gap)
            self.health(True)
            while not dead.is_set() and not self.stop.is_set():
                waiters=[asyncio.create_task(dead.wait()),asyncio.create_task(self.stop.wait())]
                done,pending=await asyncio.wait(waiters,timeout=30,return_when=asyncio.FIRST_COMPLETED)
                for task in pending:task.cancel()
                await asyncio.gather(*pending,return_exceptions=True)
                if not done:
                    # Local serial/TCP health query; does not transmit on LoRa.
                    result=await mc.commands.get_bat()
                    if not good(result):raise ConnectionError('Radio heartbeat failed')
                    await mc.ensure_contacts()
                    self.health(True)
        finally:
            self.health(False)
            if worker:
                worker.cancel()
                with suppress(asyncio.CancelledError):await worker
            if mc:
                with suppress(Exception):await asyncio.wait_for(mc.disconnect(),5)

    async def run(self):
        loop=asyncio.get_running_loop()
        for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,self.stop.set)
        delay=3
        while not self.stop.is_set():
            try:
                await self.session()
                delay=3
            except Exception as error:
                logging.error('Connection/setup: %s',error)
                self.health(False,str(error))
            if self.stop.is_set():break
            try:await asyncio.wait_for(self.stop.wait(),timeout=delay)
            except asyncio.TimeoutError:pass
            delay=min(60,delay*2)


async def one_radio_action(action):
    from meshcore import MeshCore
    host=os.getenv('MESH_HOST','10.0.0.10');port=int(os.getenv('MESH_PORT','5051'))
    await asyncio.to_thread(local_addresses,host,port)
    mc=await asyncio.wait_for(MeshCore.create_tcp(host,port,auto_reconnect=False,default_timeout=8),20)
    if mc is None:raise ConnectionError('Radio handshake failed')
    try:await prepare(mc,setup=action=='setup',inspect=action=='inspect')
    finally:await mc.disconnect()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version',action='version',version=f'%(prog)s {VERSION}')
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument('--inspect-radio',action='store_true')
    modes.add_argument('--setup-radio',action='store_true')
    modes.add_argument('--check-sources',action='store_true')
    modes.add_argument('--preview',metavar='COMMAND')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('meshcore').setLevel(logging.WARNING)
    config=Config.env()
    # A deterministic local preview location keeps command formatting and
    # source-schema checks useful without opening the radio connection.
    preview_hash='aabb'
    preview_payload={'path':preview_hash,'path_len':1,'path_hash_mode':1}
    preview_contacts={preview_hash: {'type':2,'adv_name':'Preview repeater',
                                     'adv_lat':43.96476,'adv_lon':-83.10631}}
    if args.check_sources:
        engine=Engine(config);failures=0
        for label,cmd,url in [('GOES','wx',config.goes+'/v1/weather'),('ADSB','adsb',config.adsb),('AIS','ais',config.ais)]:
            try:
                engine.sources.get(label,url,ttl=0)
                summary=engine.execute(cmd,[],preview_payload,preview_contacts,now=time.time())
                print(f'{label} {url}\n  '+summary[:500])
                if 'unavailable' in summary.lower() or 'stale' in summary.lower():failures+=1
            except Exception as error:
                print(f'{label} FAILED: {url}: {error}');failures+=1
        raise SystemExit(1 if failures else 0)
    if args.preview:
        parsed=parse_command(args.preview)
        if not parsed or parsed[0]=='more':raise SystemExit('Use a supported command other than more')
        if parsed[0]=='ping' and parsed[1]:
            raise SystemExit('Ping preview would send network probes; use ping <target> in #infobot.')
        engine=Engine(config)
        text=engine.execute(*parsed,preview_payload,preview_contacts)
        from core import chunks
        for part in chunks(text,config.byte_limit-25):print(f'{len(part.encode())} bytes: {part}')
        return
    if args.setup_radio or args.inspect_radio:
        asyncio.run(one_radio_action('setup' if args.setup_radio else 'inspect'));return
    asyncio.run(Runner(config).run())


if __name__=='__main__':main()
