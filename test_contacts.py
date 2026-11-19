import copy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from bot import Runner, ContactRefreshError, contact_snapshot
from core import Config, request_location
from meshcore.events import Event, EventType
import test_dm_location as dm_fixture
SENDER=dm_fixture.SENDER

KEY='ab'*32


class ContactTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.runner=Runner(Config(state=self.directory.name+'/s.db'))
        self.contacts=dm_fixture.DMLocationTests().contacts()
        self.record=dict(self.contacts.pop(SENDER),public_key=KEY)
        self.stale=dict(self.record,out_path_len=-1,out_path_hash_mode=-1,out_path='')
        self.mc=SimpleNamespace(contacts={**self.contacts,KEY:copy.deepcopy(self.stale)},
            pending_contacts={KEY.upper():copy.deepcopy(self.stale)},
            commands=SimpleNamespace(get_contact_by_key=AsyncMock(return_value=Event(EventType.NEXT_CONTACT,self.record))))

    async def asyncTearDown(self):
        self.runner.state.db.close()
        self.directory.cleanup()

    def test_confirmed_beats_pending_including_case_and_snapshot_is_frozen(self):
        self.mc.contacts[KEY]=copy.deepcopy(self.record)
        snapshot=contact_snapshot(self.mc)
        self.assertEqual(snapshot[KEY]['out_path'],'aabbccdd')
        self.mc.contacts[KEY]['out_path']='changed'
        self.assertEqual(snapshot[KEY]['out_path'],'aabbccdd')
        other='ef'*32
        self.mc.pending_contacts[other]={'public_key':other}
        self.assertIn(other,contact_snapshot(self.mc))

    async def test_direct_lookup_replaces_cache_removes_pending_and_logs_route(self):
        with self.assertLogs(level='INFO') as logs:
            snapshot=await self.runner.current_dm_contacts(self.mc,KEY)
        self.mc.commands.get_contact_by_key.assert_awaited_once_with(bytes.fromhex(KEY))
        self.assertNotIn(KEY.upper(),self.mc.pending_contacts)
        location,error=request_location({'_dm_sender':KEY},snapshot)
        self.assertIsNone(error)
        self.assertEqual(location['county'],'lapeer')
        self.assertEqual(self.mc.contacts[KEY]['out_path'],'aabbccdd')
        self.assertTrue(any("hops=2 hash_mode=1 path='aabbccdd'" in line for line in logs.output))

    async def test_route_changes_on_next_fetch_without_mutating_previous_request(self):
        first=await self.runner.current_dm_contacts(self.mc,KEY)
        changed=dict(self.record,out_path_len=1,out_path='aabb')
        self.mc.commands.get_contact_by_key.return_value=Event(EventType.NEXT_CONTACT,changed)
        self.mc.pending_contacts[KEY]=copy.deepcopy(self.stale)  # late old advertisement
        second=await self.runner.current_dm_contacts(self.mc,KEY)
        self.assertEqual(first[KEY]['out_path'],'aabbccdd')
        self.assertEqual(second[KEY]['out_path'],'aabb')
        self.assertEqual(request_location({'_dm_sender':KEY},second)[0]['county'],'huron')
        self.assertEqual(request_location({'_dm_sender':KEY},first)[0]['county'],'lapeer')
        self.mc.contacts[KEY]['out_path']='mutated again'
        self.assertEqual(second[KEY]['out_path'],'aabb')

    async def test_fresh_unknown_route_replaces_old_usable_route(self):
        self.mc.contacts[KEY]=copy.deepcopy(self.record)
        self.mc.commands.get_contact_by_key.return_value=Event(EventType.NEXT_CONTACT,self.stale)
        snapshot=await self.runner.current_dm_contacts(self.mc,KEY)
        self.assertEqual(snapshot[KEY]['out_path_len'],-1)
        self.assertIsNone(request_location({'_dm_sender':KEY},snapshot)[0])

    async def test_lookup_failure_does_not_mutate_caches_or_use_stale_record(self):
        before=copy.deepcopy((self.mc.contacts,self.mc.pending_contacts))
        for result in (None,Event(EventType.ERROR,{'reason':'not found'}),Event(EventType.OK,{})):
            self.mc.commands.get_contact_by_key.return_value=result
            with self.assertRaises(ContactRefreshError):
                await self.runner.current_dm_contacts(self.mc,KEY)
            self.assertEqual((self.mc.contacts,self.mc.pending_contacts),before)
        self.mc.commands.get_contact_by_key.side_effect=TimeoutError('timed out')
        with self.assertRaises(ContactRefreshError):await self.runner.current_dm_contacts(self.mc,KEY)
        self.assertEqual((self.mc.contacts,self.mc.pending_contacts),before)

    async def test_wrong_identity_is_rejected_without_clearing_pending(self):
        self.mc.commands.get_contact_by_key.return_value=Event(EventType.NEXT_CONTACT,dict(self.record,public_key='ff'*32))
        with self.assertRaises(ContactRefreshError):await self.runner.current_dm_contacts(self.mc,KEY)
        self.assertIn(KEY.upper(),self.mc.pending_contacts)
        self.assertEqual(self.mc.contacts[KEY]['out_path_len'],-1)

    async def test_new_radio_connection_overrides_previous_cached_record(self):
        await self.runner.current_dm_contacts(self.mc,KEY)
        other=SimpleNamespace(contacts={},pending_contacts={},commands=SimpleNamespace(
            get_contact_by_key=AsyncMock(return_value=Event(EventType.NEXT_CONTACT,dict(self.record,out_path_len=1,out_path='aabb')))))
        snapshot=await self.runner.current_dm_contacts(other,KEY)
        self.assertEqual(snapshot[KEY]['out_path'],'aabb')
        other.commands.get_contact_by_key.assert_awaited_once()
