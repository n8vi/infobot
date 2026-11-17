"""Shared radio arbitration and receipt-driven, bounded private delivery."""
import asyncio
import inspect
import time


def good(event, expected):
    return event is not None and event.type == expected


class SerializedCommands:
    """Serialize companion request/response pairs, including message fetching.

    MeshCore command responses have no transaction IDs. Concurrent commands
    must not both consume the same OK/MSG_SENT/ERROR event.
    """
    def __init__(self, commands):
        self.commands = commands
        self.lock = asyncio.Lock()

    def __getattr__(self, name):
        method = getattr(self.commands, name)
        if not inspect.iscoroutinefunction(method): return method
        async def call(*args, **kwargs):
            async with self.lock:
                return await method(*args, **kwargs)
        return call


class Transmitter:
    def __init__(self, mc, config):
        self.mc, self.config = mc, config
        self.lock = asyncio.Lock()
        self.channel_turn = asyncio.Lock()
        self.last_tx = 0
        self.timestamps = {}

    async def transmit(self, action, channel=False):
        # Sleep outside the lock: a channel's cooldown never blocks a ready DM.
        while True:
            async with self.lock:
                wait = self.config.gap - (time.monotonic()-self.last_tx) if channel else 0
                if wait <= 0:
                    result = await action()
                    if channel: self.last_tx = time.monotonic()
                    return result
            await asyncio.sleep(wait)

    async def channel(self, slot, text):
        from meshcore import EventType
        async with self.channel_turn:
            result = await self.transmit(lambda: self.mc.commands.send_chan_msg(slot,text), channel=True)
        if not good(result,EventType.OK):
            raise ConnectionError('Radio did not accept channel reply; not retrying')
        return True

    async def dm(self, key, text):
        from meshcore import EventType
        # Keep a message identity stable across retries, and distinguish even
        # identical consecutive pages within the same wall-clock second.
        identity = (key,text)
        timestamp = max(int(time.time()), self.timestamps.get(identity,0)+1)
        self.timestamps[identity] = timestamp
        if len(self.timestamps)>512: self.timestamps.pop(next(iter(self.timestamps)))
        acked = asyncio.Event()
        expected, early = set(), set()
        def receipt(event):
            code = event.attributes.get('code',event.payload.get('code'))
            if code in expected: acked.set()
            elif code: early.add(code)
        subscription = self.mc.subscribe(EventType.ACK,receipt)
        flood = False
        try:
            for attempt in range(self.config.dm_retries+1):
                if acked.is_set(): return True
                if attempt == self.config.dm_flood_after and not flood:
                    # Reset only this destination, and only after missed receipts.
                    result = await self.mc.commands.reset_path(key)
                    if not good(result,EventType.OK): raise ConnectionError('DM flood path reset failed')
                    flood = True
                if acked.is_set(): return True
                result = await self.transmit(lambda: self.mc.commands.send_msg(key,text,timestamp=timestamp,attempt=attempt))
                if not good(result,EventType.MSG_SENT):
                    # Without expected_ack we cannot safely identify delivery.
                    raise ConnectionError('Radio did not accept DM reply')
                flood = result.payload.get('type') == 1
                code = result.payload['expected_ack']
                code = code.hex() if isinstance(code,bytes) else code
                expected.add(code)
                if code in early: acked.set()
                suggested = result.payload.get('suggested_timeout',0)/1000*1.2
                timeout = self.config.dm_ack_timeout or max(5,min(300,suggested))
                try:
                    await asyncio.wait_for(acked.wait(),timeout)
                    # PATH_UPDATE marks contacts dirty. Refresh before the next
                    # packet so a route learned from flood is retained/used.
                    await self.mc.ensure_contacts(follow=True)
                    return True
                except asyncio.TimeoutError:
                    pass
            return acked.is_set()
        finally:
            subscription.unsubscribe()
