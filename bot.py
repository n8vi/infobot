#!/usr/bin/env python3
"""MeshCore infobot radio adapter. Run --check-sources before taking over radio."""
import argparse
import asyncio
import copy
from collections import deque
from contextlib import suppress
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import signal
import time

from core import VERSION, Config, Engine, State, parse_command, clean, local_addresses, number, packet_metadata_from_raw, validate_source
from command_worker import execute_bounded
from delivery import SerializedCommands, Transmitter

NAME = 'infobot'
CHANNEL = '#infobot'
EXPECTED_KEY = hashlib.sha256(CHANNEL.encode()).digest()[:16]


class ContactRefreshError(RuntimeError):
    """The companion did not provide an authoritative DM sender record."""


def contact_snapshot(mc):
    # Normalize keys before merging: a differently cased pending key must not
    # bypass confirmed-contact precedence. Freeze records for this request.
    pending={key.lower():value for key,value in mc.pending_contacts.items() if isinstance(key,str)}
    confirmed={key.lower():value for key,value in mc.contacts.items() if isinstance(key,str)}
    return copy.deepcopy({**pending,**confirmed})


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


def select_channel(channels, name=CHANNEL):
    matches=[c for c in channels if c.get('channel_name')==name]
    if len(matches)!=1:raise RuntimeError(f'Expected exactly one {name} channel; run --setup-radio or resolve duplicate slots')
    selected=matches[0]
    if selected.get('channel_secret')!=hashlib.sha256(name.encode()).digest()[:16]:
        raise RuntimeError(f'{name} key differs from standard hashtag key; correct it in the app')
    return selected['channel_idx']


async def prepare(mc, setup=False, inspect=False, config=None):
    config = config or Config()
    from meshcore import EventType
    info, channels = await radio_channels(mc)
    if inspect:
        print(json.dumps({'name':mc.self_info.get('name'), 'firmware':info.get('ver'),
                          'path_hash_mode':info.get('path_hash_mode'),
                          'channels':[{'index':c['channel_idx'],'name':c['channel_name']} for c in channels]},indent=2))
        return None
    if setup:
        for name, _policy in config.channels:
            matches=[c for c in channels if c.get('channel_name')==name]
            if not matches:
                empty=next((c for c in channels if c['channel_idx']!=0 and not c.get('channel_name')),None)
                if empty is None: raise RuntimeError(f'No empty channel slot for {name}')
                if not good(await mc.commands.set_channel(empty['channel_idx'],name),EventType.OK):
                    raise RuntimeError(f'Creating {name} failed')
                _,channels=await radio_channels(mc)
            select_channel(channels,name)
        if str(mc.self_info.get('name', '')).strip().casefold()!=NAME.casefold():
            logging.info('Changing companion name from %r to %s',mc.self_info.get('name'),NAME)
            if not good(await mc.commands.set_name(NAME),EventType.OK):raise RuntimeError('Radio rename failed')
        if info.get('path_hash_mode')!=1:
            if not good(await mc.commands.set_path_hash_mode(1),EventType.OK):raise RuntimeError('Two-byte path mode unsupported/failed; update companion firmware')
        if not good(await mc.commands.set_default_flood_scope('mi'),EventType.OK):
            raise RuntimeError('Setting region mi failed; check companion firmware')
        logging.info('Configured name infobot, channels %s, 2-byte paths, default region mi. No chat message sent.',config.channels)
        return select_channel(channels,config.channels[0][0])
    if str(mc.self_info.get('name', '')).strip().casefold()!=NAME.casefold():
        raise RuntimeError(f'Radio name {mc.self_info.get("name")!r} is not infobot. Run --setup-radio first')
    if info.get('path_hash_mode')!=1:raise RuntimeError('Radio is not in 2-byte path mode. Run --setup-radio first')
    for name, _policy in config.channels: select_channel(channels,name)
    return select_channel(channels,config.channels[0][0])


def admitted(payload, channel, now):
    result,_=inspect_admission(payload,channel,now)
    return result


