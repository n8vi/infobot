import asyncio
from collections import deque
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from bot import Runner
from core import Config
from delivery import Transmitter
from meshcore.events import Event, EventDispatcher, EventType

KEY='ab'*32


class ReceiptDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dispatcher=EventDispatcher()
        await self.dispatcher.start()
        self.mc=SimpleNamespace(subscribe=self.dispatcher.subscribe,
            commands=SimpleNamespace(),ensure_contacts=AsyncMock())
        self.tx=Transmitter(self.mc,Config(dm_ack_timeout=.01,dm_retries=0))
        self.ack_subscription=self.mc.subscribe(EventType.ACK,self.tx.log_receipt)
        self.path_subscription=self.mc.subscribe(EventType.PATH_UPDATE,self.tx.log_path_update)

    async def asyncTearDown(self):
        self.ack_subscription.unsubscribe()
        self.path_subscription.unsubscribe()
        await self.dispatcher.stop()

    async def emit(self,kind,payload):
        await self.dispatcher.dispatch(Event(kind,payload,payload))
        await self.dispatcher.queue.join()

    async def test_idle_unmatched_and_late_confirmations_are_visible(self):
        async def send(*args,**kwargs):
            await self.emit(EventType.ACK,{'code':'ffffffff','trip_time':123})
            return Event(EventType.MSG_SENT,{'expected_ack':b'abcd','suggested_timeout':10})
        self.mc.commands.send_msg=send
        with self.assertLogs(level='INFO') as logs:
            self.assertFalse(await self.tx.dm(KEY,'Pong!'))
            self.assertFalse(self.tx.pending_receipts)
            await self.emit(EventType.ACK,{'code':b'abcd'.hex(),'trip_time':30000})
        output='\n'.join(logs.output)
        self.assertIn("RX ACK event: code='ffffffff'",output)
        self.assertIn('awaiting_send_result',output)
        self.assertIn("expected=['61626364']",output)
        self.assertIn('trip_time_ms=30000 matching_peers=[] pending=[]',output)
        self.assertNotIn('TX DM receipt confirmed',output)

    async def test_early_receipt_is_logged_before_expected_code_then_matches(self):
        async def send(*args,**kwargs):
            await self.emit(EventType.ACK,{'code':b'abcd'.hex(),'trip_time':12})
            return Event(EventType.MSG_SENT,{'expected_ack':b'abcd','suggested_timeout':10})
        self.mc.commands.send_msg=send
        with self.assertLogs(level='INFO') as logs:
            self.assertTrue(await self.tx.dm(KEY,'Pong!'))
        output='\n'.join(logs.output)
        self.assertIn("'awaiting_send_result': True, 'expected': []",output)
        self.assertIn('TX DM receipt confirmed',output)
        self.assertIn('confirmed=True',output)
        self.assertFalse(self.tx.pending_receipts)

    async def test_matched_event_includes_pending_destination_and_code(self):
        ready=asyncio.Event()
        async def send(*args,**kwargs):
            ready.set()
            return Event(EventType.MSG_SENT,{'expected_ack':b'abcd','suggested_timeout':10})
        self.mc.commands.send_msg=send
        with self.assertLogs(level='INFO') as logs:
            task=asyncio.create_task(self.tx.dm(KEY,'Pong!'))
            await ready.wait()
            await self.emit(EventType.ACK,{'code':b'abcd'.hex(),'trip_time':5})
            self.assertTrue(await task)
        self.assertTrue(any('matching_peers=[\''+KEY+'\']' in line for line in logs.output))
        self.assertFalse(self.tx.pending_receipts)

    async def test_idle_raw_ack_and_encrypted_path_are_observations_only(self):
        runner=object.__new__(Runner);runner.rx_logs=deque(maxlen=128)
        # FLOOD ACK header, one 2-byte hop, four-byte ACK plus extension.
        ack=b'\x0d\x41\xaa\xbb\x12\x34\x56\x78\x99'
        path=b'\x21\x41\xcc\xdd\x01\x02\x03\x04'
        with self.assertLogs(level='INFO') as logs:
            runner.receive_rx_log(Event(EventType.RX_LOG_DATA,{'payload':ack}),self.tx)
            runner.receive_rx_log(Event(EventType.RX_LOG_DATA,{'raw_hex':path.hex()}),self.tx)
            await self.emit(EventType.PATH_UPDATE,{'public_key':KEY})
        output='\n'.join(logs.output)
        self.assertIn("RX raw ACK packet: code='12345678' payload_hex=1234567899",output)
        self.assertIn("hops=1 hash_bytes=2 path='aabb'",output)
        self.assertIn('RX encrypted PATH packet (not delivery confirmation)',output)
        self.assertIn("RX PATH_UPDATE event: peer='"+KEY+"' pending=[]",output)
        self.assertNotIn('TX DM receipt confirmed',output)
        self.assertEqual(len(runner.rx_logs),2)

    async def test_truncated_and_nonhex_raw_ack_payloads_do_not_crash(self):
        with self.assertLogs(level='INFO') as logs:
            for payload in (b'\x12',None,'not hex'):
                self.tx.log_rx_packet({'payload_type':3,'pkt_payload':payload})
        self.assertEqual(len(logs.output),3)
        self.assertTrue(all('code=None' in line for line in logs.output))
