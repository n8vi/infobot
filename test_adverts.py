import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bot import Runner
from core import Config
from delivery import Transmitter
from meshcore.events import Event, EventType


class AdvertTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.runner=Runner(Config(state=self.directory.name+'/s.db'))

    async def asyncTearDown(self):
        self.runner.state.db.close()
        self.directory.cleanup()

    async def test_startup_four_hours_and_reconnect_keep_schedule(self):
        first=SimpleNamespace(advert=AsyncMock())
        reconnected=SimpleNamespace(advert=AsyncMock())
        clock=100.0
        with patch('bot.time',SimpleNamespace(monotonic=lambda:clock)):
            await self.runner.advertise_if_due(first)
            first.advert.assert_awaited_once()
            self.assertEqual(self.runner.next_advert,14500)
            clock=14499
            await self.runner.advertise_if_due(reconnected)
            reconnected.advert.assert_not_awaited()
            clock=14500
            await self.runner.advertise_if_due(reconnected)
            reconnected.advert.assert_awaited_once()
            self.assertEqual(self.runner.next_advert,28900)

    async def test_disabled_and_shutdown_do_not_transmit(self):
        transmitter=SimpleNamespace(advert=AsyncMock())
        self.runner.config.advert_interval=0
        await self.runner.advertise_if_due(transmitter)
        self.runner.config.advert_interval=14400
        self.runner.stop.set()
        await self.runner.advertise_if_due(transmitter)
        transmitter.advert.assert_not_awaited()

    async def test_failure_retries_after_sixty_seconds(self):
        transmitter=SimpleNamespace(advert=AsyncMock(side_effect=[ConnectionError('busy'),None]))
        clock=100.0
        with patch('bot.time',SimpleNamespace(monotonic=lambda:clock)):
            with self.assertLogs(level='WARNING'):
                await self.runner.advertise_if_due(transmitter)
            self.assertEqual(self.runner.next_advert,160)
            clock=159
            await self.runner.advertise_if_due(transmitter)
            self.assertEqual(transmitter.advert.await_count,1)
            clock=160
            await self.runner.advertise_if_due(transmitter)
            self.assertEqual(transmitter.advert.await_count,2)
            self.assertEqual(self.runner.next_advert,14560)

    async def test_transmitter_sends_flood_and_rejects_radio_error(self):
        commands=SimpleNamespace(send_advert=AsyncMock(return_value=Event(EventType.OK,{})))
        tx=Transmitter(SimpleNamespace(commands=commands),Config(gap=0))
        with self.assertLogs(level='INFO') as logs:
            await tx.advert()
        commands.send_advert.assert_awaited_once_with(flood=True)
        self.assertTrue(any('flood advert accepted' in line for line in logs.output))
        commands.send_advert.return_value=Event(EventType.ERROR,{})
        with self.assertRaises(ConnectionError):await tx.advert()

    def test_environment_defaults_and_validation(self):
        with patch.dict(os.environ,{},clear=True):self.assertEqual(Config.env().advert_interval,14400)
        for value,expected in [('0',0),('7200',7200)]:
            with patch.dict(os.environ,{'ADVERT_INTERVAL':value},clear=True):
                self.assertEqual(Config.env().advert_interval,expected)
        for value in ('-1','59','nan','inf'):
            with patch.dict(os.environ,{'ADVERT_INTERVAL':value},clear=True):
                with self.assertRaises(ValueError):Config.env()
