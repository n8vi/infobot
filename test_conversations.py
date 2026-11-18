import asyncio
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from bot import Runner
from core import Config


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.runner=Runner(Config(state=self.directory.name+'/s.db',parts=3))
        self.runner.queued=0
        self.dead=asyncio.Event()
        self.tasks=[]
        self.mc=SimpleNamespace(contacts={},pending_contacts={})

    async def asyncTearDown(self):
        for task in self.tasks:task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        self.runner.state.db.close()
        self.directory.cleanup()

    def start(self,key,tx):
        queue=asyncio.Queue()
        self.tasks.append(asyncio.create_task(self.runner.worker(self.mc,key,queue,self.dead,tx)))
        return queue

    def enqueue(self,queue,sender,cmd,payload=None):
        self.runner.queued+=1
        queue.put_nowait((sender,cmd,[],payload or {},time.monotonic()))

    async def test_slow_channel_preserves_own_order_without_blocking_others(self):
        gate=asyncio.Event();started=asyncio.Event();second=asyncio.Event();sent=[]
        async def execute(engine,cmd,*args):
            if cmd=='slow':started.set();await gate.wait()
            if cmd=='second':second.set()
            return cmd
        self.runner.execute=execute
        async def channel(slot,text):sent.append((slot,text));return True
        tx=SimpleNamespace(channel=channel)
        a=self.start(('channel',1),tx);b=self.start(('channel',2),tx)
        self.enqueue(a,'A','slow');self.enqueue(a,'A','second')
        await started.wait()
        self.enqueue(b,'B','fast')
        await asyncio.wait_for(b.join(),1)
        self.assertEqual(sent,[(2,'fast')]);self.assertFalse(second.is_set())
        gate.set();await asyncio.wait_for(a.join(),1)
        self.assertEqual(sent,[(2,'fast'),(1,'slow'),(1,'second')])

    async def test_dm_failure_pauses_and_more_resumes_failed_packet(self):
        sends=[];fail=True
        async def execute(*args):return '\n'.join(f'Line {i}: '+('text '*16) for i in range(12))
        self.runner.execute=execute
        async def dm(key,text):
            nonlocal fail
            sends.append(text)
            if len(sends)==2 and fail:fail=False;return False
            return True
        queue=self.start(('dm','ab'*32),SimpleNamespace(dm=dm))
        self.enqueue(queue,'private-user','help');await asyncio.wait_for(queue.join(),1)
        batch=list(self.runner.state.pages['private-user'][2])
        self.assertEqual(sends,batch[:2])
        self.enqueue(queue,'private-user','more');await asyncio.wait_for(queue.join(),1)
        self.assertEqual(sends[2:],batch[1:])
        self.assertNotIn('private-user',self.runner.pending_pages)
        self.enqueue(queue,'private-user','more');await asyncio.wait_for(queue.join(),1)
        self.assertTrue(len(sends)>4)

    def test_dm_prefix_collision_is_rejected(self):
        prefix='ab'*6
        contacts={prefix+'11'*26:{},prefix+'22'*26:{}}
        self.assertIsNone(self.runner.dm_contact({'pubkey_prefix':prefix},contacts))
        del contacts[prefix+'22'*26]
        self.assertEqual(self.runner.dm_contact({'pubkey_prefix':prefix},contacts),prefix+'11'*26)

    async def test_dm_location_refreshes_contacts_before_execution(self):
        from core import request_location
        from test_dm_location import DMLocationTests, SENDER
        contacts=DMLocationTests().contacts()
        async def refresh(**kwargs):self.mc.contacts.update(contacts)
        self.mc.ensure_contacts=AsyncMock(side_effect=refresh)
        sent=[]
        async def execute(engine,cmd,args,payload,current):
            location,error=request_location(payload,current)
            self.assertIsNone(error)
            return location['county']
        async def dm(key,text):sent.append(text);return True
        self.runner.execute=execute
        queue=self.start(('dm',SENDER),SimpleNamespace(dm=dm))
        self.enqueue(queue,'private-user','wx',{'_dm_sender':SENDER})
        await asyncio.wait_for(queue.join(),1)
        self.assertEqual(sent,['lapeer'])
        self.mc.ensure_contacts.assert_awaited_once_with(follow=True)
