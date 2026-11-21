import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from core import Config
from delivery import Transmitter, SerializedCommands
from meshcore.events import Event, EventDispatcher, EventType

KEY='ab'*32


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def radio(self, config=None):
        dispatcher=EventDispatcher()
        await dispatcher.start()
        self.addAsyncCleanup(dispatcher.stop)
        commands=SimpleNamespace(reset_path=AsyncMock(return_value=Event(EventType.OK,{})))
        mc=SimpleNamespace(commands=commands,subscribe=dispatcher.subscribe,
                           ensure_contacts=AsyncMock())
        return mc,dispatcher,Transmitter(mc,config or Config(dm_ack_timeout=.01))

    async def ack(self, dispatcher, code):
        await dispatcher.dispatch(Event(EventType.ACK,{'code':code},{'code':code}))
        await dispatcher.queue.join()

    async def test_early_receipt_and_no_fixed_delay(self):
        mc,d,tx=await self.radio()
        sends=[]
        async def send(key,text,timestamp,attempt):
            code=(len(sends)+1).to_bytes(4,'little')
            sends.append((timestamp,attempt))
            await self.ack(d,code.hex())  # receipt before send_msg returns
            return Event(EventType.MSG_SENT,{'expected_ack':code,'suggested_timeout':99999})
        mc.commands.send_msg=send
        start=time.monotonic()
        self.assertTrue(await tx.dm(KEY,'page one'))
        self.assertTrue(await tx.dm(KEY,'page two'))
        self.assertLess(time.monotonic()-start,.5)
        self.assertEqual([a for t,a in sends],[0,0])
        self.assertEqual(mc.ensure_contacts.await_count,2)

    async def test_retry_identity_late_ack_and_flood_reset(self):
        mc,d,tx=await self.radio(Config(dm_ack_timeout=.01,dm_retries=3,dm_flood_after=2))
        sends=[]
        async def send(key,text,timestamp,attempt):
            code=bytes([attempt+1])*4
            sends.append((key,text,timestamp,attempt))
            if attempt==2:
                # A delayed ACK from attempt zero still acknowledges this page.
                await self.ack(d,'01010101')
            return Event(EventType.MSG_SENT,{'expected_ack':code,'suggested_timeout':10,'type':int(attempt>=2)})
        mc.commands.send_msg=send
        self.assertTrue(await tx.dm(KEY,'same page'))
        self.assertEqual([x[3] for x in sends],[0,1,2])
        self.assertEqual(len({x[2] for x in sends}),1)
        self.assertEqual({x[1] for x in sends},{'same page'})
        mc.commands.reset_path.assert_awaited_once_with(KEY)
        mc.ensure_contacts.assert_awaited_once_with(follow=True)

    async def test_unrelated_ack_exhausts_exact_retry_budget(self):
        mc,d,tx=await self.radio(Config(dm_ack_timeout=.001,dm_retries=1,dm_flood_after=2))
        async def send(*args,**kwargs):
            await self.ack(d,'ffffffff')
            return Event(EventType.MSG_SENT,{'expected_ack':b'abcd','suggested_timeout':1})
        mc.commands.send_msg=AsyncMock(side_effect=send)
        self.assertFalse(await tx.dm(KEY,'page'))
        self.assertEqual(mc.commands.send_msg.await_count,2)
        mc.commands.reset_path.assert_not_awaited()

    async def test_waiting_for_dm_does_not_block_other_destinations(self):
        mc,d,tx=await self.radio(Config(dm_ack_timeout=1,gap=.01))
        waiting=asyncio.Event()
        async def send(*args,**kwargs):
            waiting.set()
            return Event(EventType.MSG_SENT,{'expected_ack':b'abcd','suggested_timeout':1})
        mc.commands.send_msg=send
        mc.commands.send_chan_msg=AsyncMock(return_value=Event(EventType.OK,{}))
        task=asyncio.create_task(tx.dm(KEY,'page'))
        await waiting.wait()
        self.assertTrue(await asyncio.wait_for(tx.channel(1,'channel reply'),.1))
        self.assertFalse(task.done())
        await self.ack(d,b'abcd'.hex())
        self.assertTrue(await task)

    async def test_channel_gap_does_not_hold_transmitter_lock(self):
        mc,d,tx=await self.radio(Config(dm_ack_timeout=.1,gap=.15))
        mc.commands.send_chan_msg=AsyncMock(return_value=Event(EventType.OK,{}))
        async def send(*args,**kwargs):
            await self.ack(d,b'abcd'.hex())
            return Event(EventType.MSG_SENT,{'expected_ack':b'abcd','suggested_timeout':1})
        mc.commands.send_msg=send
        await tx.channel(1,'first')
        second=asyncio.create_task(tx.channel(1,'second'))
        self.assertTrue(await asyncio.wait_for(tx.dm(KEY,'private'),.1))
        self.assertFalse(second.done())
        await second

    async def test_previous_message_receipt_cannot_ack_next_message(self):
        mc,d,tx=await self.radio(Config(dm_ack_timeout=.001,dm_retries=0))
        async def send(key,text,**kwargs):
            await self.ack(d,'01010101')
            return Event(EventType.MSG_SENT,{'expected_ack':bytes([1 if text=='first' else 2])*4,'suggested_timeout':1})
        mc.commands.send_msg=send
        self.assertTrue(await tx.dm(KEY,'first'))
        self.assertFalse(await tx.dm(KEY,'second'))

    async def test_already_flood_route_is_not_reset_again(self):
        mc,d,tx=await self.radio(Config(dm_ack_timeout=.001,dm_retries=3,dm_flood_after=2))
        mc.commands.send_msg=AsyncMock(return_value=Event(EventType.MSG_SENT,{
            'expected_ack':b'abcd','suggested_timeout':1,'type':1}))
        self.assertFalse(await tx.dm(KEY,'page'))
        self.assertEqual(mc.commands.send_msg.await_count,4)
        mc.commands.reset_path.assert_not_awaited()

    async def test_cancelled_wait_removes_receipt_subscription(self):
        mc,d,tx=await self.radio(Config(dm_ack_timeout=10))
        sent=asyncio.Event()
        async def send(*args,**kwargs):
            sent.set()
            return Event(EventType.MSG_SENT,{'expected_ack':b'abcd','suggested_timeout':1})
        mc.commands.send_msg=send
        task=asyncio.create_task(tx.dm(KEY,'page'))
        await sent.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertFalse(any(s.event_type==EventType.ACK for s in d.subscriptions))

    async def test_command_pairs_are_serialized(self):
        entered=[]
        async def command(value):
            entered.append(value)
            await asyncio.sleep(.01)
            self.assertEqual(entered[-1],value)
            return value
        proxy=SerializedCommands(SimpleNamespace(command=command))
        self.assertEqual(await asyncio.gather(proxy.command(1),proxy.command(2)),[1,2])
