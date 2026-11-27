"""Shared radio arbitration and receipt-driven, bounded private delivery."""
import asyncio
import inspect
import logging
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
        self.pending_receipts = {}

    def receipt_expectations(self):
        return [dict(peer=item['peer'],timestamp=item['timestamp'],attempt=item['attempt'],
                     awaiting_send_result=item['awaiting_send_result'],
                     expected=sorted(item['expected'],key=repr))
                for item in self.pending_receipts.values()]

    def log_receipt(self,event):
        # This observer never acknowledges messages or changes retry state.
        # Log unmatched, early and late events even when no DM is waiting.
        code=event.attributes.get('code',event.payload.get('code'))
        matches=[item['peer'] for item in self.pending_receipts.values() if code in item['expected']]
        logging.info('RX ACK event: code=%r payload_code=%r trip_time_ms=%r matching_peers=%r pending=%r',
                     code,event.payload.get('code'),event.payload.get('trip_time'),matches,self.receipt_expectations())

    def log_path_update(self,event):
        logging.info('RX PATH_UPDATE event: peer=%r pending=%r',
                     event.payload.get('public_key'),self.receipt_expectations())

    @staticmethod
    def packet_payload(fields):
        payload=fields.get('pkt_payload',b'')
        if isinstance(payload,str):
            try: payload=bytes.fromhex(payload)
            except ValueError: payload=b''
        if not isinstance(payload,(bytes,bytearray)): payload=b''
        return bytes(payload)

    def receive_raw_ack(self,fields):
        if fields.get('payload_type')!=3: return False
        payload=self.packet_payload(fields)
        if len(payload)<4: return False
        # MeshCore correlates receipts on the first four wire-order bytes.
        # Optional extension bytes are not part of the receipt identifier.
        code=payload[:4].hex()
        matched=False
        for item in list(self.pending_receipts.values()):
            matched=item['observe_ack'](code,'raw RF ACK') or matched
        return matched

    def log_rx_packet(self,fields):
        kind=fields.get('payload_type')
        if kind not in (3,8): return  # public ACK or encrypted PATH return
        payload=self.packet_payload(fields)
        if kind==3:
            logging.info('RX raw ACK packet: code=%r payload_hex=%s route=%r hops=%r hash_bytes=%r path=%r pending=%r',
                         payload[:4].hex() if len(payload)>=4 else None,payload.hex(),
                         fields.get('route_type'),fields.get('path_len'),fields.get('path_hash_size'),
                         fields.get('path'),self.receipt_expectations())
        else:
            # PATH contents are encrypted; their presence alone cannot confirm
            # any pending message. Do not log ciphertext as a decoded receipt.
            logging.info('RX encrypted PATH packet (not delivery confirmation): route=%r hops=%r hash_bytes=%r path=%r payload_bytes=%s pending=%r',
                         fields.get('route_type'),fields.get('path_len'),fields.get('path_hash_size'),
                         fields.get('path'),len(payload),self.receipt_expectations())

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

    async def channel(self, slot, text, region=None):
        from meshcore import EventType

        async def send():
            commands = self.mc.commands
            try:
                scope = await commands.force_unscoped() if region is None else await commands.set_flood_scope(region)
                if not good(scope,EventType.OK):
                    raise ConnectionError('Radio did not accept channel reply scope')
                return await commands.send_chan_msg(slot,text)
            finally:
                # Restore the companion default before an advert or DM can send.
                if not good(await commands.set_flood_scope(None),EventType.OK):
                    raise ConnectionError('Radio did not restore default flood scope')

        async with self.channel_turn:
            result = await self.transmit(send, channel=True)
        if not good(result,EventType.OK):
            logging.warning('TX channel #%s rejected by radio: %r',slot,text)
            raise ConnectionError('Radio did not accept channel reply; not retrying')
        logging.info('TX accepted by radio on channel #%s (delivery unconfirmed): %r',slot,text)
        return True

    async def advert(self):
        from meshcore import EventType
        # Flood adverts propagate discovery beyond immediate neighbors. Use
        # the same pacing/arbitration as channel traffic, without blocking DMs.
        async with self.channel_turn:
            result = await self.transmit(lambda: self.mc.commands.send_advert(flood=True),channel=True)
        if not good(result,EventType.OK):
            raise ConnectionError('Radio did not accept flood advert')
        logging.info('TX flood advert accepted by radio (reception unconfirmed)')

    async def dm(self, key, text):
        from meshcore import EventType
        # Keep identity stable across retries. The receipt hash excludes the
        # recipient, so identical text needs distinct timestamps even across
        # different DM destinations in the same wall-clock second.
        identity = text
        timestamp = max(int(time.time()), self.timestamps.get(identity,0)+1)
        self.timestamps[identity] = timestamp
        if len(self.timestamps)>512: self.timestamps.pop(next(iter(self.timestamps)))
        acked = asyncio.Event()
        expected, early = set(), {}
        tracking=object()
        def observe_ack(code,source):
            if code in expected:
                if not acked.is_set():
                    logging.info('TX DM receipt confirmed from %s (ack=%s, source=%s): %r',key,code,source,text)
                acked.set()
                return True
            elif code: early[code]=source
            return False
        self.pending_receipts[tracking]=dict(peer=key,timestamp=timestamp,attempt=0,
                                            awaiting_send_result=True,expected=expected,observe_ack=observe_ack)
        def receipt(event):
            observe_ack(event.attributes.get('code',event.payload.get('code')),'companion receipt')
        subscription = self.mc.subscribe(EventType.ACK,receipt)
        flood = False
        try:
            for attempt in range(self.config.dm_retries+1):
                if acked.is_set(): return True
                if attempt == self.config.dm_flood_after and not flood:
                    # Reset only this destination, and only after missed receipts.
                    result = await self.mc.commands.reset_path(key)
                    if not good(result,EventType.OK): raise ConnectionError('DM flood path reset failed')
                    logging.info('TX DM to %s: flood fallback after %s unanswered attempts',key,attempt)
                    flood = True
                if acked.is_set(): return True
                self.pending_receipts[tracking].update(attempt=attempt+1,awaiting_send_result=True)
                result = await self.transmit(lambda: self.mc.commands.send_msg(key,text,timestamp=timestamp,attempt=attempt))
                if not good(result,EventType.MSG_SENT):
                    logging.warning('TX DM to %s rejected by radio (attempt %s): %r',key,attempt+1,text)
                    # Without expected_ack we cannot safely identify delivery.
                    raise ConnectionError('Radio did not accept DM reply')
                flood = result.payload.get('type') == 1
                code = result.payload['expected_ack']
                code = code.hex() if isinstance(code,bytes) else code
                expected.add(code)
                self.pending_receipts[tracking]['awaiting_send_result']=False
                logging.info('TX DM accepted by radio for %s (attempt %s/%s, route=%s, ack=%s; awaiting receipt): %r',
                             key,attempt+1,self.config.dm_retries+1,'flood' if flood else 'direct',code,text)
                if code in early and not acked.is_set():
                    observe_ack(code,early[code])
                suggested = result.payload.get('suggested_timeout',0)/1000*1.2
                timeout = self.config.dm_ack_timeout or max(5,min(300,suggested))
                logging.info('DM ACK wait: peer=%s timestamp=%s attempt=%s expected=%r suggested_timeout_ms=%r timeout=%gs',
                             key,timestamp,attempt+1,sorted(expected,key=repr),result.payload.get('suggested_timeout'),timeout)
                try:
                    await asyncio.wait_for(acked.wait(),timeout)
                    # PATH_UPDATE marks contacts dirty. Refresh before the next
                    # packet so a route learned from flood is retained/used.
                    await self.mc.ensure_contacts(follow=True)
                    return True
                except asyncio.TimeoutError:
                    logging.warning('TX DM receipt timed out for %s (attempt %s/%s, timeout=%gs): %r',
                                    key,attempt+1,self.config.dm_retries+1,timeout,text)
            return acked.is_set()
        finally:
            subscription.unsubscribe()
            self.pending_receipts.pop(tracking,None)
            logging.info('DM ACK tracking ended: peer=%s timestamp=%s confirmed=%s expected=%r',
                         key,timestamp,acked.is_set(),sorted(expected,key=repr))