def inspect_admission(payload, channel, now, policy='bare'):
    if payload.get('channel_idx')!=channel:return None,'other channel'
    text=payload.get('text','')
    if not isinstance(text,str):return None,'missing or invalid text'
    if len(text.encode())>512:return None,'message exceeds 512-byte limit'
    sender, separator, message=text.partition(': ')
    if not separator or not sender:return None,'missing sender prefix'
    if sender.casefold()==NAME:return None,'message from infobot itself'
    parsed=parse_command(message,policy)
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
        self.channel_names={}
        self.worker_health={}
        self.last_command_error=None
        self.pending_pages={}
        self.execution_slots=asyncio.Semaphore(config.command_workers)
        self.retired_tasks=set()
        self.next_advert=0.0
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
        for received_at,log in reversed(self.rx_logs):
            if now-received_at>10: break
            # Group logs may omit channel_idx but include the authenticated
            # channel name, which is resolved to its slot at startup.
            log_channel=log.get('channel_idx', self.channel_names.get(log.get('chan_name')))
            if log_channel is not None and log_channel != payload.get('channel_idx'): continue
            target_hash, log_hash = payload.get('pkt_hash'), log.get('pkt_hash')
            if target_hash is not None and log_hash is not None and str(target_hash)!=str(log_hash): continue
            text=log.get('text') or log.get('message')
            if text is not None and text != payload.get('text'): continue
            timestamp=number(log.get('sender_timestamp'))
            if timestamp is not None and timestamp != number(payload.get('sender_timestamp')): continue
            hash_match=target_hash is not None and log_hash is not None and str(target_hash)==str(log_hash)
            text_match=(log_channel is not None and text == payload.get('text') and
                        timestamp is not None and timestamp == number(payload.get('sender_timestamp')))
            if not (hash_match or text_match): continue
            for key in ('route_type','route_typename','transport_code','transport_codes','pkt_payload',
                        'payload_type','path_hash_size','path','path_len','path_hash_mode','pkt_hash'):
                if log.get(key) is not None: payload[key]=log[key]
            if isinstance(log.get('path_hash_size'),int): payload['path_hash_mode']=log['path_hash_size']-1
            payload['_rxlog_matched']=True
            return payload
        # The library's plaintext hash match does not include channel identity.
        # Do not trust an unverified path copied by that weaker correlation.
        payload.pop('path',None)
        return payload

    def health(self,connected,error=None):
        workers={str(key):dict(value) for key,value in self.worker_health.items()}
        healthy=all(value.get('state')!='failed' and
                    not (value.get('state')=='executing' and time.monotonic()-value['since']>self.config.command_timeout+2)
                    for value in workers.values())
        tmp=self.health_path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'at':time.time(),'connected':connected,'workers_ok':healthy,
                                  'workers':workers,'last_command_error':self.last_command_error,'error':error}))
        tmp.replace(self.health_path)

    async def execute(self,engine,cmd,args,payload,contacts):
        if cmd in ('about','help','path','hashsize') or (cmd=='ping' and not args):
            return engine.execute(cmd,args,payload,contacts)
        return await execute_bounded(engine,cmd,args,payload,contacts,self.config.command_timeout,self.execution_slots)

    async def current_dm_contacts(self,mc,sender):
        from meshcore import EventType
        try:
            result=await asyncio.wait_for(mc.commands.get_contact_by_key(bytes.fromhex(sender)),10)
        except Exception as error:
            raise ContactRefreshError('DM sender contact lookup failed') from error
        if not good(result,EventType.NEXT_CONTACT) or not isinstance(result.payload,dict):
            raise ContactRefreshError('Companion did not return the DM sender contact')
        record=result.payload
        key=record.get('public_key')
        if not isinstance(key,str) or key.lower()!=sender.lower():
            raise ContactRefreshError('Companion returned a different contact for the DM sender')
        key=key.lower()
        # Replace the complete record; never retain stale route fields from an
        # older advertisement. Remove pending duplicates only after success.
        for cached in list(mc.contacts):
            if isinstance(cached,str) and cached.lower()==key: mc.contacts.pop(cached)
        mc.contacts[key]=copy.deepcopy(record)
        for pending in list(mc.pending_contacts):
            if isinstance(pending,str) and pending.lower()==key: mc.pending_contacts.pop(pending)
        snapshot=contact_snapshot(mc)
        logging.info('DM route fetched from companion for %s: hops=%r hash_mode=%r path=%r',
                     key,record.get('out_path_len'),record.get('out_path_hash_mode'),record.get('out_path'))
        return snapshot

    async def worker(self,mc,conversation,queue,dead,transmitter):
        engine=Engine(self.config)
        engine.started=self.engine.started
        while not dead.is_set():
            self.worker_health[conversation]={'state':'idle','since':time.monotonic()}
            sender,cmd,args,payload,queued=await queue.get()
            self.queued-=1
            try:
                if time.monotonic()-queued>90:
                    logging.warning('DROP expired request in %s',conversation)
                    continue
                now=time.time()
                self.pending_pages={k:v for k,v in self.pending_pages.items() if v[0]>now}
                if cmd=='repeat':
                    page_number=int(args[0]) if len(args)==1 and args[0].isascii() and args[0].isdigit() else 0
                    repeated=self.state.repeat_page(sender,page_number,now) if page_number else None
                    parts=[repeated if repeated is not None else 'Use repeat <page number>.' if page_number==0 else f'No saved page {page_number} to repeat.']
                elif cmd=='more' and sender in self.pending_pages:
                    _expiry,parts=self.pending_pages.pop(sender)
                else:
                    if cmd=='more': text=None
                    else:
                        self.pending_pages.pop(sender,None)
                        self.worker_health[conversation]={'state':'executing','since':time.monotonic()}
                        try:
                            contacts=contact_snapshot(mc)
                            if conversation[0]=='dm' and cmd in ('wx','sat','adsb','ais') and not (cmd=='wx' and args):
                                contacts=await self.current_dm_contacts(mc,conversation[1])
                            text=await self.execute(engine,cmd,args,payload,contacts)
                        except ContactRefreshError as error:
                            self.last_command_error=str(error)
                            logging.warning('DM route refresh failed for %s: %s',conversation[1],error)
                            text="Location unavailable: couldn't refresh your route from the companion. Try again or use wx <county>."
                        except Exception as error:
                            self.last_command_error=f'{cmd}: {type(error).__name__}: {error}'
                            logging.warning('Command failed in %s: %s',conversation,self.last_command_error)
                            text=f'{cmd}: data source unavailable or command timed out. Try again later.'
                        if cmd=='status':
                            active=sum(v.get('state')!='idle' for v in self.worker_health.values())
                            text+=f' Workers: {active} active/{len(self.worker_health)} total; {self.queued} queued.'
                            if self.last_command_error: text+=' Last command error: '+clean(self.last_command_error)[:100]
                    parts=self.state.page(sender,text,self.config,now)
                self.worker_health[conversation]={'state':'sending','since':time.monotonic()}
                for index,part in enumerate(parts):
                    if dead.is_set(): return
                    if conversation[0]=='channel':
                        await transmitter.channel(conversation[1],part)
                    elif not await transmitter.dm(conversation[1],part):
                        # The cursor already advanced for this batch. Preserve
                        # the failed packet and remaining batch for the next more.
                        expiry=self.state.pages.get(sender,(now+600,))[0]
                        if cmd!='repeat': self.pending_pages[sender]=(expiry,parts[index:])
                        logging.warning('DM delivery unconfirmed in %s; response paused',conversation)
                        break
            except asyncio.CancelledError:
                raise
            except Exception:
                self.worker_health[conversation]={'state':'failed','since':time.monotonic()}
                logging.exception('Conversation worker failed')
                dead.set()
                return
            finally:
                queue.task_done()

    def dm_contact(self,payload,contacts):
        prefix=payload.get('pubkey_prefix','')
        if not isinstance(prefix,str) or not re.fullmatch(r'[0-9a-fA-F]{12}',prefix): return None
        matches={key.lower():value for key,value in contacts.items()
                 if isinstance(key,str) and re.fullmatch(r'[0-9a-fA-F]{64}',key) and key.lower().startswith(prefix.lower())}
        return next(iter(matches)) if len(matches)==1 else None

    async def advertise_if_due(self,transmitter):
        if not self.config.advert_interval or self.stop.is_set() or time.monotonic()<self.next_advert:
            return
        try:
            await transmitter.advert()
        except Exception as error:
            self.next_advert=time.monotonic()+60
            logging.warning('Advert failed; retrying in 60s: %s',error)
        else:
            # Keep the deadline across reconnects to avoid an advert storm.
            self.next_advert=time.monotonic()+self.config.advert_interval

    async def session(self):
        from meshcore import MeshCore,EventType
        host=os.getenv('MESH_HOST','10.0.0.10');port=int(os.getenv('MESH_PORT','5051'))
        await asyncio.wait_for(asyncio.to_thread(local_addresses,host,port),10)
        mc=None
        conversations={}
        self.worker_health={}
        self.queued=0
        self.rx_logs.clear()
        try:
            mc=await asyncio.wait_for(MeshCore.create_tcp(host,port,auto_reconnect=False,default_timeout=8),20)
            if mc is None: raise ConnectionError('Radio handshake failed')
            mc.commands=SerializedCommands(mc.commands)
            await prepare(mc,config=self.config)
            mc.set_decrypt_channel_logs(True)
            _,channels=await radio_channels(mc)
            rules={select_channel(channels,name):policy for name,policy in self.config.channels}
            self.channel_names={c['channel_name']:c['channel_idx'] for c in channels}
            await mc.ensure_contacts()
            dead=asyncio.Event()
            transmitter=Transmitter(mc,self.config)
            mc.subscribe(EventType.DISCONNECTED,lambda event:dead.set())
            def receive(event):
                payload=dict(event.payload)
                now=time.time()
                if event.type==EventType.CHANNEL_MSG_RECV:
                    slot=payload.get('channel_idx')
                    if slot not in rules: return
                    self.attach_rx_metadata(payload)
                    parsed,reason=inspect_admission(payload,slot,now,rules[slot])
                    if parsed is None:
                        if reason!='message from infobot itself':
                            logging.info('DROP channel #%s message: %s; text=%r',slot,reason,payload.get('text'))
                        return
                    sender,(cmd,args)=parsed
                    conversation=('channel',slot)
                else:
                    if not self.config.dm_enabled or payload.get('txt_type',0)!=0: return
                    timestamp=number(payload.get('sender_timestamp'))
                    text=payload.get('text')
                    if timestamp is None or not -30<=now-timestamp<=120 or not isinstance(text,str) or len(text.encode())>512: return
                    key=self.dm_contact(payload,contact_snapshot(mc))
                    parsed=parse_command(text)
                    if key is None or parsed is None: return
                    sender=key
                    cmd,args=parsed
                    conversation=('dm',key)
                    # A private RX event has no captured hop hashes. Resolve
                    # its location separately using this sender's learned route.
                    payload['_dm_sender']=key
                scoped=json.dumps([*conversation,sender],separators=(',',':'))
                payload['_conversation']=json.dumps(conversation)
                if conversation not in conversations:
                    if len(conversations)>=self.config.conversation_limit:
                        idle=next((k for k,(q,t) in conversations.items()
                                   if q.empty() and self.worker_health.get(k,{}).get('state')=='idle'),None)
                        if idle is None: return
                        retired=conversations.pop(idle)[1]
                        retired.cancel()
                        self.retired_tasks.add(retired)
                        retired.add_done_callback(self.retired_tasks.discard)
                        self.worker_health.pop(idle,None)
                    queue=asyncio.Queue(maxsize=self.config.queue_limit)
                    task=asyncio.create_task(self.worker(mc,conversation,queue,dead,transmitter))
                    conversations[conversation]=(queue,task)
                queue,_task=conversations[conversation]
                if queue.full() or self.queued>=self.config.global_queue_limit:
                    logging.warning('DROP request in %s from %s: queue full; text=%r',conversation,sender,payload.get('text'))
                    return
                rejection=self.state.accept_reason(payload,scoped,self.config.cooldown,now)
                if rejection:
                    reason,remaining=rejection
                    logging.info('DROP request in %s from %s: %s (%.1fs remaining); text=%r',
                                 conversation,sender,reason,remaining,payload.get('text'))
                    return
                queue.put_nowait((scoped,cmd,args,payload,time.monotonic()))
                self.queued+=1
                logging.info('RX %s from %s: text=%r command=%s args=%r',
                             conversation,sender,payload.get('text'),cmd,args)
            mc.subscribe(EventType.CHANNEL_MSG_RECV,receive)
            mc.subscribe(EventType.CONTACT_MSG_RECV,receive)
            mc.subscribe(EventType.RX_LOG_DATA,self.receive_rx_log)
            await self.advertise_if_due(transmitter)
            await mc.start_auto_message_fetching()
            logging.info('Listening as infobot v%s: channels %s; DMs %s',VERSION,list(self.config.channels),self.config.dm_enabled)
            self.health(True)
            while not dead.is_set() and not self.stop.is_set():
                waiters=[asyncio.create_task(dead.wait()),asyncio.create_task(self.stop.wait())]
                timeout=min(30,max(.1,self.next_advert-time.monotonic())) if self.config.advert_interval else 30
                done,pending=await asyncio.wait(waiters,timeout=timeout,return_when=asyncio.FIRST_COMPLETED)
                for task in pending: task.cancel()
                await asyncio.gather(*pending,return_exceptions=True)
                if not done:
                    result=await mc.commands.get_bat()
                    if not good(result): raise ConnectionError('Radio heartbeat failed')
                    await mc.ensure_contacts(follow=True)
                    await self.advertise_if_due(transmitter)
                    self.health(True)
        finally:
            self.health(False)
            tasks=[task for _queue,task in conversations.values()] + list(self.retired_tasks)
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            if mc:
                with suppress(Exception): await mc.stop_auto_message_fetching()
                with suppress(Exception): await asyncio.wait_for(mc.disconnect(),5)

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
    try:await prepare(mc,setup=action=='setup',inspect=action=='inspect',config=Config.env())
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
                data=engine.sources.get(label,url,ttl=0)
                validate_source(label,data,config,time.time())
                summary=engine.execute(cmd,[],preview_payload,preview_contacts,now=time.time())
                print(f'{label} {url}\n  '+summary[:500])
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
