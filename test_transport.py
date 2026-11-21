"""Integration test against actual meshcore library using an emulated TCP radio.
No physical hardware and no LoRa transmission. Requires requirements.txt.
"""
import asyncio
from contextlib import suppress
import os
import tempfile
import time
import unittest
from unittest.mock import patch

try:
    import meshcore
    HAS_LIBRARY=True
except ImportError:HAS_LIBRARY=False
from bot import Runner, EXPECTED_KEY
from core import Config

@unittest.skipUnless(HAS_LIBRARY,'install requirements.txt for TCP integration test')
class RadioTest(unittest.IsolatedAsyncioTestCase):
    async def test_web_command_uses_bounded_worker(self):
        from unittest.mock import AsyncMock

        runner = Runner(Config(state=':memory:', command_timeout=45))
        try:
            with patch('bot.execute_bounded', new_callable=AsyncMock, return_value='Answer') as execute:
                self.assertEqual(await runner.execute(runner.engine, 'bot', ['a question'], {}, {}), 'Answer')
                execute.assert_awaited_once_with(runner.engine, 'bot', ['a question'], {}, {}, 45,
                                                runner.execution_slots)
        finally:
            runner.state.db.close()

    async def test_real_library_tcp_ping_and_channel_isolation(self):
        sent=[];adverts=[];completed=asyncio.Event();connections=[]
        ts=int(time.time())
        pending=[(0,'Stranger: ping',ts),(1,'infobot: ping',ts),
                 (1,'Old: ping',ts-600),(1,'Brad: ping',ts),(1,'Brad: ping',ts)]
        async def peer(reader,writer):
            connections.append(writer)
            async def frame(body):
                writer.write(b'>'+len(body).to_bytes(2,'little')+body);await writer.drain()
            try:
                while True:
                    header=await reader.readexactly(3)
                    body=await reader.readexactly(int.from_bytes(header[1:],'little'))
                    cmd=body[0]
                    if cmd==1:
                        await frame(b'\x05'+bytes([1,20,22])+bytes(32)+bytes(8)+bytes(4)+bytes(8)+bytes([8,5])+b'infobot')
                    elif cmd==22:
                        await frame(b'\x0d'+bytes([10,1,3])+bytes(4)+b'test'.ljust(12,b'\0')+b'emulator'.ljust(40,b'\0')+b'1.0'.ljust(20,b'\0')+bytes([0,1]))
                    elif cmd==31:
                        idx=body[1];name=['Public','#infobot',''][idx]
                        await frame(bytes([18,idx])+name.encode().ljust(32,b'\0')+(EXPECTED_KEY if idx==1 else bytes(16)))
                    elif cmd==4:
                        await frame(b'\x02'+bytes(4));await frame(b'\x04'+ts.to_bytes(4,'little'))
                    elif cmd==10:
                        if pending:
                            ch,text,timestamp=pending.pop(0)
                            await frame(bytes([17,20,0,0,ch,64,0])+timestamp.to_bytes(4,'little')+text.encode())
                            await frame(b'\x83')
                        else:await frame(b'\x0a')
                    elif cmd==7:
                        adverts.append(body)
                        await frame(b'\0')
                    elif cmd==3:
                        sent.append((body[2],body[7:].decode()))
                        await frame(b'\0');completed.set()
                    elif cmd==20:await frame(b'\x0c'+(4000).to_bytes(2,'little'))
                    else:await frame(b'\0')
            except (asyncio.IncompleteReadError,ConnectionError):pass
            finally:writer.close()
        server=await asyncio.start_server(peer,'127.0.0.1',0)
        port=server.sockets[0].getsockname()[1]
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'MESH_HOST':'127.0.0.1','MESH_PORT':str(port)}),self.assertLogs(level='INFO') as logs:
            runner=Runner(Config(state=d+'/s.db',gap=0.01))
            task=asyncio.create_task(runner.session())
            try:
                await asyncio.wait_for(completed.wait(),12)
                await asyncio.sleep(.3)
                self.assertEqual(sent,[(1,'Pong!')])
                self.assertEqual(adverts,[b'\x07\x01'])
                self.assertTrue(any("from Brad: text='Brad: ping' command=ping args=[]" in line for line in logs.output))
                self.assertTrue(any("TX accepted by radio on channel #1 (delivery unconfirmed): 'Pong!'" in line for line in logs.output))
            finally:
                runner.stop.set()
                try:await asyncio.wait_for(task,3)
                except asyncio.TimeoutError:task.cancel()
                with suppress(asyncio.CancelledError):await task
                runner.state.db.close()
        server.close();await server.wait_closed()
        for w in connections:w.close()

