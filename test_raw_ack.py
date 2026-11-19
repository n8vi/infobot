import asyncio
import hashlib
from collections import deque
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bot import Runner
from core import Config
from delivery import Transmitter
from meshcore.events import Event, EventDispatcher, EventType

KEY='ab'*32
CAPTURE=bytes.fromhex('0e4350acd1aa7d4691f2ea4a006e')
CODE=bytes.fromhex('91f2ea4a')


class RawAckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dispatcher=EventDispatcher()
        await self.dispatcher.start()
        self.mc=SimpleNamespace(commands=SimpleNamespace(reset_path=AsyncMock()),
            subscribe=self.dispatcher.subscribe,ensure_contacts=AsyncMock())
        self.tx=Transmitter(self.mc,Config(dm_ack_timeout=.01,dm_retries=0))
        self.runner=object.__new__(Runner)
        self.runner.rx_logs=deque(maxlen=128)
        self.tasks=[]

    async def asyncTearDown(self):
        for task in self.tasks:task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        await self.dispatcher.stop()

    def raw_packet(self,raw=CAPTURE):
        self.runner.receive_rx_log(Event(EventType.RX_LOG_DATA,{'payload':raw}),self.tx)

    async def test_captured_six_byte_ack_stops_retry_and_duplicate_receipts_confirm_once(self):
        self.tx.config.dm_retries=3
        def receive():
            self.raw_packet();self.raw_packet()
        async def send(*args,**kwargs):
            asyncio.get_running_loop().call_soon(receive)
            return Event(EventType.MSG_SENT,{'expected_ack':CODE,'suggested_timeout':10000})
        self.mc.commands.send_msg=AsyncMock(side_effect=send)
        with self.assertLogs(level='INFO') as logs:
            self.assertTrue(await self.tx.dm(KEY,'Pong!'))
            await self.dispatcher.dispatch(Event(EventType.ACK,{'code':CODE.hex()},{'code':CODE.hex()}))
            await self.dispatcher.queue.join()
        self.mc.commands.send_msg.assert_awaited_once()
        self.mc.commands.reset_path.assert_not_awaited()
        confirmed=[line for line in logs.output if 'TX DM receipt confirmed' in line]
        self.assertEqual(len(confirmed),1)
        self.assertIn('source=raw RF ACK',confirmed[0])
        self.assertFalse(self.tx.pending_receipts)

    async def test_raw_ack_before_send_result_is_rechecked_after_expected_code_arrives(self):
        async def send(*args,**kwargs):
            self.raw_packet()
            return Event(EventType.MSG_SENT,{'expected_ack':CODE,'suggested_timeout':10000})
        self.mc.commands.send_msg=send
        self.assertTrue(await self.tx.dm(KEY,'Pong!'))

    async def test_legacy_four_byte_ack_and_hex_payload_are_supported(self):
        for payload in (CODE,bytearray(CODE),CODE.hex()):
            async def send(*args,**kwargs):
                asyncio.get_running_loop().call_soon(self.tx.receive_raw_ack,{'payload_type':3,'pkt_payload':payload})
                return Event(EventType.MSG_SENT,{'expected_ack':CODE,'suggested_timeout':10})
            self.mc.commands.send_msg=send
            self.assertTrue(await self.tx.dm(KEY,'Pong!'))

    async def test_invalid_unrelated_ack_and_path_cannot_confirm(self):
        ready=asyncio.Event()
        self.tx.config.dm_ack_timeout=1
        async def send(*args,**kwargs):
            ready.set()
            return Event(EventType.MSG_SENT,{'expected_ack':CODE,'suggested_timeout':10})
        self.mc.commands.send_msg=send
        task=asyncio.create_task(self.tx.dm(KEY,'Pong!'));self.tasks.append(task)
        await ready.wait()
        for fields in ({'payload_type':8,'pkt_payload':CODE},
                       {'payload_type':3,'pkt_payload':bytes(4)},
                       {'payload_type':3,'pkt_payload':CODE[:3]},
                       {'payload_type':3,'pkt_payload':'not hex'},
                       {'payload_type':3,'pkt_payload':None}):
            self.assertFalse(self.tx.receive_raw_ack(fields))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.raw_packet()
        self.assertTrue(await task)

    async def test_late_raw_ack_for_previous_attempt_is_accepted(self):
        self.tx.config.dm_retries=3
        attempts=[]
        async def send(key,text,timestamp,attempt):
            attempts.append(attempt)
            if attempt==1:self.raw_packet()
            return Event(EventType.MSG_SENT,{'expected_ack':CODE if attempt==0 else b'next','suggested_timeout':10})
        self.mc.commands.send_msg=send
        self.assertTrue(await self.tx.dm(KEY,'Pong!'))
        self.assertEqual(attempts,[0,1])
        self.mc.commands.reset_path.assert_not_awaited()

    async def test_raw_ack_only_completes_matching_destination(self):
        other='cd'*32
        ready=asyncio.Event();codes={}
        self.tx.config.dm_ack_timeout=1
        async def send(key,text,timestamp,attempt):
            # Actual MeshCore receipt hash: recipient key is NOT included.
            codes[key]=hashlib.sha256(timestamp.to_bytes(4,'little')+bytes([attempt])+text.encode()+bytes(32)).digest()[:4]
            if len(codes)==2:ready.set()
            return Event(EventType.MSG_SENT,{'expected_ack':codes[key],'suggested_timeout':10})
        self.mc.commands.send_msg=send
        with patch('delivery.time.time',return_value=1795047116):
            a=asyncio.create_task(self.tx.dm(KEY,'Pong!'))
            b=asyncio.create_task(self.tx.dm(other,'Pong!'))
            self.tasks.extend([a,b]);await ready.wait()
            self.assertNotEqual(codes[KEY],codes[other])
            self.tx.receive_raw_ack({'payload_type':3,'pkt_payload':codes[KEY]})
            self.assertTrue(await a)
            self.assertFalse(b.done())
            self.tx.receive_raw_ack({'payload_type':3,'pkt_payload':codes[other]})
            self.assertTrue(await b)

    async def test_old_raw_ack_does_not_confirm_a_new_message(self):
        async def send(key,text,**kwargs):
            self.raw_packet()
            return Event(EventType.MSG_SENT,{'expected_ack':CODE if text=='first' else b'next','suggested_timeout':10})
        self.mc.commands.send_msg=send
        self.assertTrue(await self.tx.dm(KEY,'first'))
        self.assertFalse(await self.tx.dm(KEY,'second'))
        self.assertFalse(self.tx.receive_raw_ack({'payload_type':3,'pkt_payload':CODE}))
