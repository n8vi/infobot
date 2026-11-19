import unittest
from bot import Runner
from core import Config

NOW=1795047116
KEY='ab'*32


class DMAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.runner=object.__new__(Runner)
        self.runner.config=Config()
        self.payload=dict(pubkey_prefix=KEY[:12],txt_type=0,sender_timestamp=NOW,text='ping',path_len=255,path_hash_mode=-1)
        self.contacts={KEY:{}}

    def test_event_logged_before_acceptance_including_clock_and_text(self):
        with self.assertLogs(level='INFO') as logs:
            accepted,reason=self.runner.inspect_dm_admission(dict(self.payload,text='wx lapeer'),self.contacts,NOW)
        self.assertEqual(accepted,(KEY,('wx',['lapeer'])))
        self.assertIsNone(reason)
        self.assertEqual(len(logs.output),1)
        for value in ('RX DM event before admission',"sender_prefix='abababababab'",'txt_type=0',
                      'sender_timestamp=1795047116','host_timestamp=1795047116.000',
                      'age_s=0.0',"text='wx lapeer'",'path_len=255'):
            self.assertIn(value,logs.output[0])

    def test_all_admission_rejections_log_input_before_specific_reason(self):
        cases=[
            (None,self.contacts,'invalid event payload'),
            (dict(self.payload,txt_type=2),self.contacts,'unsupported DM message type'),
            (dict(self.payload,sender_timestamp=None),self.contacts,'missing or invalid sender timestamp'),
            (dict(self.payload,sender_timestamp='nan'),self.contacts,'missing or invalid sender timestamp'),
            (dict(self.payload,sender_timestamp=NOW+31),self.contacts,'31.0s in the future'),
            (dict(self.payload,sender_timestamp=NOW-121),self.contacts,'121.0s old'),
            (dict(self.payload,text=None),self.contacts,'missing or invalid text'),
            (dict(self.payload,text='🌞'*129),self.contacts,'512-byte limit'),
            (dict(self.payload,pubkey_prefix='bad'),self.contacts,'invalid sender public-key prefix'),
            (self.payload,{},'sender is unknown'),
            (self.payload,{KEY:{},KEY[:12]+'cd'*26:{}},'ambiguous (2 matching contacts)'),
            (dict(self.payload,text='ordinary chat'),self.contacts,'not a supported command'),
        ]
        for payload,contacts,expected in cases:
            with self.subTest(expected=expected),self.assertLogs(level='INFO') as logs:
                accepted,reason=self.runner.inspect_dm_admission(payload,contacts,NOW)
            self.assertIsNone(accepted)
            self.assertIn(expected,reason)
            self.assertEqual(len(logs.output),2)
            self.assertIn('RX DM event before admission',logs.output[0])
            self.assertIn('DROP DM event: '+reason,logs.output[1])

    def test_disabled_dms_still_log_received_event(self):
        self.runner.config.dm_enabled=False
        with self.assertLogs(level='INFO') as logs:
            accepted,reason=self.runner.inspect_dm_admission(self.payload,self.contacts,NOW)
        self.assertIsNone(accepted)
        self.assertEqual(reason,'DMs disabled by configuration')
        self.assertIn('RX DM event before admission',logs.output[0])
        self.assertIn('DMs disabled',logs.output[1])

    def test_existing_time_boundaries_and_missing_type_default_are_preserved(self):
        for timestamp in (NOW-120,NOW+30):
            payload=dict(self.payload,sender_timestamp=timestamp)
            payload.pop('txt_type')
            with self.assertLogs(level='INFO'):
                accepted,reason=self.runner.inspect_dm_admission(payload,self.contacts,NOW)
            self.assertEqual(accepted,(KEY,('ping',[])))
            self.assertIsNone(reason)