@unittest.skipUnless(HAS_LIBRARY,'install requirements.txt for radio setup tests')
class SetupTest(unittest.IsolatedAsyncioTestCase):
    async def test_setup_uses_empty_slot_and_keeps_public(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from meshcore import EventType
        from meshcore.events import Event
        from bot import prepare
        channels=[{'channel_idx':0,'channel_name':'Public','channel_secret':bytes(16)},
                  {'channel_idx':1,'channel_name':'#bots','channel_secret':bytes(16)},
                  {'channel_idx':2,'channel_name':'','channel_secret':bytes(16)}]
        commands=SimpleNamespace()
        commands.send_device_query=AsyncMock(return_value=Event(EventType.DEVICE_INFO,{'max_channels':3,'path_hash_mode':0}))
        async def channel(index):return Event(EventType.CHANNEL_INFO,channels[index])
        async def set_channel(index,name):
            channels[index]=dict(channel_idx=index,channel_name=name,channel_secret=EXPECTED_KEY)
            return Event(EventType.OK,{})
        commands.get_channel=AsyncMock(side_effect=channel)
        commands.set_channel=AsyncMock(side_effect=set_channel)
        for name in ('set_name','set_path_hash_mode','set_default_flood_scope'):
            setattr(commands,name,AsyncMock(return_value=Event(EventType.OK,{})))
        mc=SimpleNamespace(commands=commands,self_info={'name':'old bot'})
        result=await prepare(mc,setup=True)
        self.assertEqual(result,2)
        self.assertEqual(channels[0]['channel_name'],'Public')
        self.assertEqual(channels[1]['channel_name'],'#bots')
        commands.set_name.assert_awaited_once_with('infobot')
        commands.set_path_hash_mode.assert_awaited_once_with(1)
        commands.set_default_flood_scope.assert_awaited_once_with('mi')

    async def test_reconnect_after_failure(self):
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as d:
            runner=Runner(Config(state=d+'/s.db'))
            calls=[]
            async def session():
                calls.append(1)
                if len(calls)==1:raise ConnectionError('simulated drop')
                runner.stop.set()
            runner.session=session
            loop=asyncio.get_running_loop()
            with patch.object(loop,'add_signal_handler'):
                await asyncio.wait_for(runner.run(),5)
            self.assertEqual(len(calls),2)
            runner.state.db.close()

@unittest.skipUnless(HAS_LIBRARY,'install requirements.txt for TCP integration test')
class MultiConversationRadioTest(unittest.IsolatedAsyncioTestCase):
    async def test_channels_dms_receipts_and_flood_on_real_wire(self):
        import hashlib
        keys=[bytes.fromhex('11'*32),bytes.fromhex('22'*32)]
        ts=int(time.time())
        # All inputs arrive before the first DM receipt times out.
        pending=[('channel',1,'Brad: ping'),('channel',2,'Brad: ping'),
                 ('channel',2,'Brad: @[infobot] ping'),
                 ('dm_old',keys[0],'ping'),('dm_unknown',bytes.fromhex('33'*32),'ping'),
                 ('dm',keys[0],'ordinary chat'),('dm',keys[0],'wx'),('dm',keys[1],'ping')]
        sent=[];resets=[];contact_queries=[];connections=[];completed=asyncio.Event()
        def check():
            channels=[x for x in sent if x[0]=='channel']
            a=[x for x in sent if x[0]=='dm' and x[1]==keys[0][:6]]
            b=[x for x in sent if x[0]=='dm' and x[1]==keys[1][:6]]
            if len(channels)==2 and len(a)>=2 and b:completed.set()
        async def peer(reader,writer):
            connections.append(writer)
            async def frame(body):
                writer.write(b'>'+len(body).to_bytes(2,'little')+body);await writer.drain()
            try:
                while True:
                    header=await reader.readexactly(3)
                    body=await reader.readexactly(int.from_bytes(header[1:],'little'))
                    cmd=body[0]
                    if cmd==1:
                        await frame(b'\x05'+bytes([1,20,22])+bytes(32)+bytes(8)+bytes(4)+bytes(8)+bytes([8,5])+b'infobot')
                    elif cmd==22:
                        await frame(b'\x0d'+bytes([10,1,3])+bytes(4)+b'test'.ljust(12,b'\0')+b'emulator'.ljust(40,b'\0')+b'1.0'.ljust(20,b'\0')+bytes([0,1]))
                    elif cmd==31:
                        idx=body[1];name=['Public','#infobot','#other'][idx]
                        key=hashlib.sha256(name.encode()).digest()[:16] if idx else bytes(16)
                        await frame(bytes([18,idx])+name.encode().ljust(32,b'\0')+key)
                    elif cmd==4:
                        await frame(b'\x02'+(2).to_bytes(4,'little'))
                        for key in keys:
                            await frame(b'\x03'+key+bytes([1,0,65])+b'\xaa\xbb'.ljust(64,b'\0')+
                                        b'User'.ljust(32,b'\0')+ts.to_bytes(4,'little')+
                                        (43900000).to_bytes(4,'little',signed=True)+(-83100000).to_bytes(4,'little',signed=True)+ts.to_bytes(4,'little'))
                        await frame(b'\x04'+ts.to_bytes(4,'little'))
                    elif cmd==30:
                        key=body[1:33]
                        contact_queries.append(key)
                        await frame(b'\x03'+key+bytes([1,0,65])+b'\xaa\xbb'.ljust(64,b'\0')+
                                    b'User'.ljust(32,b'\0')+ts.to_bytes(4,'little')+
                                    (43900000).to_bytes(4,'little',signed=True)+(-83100000).to_bytes(4,'little',signed=True)+ts.to_bytes(4,'little'))
                    elif cmd==10:
                        if pending:
                            kind,target,text=pending.pop(0)
                            if kind=='channel':
                                await frame(bytes([17,20,0,0,target,64,0])+ts.to_bytes(4,'little')+text.encode())
                            else:
                                await frame(bytes([16,20,0,0])+target[:6]+bytes([255,0])+(ts-121 if kind=='dm_old' else ts).to_bytes(4,'little')+text.encode())
                            await frame(b'\x83')
                        else:await frame(b'\x0a')
                    elif cmd==3:
                        sent.append(('channel',body[2],body[7:].decode()))
                        await frame(b'\0');check()
                    elif cmd==2:
                        key=body[7:13];attempt=body[2];text=body[13:].decode()
                        sent.append(('dm',key,attempt,text,body[3:7]))
                        code=len(sent).to_bytes(4,'little')
                        await frame(b'\x06'+bytes([int(attempt>0)])+code+(10).to_bytes(4,'little'))
                        # First destination misses its first receipt, then gets
                        # only a raw RF ACK (OpenHop without a 0x82 push). The
                        # second destination uses the normal companion receipt.
                        raw_code=code if key==keys[0][:6] and attempt>0 else b'\xff'*4
                        await frame(b'\x88\x00\xba'+b'\x0e\x00'+raw_code+b'\x00\x47')
                        await frame(b'\x88\x00\xba'+b'\x21\x00'+bytes(8))
                        await frame(b'\x82'+b'\xff'*4+(5).to_bytes(4,'little'))
                        await frame(b'\x81'+next(k for k in keys if k[:6]==key))
                        if key!=keys[0][:6]:
                            await frame(b'\x82'+code+(5).to_bytes(4,'little'))
                        check()
                    elif cmd==13:
                        resets.append(body[1:]);await frame(b'\0')
                    elif cmd==20:await frame(b'\x0c'+(4000).to_bytes(2,'little'))
                    else:await frame(b'\0')
            except (asyncio.IncompleteReadError,ConnectionError):pass
            finally:writer.close()
        server=await asyncio.start_server(peer,'127.0.0.1',0)
        port=server.sockets[0].getsockname()[1]
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'MESH_HOST':'127.0.0.1','MESH_PORT':str(port)}),self.assertLogs(level='INFO') as logs:
            runner=Runner(Config(state=d+'/s.db',gap=.01,channels=(('#infobot','bare'),('#other','mention')),
                                 dm_ack_timeout=.5,dm_flood_after=1,dm_retries=2))
            dm_senders=[]
            original_execute=runner.execute
            async def execute(engine,cmd,args,payload,contacts):
                if payload.get('type')=='PRIV':dm_senders.append(payload.get('_dm_sender'))
                if cmd=='wx':
                    self.assertEqual(contacts[keys[0].hex()]['out_path'],'aabb')
                    return 'Pong!'  # avoid accessing live feeds in the wire test
                return await original_execute(engine,cmd,args,payload,contacts)
            runner.execute=execute
            task=asyncio.create_task(runner.session())
            try:
                await asyncio.wait_for(completed.wait(),15)
                await asyncio.sleep(.1)
                self.assertCountEqual([x[1:] for x in sent if x[0]=='channel'],[(1,'Pong!'),(2,'Pong!')])
                a=[x for x in sent if x[0]=='dm' and x[1]==keys[0][:6]]
                self.assertEqual([x[2] for x in a],[0,1])
                self.assertEqual(a[0][4],a[1][4])
                self.assertEqual(resets,[keys[0]])
                self.assertEqual(contact_queries,[keys[0]])
                self.assertCountEqual(dm_senders,[key.hex() for key in keys])
                self.assertTrue(any('RX DM event before admission' in line and "text='wx'" in line for line in logs.output))
                self.assertTrue(any('DROP DM event: message is' in line for line in logs.output))
                self.assertTrue(any('DROP DM event: sender is unknown' in line for line in logs.output))
                self.assertTrue(any('DROP DM event: not a supported command' in line for line in logs.output))
                self.assertTrue(any("text='Brad: @[infobot] ping'" in line for line in logs.output))
                self.assertTrue(any("TX DM accepted by radio" in line and "'Pong!'" in line for line in logs.output))
                self.assertTrue(any("TX DM receipt confirmed" in line and "'Pong!'" in line for line in logs.output))
                self.assertTrue(any('flood fallback' in line for line in logs.output))
                self.assertTrue(any("RX ACK event: code='ffffffff'" in line for line in logs.output))
                self.assertTrue(any('RX raw ACK packet:' in line for line in logs.output))
                self.assertTrue(any('source=raw RF ACK' in line for line in logs.output))
                self.assertTrue(any('source=companion receipt' in line for line in logs.output))
                self.assertTrue(any('RX encrypted PATH packet' in line for line in logs.output))
                self.assertTrue(any('RX PATH_UPDATE event:' in line for line in logs.output))
                b=next(x for x in sent if x[0]=='dm' and x[1]==keys[1][:6])
                self.assertLess(sent.index(b),sent.index(a[1]))
            finally:
                runner.stop.set()
                try:await asyncio.wait_for(task,3)
                except asyncio.TimeoutError:task.cancel()
                with suppress(asyncio.CancelledError):await task
                runner.state.db.close()
                server.close();await server.wait_closed()
                for writer in connections:writer.close()
