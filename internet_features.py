"""Helpers for infobot Internet commands. Normal data-source access remains in
core.Sources and is LAN-only. This module contains the DNS probe, the public
HTTP fetch command, and CelesTrak element refresh plus local SGP4 predictions;
ping is bounded in core.py.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from datetime import datetime, timedelta, timezone
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import struct
import time
from urllib.parse import quote, urljoin, urlsplit
import urllib.request
from zoneinfo import ZoneInfo

LOCAL = ZoneInfo('America/Detroit')
DNS_ROOTS = (
    ('a', '198.41.0.4'),
    ('b', '170.247.170.2'),
    ('c', '192.33.4.12'),
    ('d', '199.7.91.13'),
    ('f', '192.5.5.241'),
    ('h', '198.97.190.53'),
)
AMPR_LAN = ipaddress.ip_network('44.31.120.0/24')
CELESTRAK_URL = 'https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT=json'
CELESTRAK_HOSTS = {'celestrak.org', 'www.celestrak.org'}


def _query_root(name, address, timeout=1.4):
    """Ask an authoritative root server for the root NS record, without a resolver."""
    transaction = secrets.randbelow(65536)
    packet = struct.pack('!HHHHHH', transaction, 0, 1, 0, 0, 0) + b'\0' + struct.pack('!HH', 2, 1)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(packet, (address, 53))
            response, _ = sock.recvfrom(2048)
        if len(response) < 12:
            return False
        ident, flags, qdcount, _, _, _ = struct.unpack('!HHHHHH', response[:12])
        return ident == transaction and bool(flags & 0x8000) and (flags & 0x000f) == 0 and qdcount == 1
    except OSError:
        return False


def internet_status():
    """Probe several hard-coded DNS root IPv4 addresses directly over UDP/53."""
    answered = []
    with ThreadPoolExecutor(max_workers=len(DNS_ROOTS)) as pool:
        futures = {pool.submit(_query_root, name, address): name for name, address in DNS_ROOTS}
        try:
            for future in as_completed(futures, timeout=2.0):
                if future.result():
                    answered.append(futures[future])
        except FuturesTimeout:
            pass
    if answered:
        return f'Internet: reachable ({len(answered)}/{len(DNS_ROOTS)} DNS roots replied).'
    return f'Internet: unreachable (0/{len(DNS_ROOTS)} DNS roots replied).'


def _public_address(host, port):
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(item[4][0]) for item in
                         socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
        except OSError as error:
            raise ValueError(f'get: could not resolve host ({error}).') from error
    if not addresses:
        raise ValueError('get: host has no addresses.')
    # The mesh is public and the command is unauthenticated. Keep this fetcher
    # from becoming a route into the private LAN or local AMPRNet services.
    if any(not address.is_global or address in AMPR_LAN for address in addresses):
        raise ValueError('get: only public Internet endpoints are allowed.')
    return addresses[0]


def _connect_pinned(address, port, timeout):
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((str(address), port))
        return sock
    except Exception:
        sock.close()
        raise


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address = address

    def connect(self):
        self.sock = _connect_pinned(self.address, self.port, self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, address, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self.address = address

    def connect(self):
        sock = _connect_pinned(self.address, self.port, self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def _decode_text(body, content_type):
    match = re.search(r'\bcharset\s*=\s*["\']?([^;"\'\s]+)', content_type or '', re.I)
    charset = match.group(1) if match else 'utf-8'
    try:
        return body.decode(charset)
    except (LookupError, UnicodeDecodeError) as error:
        raise ValueError('get: response is not valid text in its declared charset.') from error


def fetch_one_packet(url, byte_limit):
    """Fetch a public HTTP(S) text response and return it unchanged if it fits."""
    if len(url) > 2048:
        return 'get: URL is too long.'
    current = url
    context = ssl.create_default_context()
    for redirect in range(4):
        try:
            parsed = urlsplit(current)
            scheme = parsed.scheme.lower()
            if scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                return 'Use get http:// or https:// followed by a public endpoint.'
            host = parsed.hostname
            port = parsed.port or (443 if scheme == 'https' else 80)
            address = _public_address(host, port)
            path = quote(parsed.path or '/', safe="/%:@!$&'()*+,;=-._~")
            if parsed.query:
                path += '?' + quote(parsed.query, safe="/?=&%:@!$'()*+,;~-._")
            host_header = f'[{host}]' if ':' in host else host
            if port != (443 if scheme == 'https' else 80):
                host_header += f':{port}'
            conn_type = _PinnedHTTPS if scheme == 'https' else _PinnedHTTP
            if scheme == 'https':
                conn = conn_type(host, port, address, 5, context)
            else:
                conn = conn_type(host, port, address, 5)
            try:
                conn.request('GET', path, headers={
                    'Host': host_header,
                    'User-Agent': 'infobot/0.5.2',
                    'Accept': '*/*',
                    'Accept-Encoding': 'identity',
                    'Connection': 'close',
                })
                response = conn.getresponse()
                location = response.getheader('Location')
                if response.status in (301, 302, 303, 307, 308) and location:
                    if redirect == 3:
                        return 'get: too many redirects.'
                    current = urljoin(current, location)
                    continue
                body = response.read(byte_limit + 1)
                if len(body) > byte_limit:
                    return f'get: response exceeds one packet ({byte_limit} byte limit).'
                text = _decode_text(body, response.getheader('Content-Type', ''))
                if len(text.encode('utf-8')) > byte_limit:
                    return f'get: response exceeds one packet ({byte_limit} byte limit).'
                if not text:
                    return 'get: endpoint returned an empty response.'
                return text
            finally:
                conn.close()
        except (ValueError, OSError, http.client.HTTPException, ssl.SSLError) as error:
            return str(error) if str(error).startswith('get:') else f'get: fetch failed ({error}).'
    return 'get: redirect loop.'


class _CelestrakRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme != 'https' or target.hostname not in CELESTRAK_HOSTS:
            raise ValueError('Satellite data redirect rejected; expected CelesTrak HTTPS.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SatellitePasses:
    def __init__(self, config):
        self.config = config
        self.cache_path = Path(config.state).with_name('amateur-elements.json')
        self.last_attempt = 0.0

    def _read_cache(self):
        try:
            payload = json.loads(self.cache_path.read_text())
            if isinstance(payload, dict) and isinstance(payload.get('elements'), list) and payload['elements']:
                return payload
        except (OSError, ValueError, TypeError):
            pass
        return None

    def _write_cache(self, payload):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, separators=(',', ':')))
        os.replace(temporary, self.cache_path)

    def _refresh(self, cached, now):
        if now - self.last_attempt < 1800:
            return cached
        self.last_attempt = now
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _CelestrakRedirect())
            request = urllib.request.Request(CELESTRAK_URL, headers={
                'Accept': 'application/json', 'User-Agent': 'infobot/0.5.2',
            })
            with opener.open(request, timeout=8) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError('CelesTrak amateur element response exceeded 2 MB')
            elements = json.loads(raw)
            if not isinstance(elements, list) or not elements:
                raise ValueError('CelesTrak returned no amateur satellite elements')
            payload = {'fetched_at': now, 'elements': elements}
            self._write_cache(payload)
            return payload
        except Exception as error:
            if cached:
                cached['last_refresh_error'] = str(error)[:160]
                try:
                    self._write_cache(cached)
                except OSError:
                    pass
                return cached
            return None

    def _elements(self, now):
        cached = self._read_cache()
        fetched = _number(cached.get('fetched_at')) if cached else None
        refresh_after = self.config.sat_tle_refresh_hours * 3600
        if cached is None or fetched is None or now - fetched >= refresh_after:
            cached = self._refresh(cached, now)
        if cached is None:
            return None, None, 'sat: no cached amateur elements; CelesTrak refresh failed.'
        fetched = _number(cached.get('fetched_at'))
        if fetched is None:
            return None, None, 'sat: element cache has no valid refresh time.'
        age = max(0, now - fetched)
        try:
            from skyfield.api import EarthSatellite, load
            ts = load.timescale(builtin=True)
            satellites = []
            for record in cached['elements']:
                try:
                    sat = EarthSatellite.from_omm(ts, record)
                    sat_name = str(record.get('OBJECT_NAME') or sat.name or '').strip()
                    sat_id = int(record.get('NORAD_CAT_ID') or sat.model.satnum)
                    if sat_name:
                        satellites.append((sat_name, sat_id, sat))
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
            if not satellites:
                return None, age, 'sat: no usable amateur orbital elements in cache.'
            return (ts, satellites), age, None
        except ImportError:
            return None, age, 'sat: orbit library missing; rebuild the image with Skyfield installed.'

    def report(self, args, now, location):
        loaded, age, error = self._elements(now)
        if error:
            return error
        ts, satellites = loaded
        from skyfield.api import wgs84
        observer = wgs84.latlon(location['lat'], location['lon'],
                                elevation_m=self.config.sat_alt_m)
        t_now = ts.from_datetime(datetime.fromtimestamp(now, timezone.utc))
        stale = f'Elements {age/86400:.1f}d old; predictions may be stale.\n' if age > 3*86400 else ''
        if not args:
            return stale + self._visible_next_hour(ts, satellites, observer, now)
        queries = [item.strip() for item in ' '.join(args).split(',') if item.strip()]
        if not queries:
            return 'Use sat [NORAD ID|name[,name...]].'
        by_id = {sat_id: (name, sat_id, sat) for name, sat_id, sat in satellites}
        output = []
        for query in queries:
            if query.isdecimal():
                match = by_id.get(int(query))
                if match is None:
                    output.append(f'{query}: no amateur satellite found.')
                    continue
                matches = [match]
            else:
                matches = [item for item in satellites if query.casefold() in item[0].casefold()]
                if len(matches) > 1:
                    choices = ', '.join(f'{name} ({sat_id})' for name, sat_id, _ in matches[:4])
                    output.append(f'{query}: multiple matches: {choices}.')
                    continue
                if not matches:
                    output.append(f'{query}: no amateur satellite found.')
                    continue
            name, sat_id, sat = matches[0]
            output.append(self._next_pass(name, sat_id, sat, observer, ts, t_now))
        return stale + '\n'.join(output)

    def _visible_next_hour(self, ts, satellites, observer, now):
        from skyfield.api import wgs84
        start = datetime.fromtimestamp(now, timezone.utc)
        step = 30
        samples = [start + timedelta(seconds=step*i) for i in range(121)]
        times = ts.from_datetimes(samples)
        rows = []
        for name, sat_id, sat in satellites:
            try:
                altitudes = (sat - observer).at(times).altaz()[0].degrees
            except Exception:
                continue
            current = float(altitudes[0])
            if current >= 0:
                rows.append((0, -current, f'{name} up now {current:.0f} deg'))
                continue
            for i in range(1, len(samples)):
                previous, following = float(altitudes[i-1]), float(altitudes[i])
                if previous < 0 <= following:
                    fraction = -previous / (following-previous) if following != previous else 0
                    rise = samples[i-1] + timedelta(seconds=step*fraction)
                    if rise <= start + timedelta(hours=1):
                        peak = max(float(v) for v in altitudes[i:])
                        rows.append((1, rise.timestamp(), f'{name} rises {rise.astimezone(LOCAL):%H:%M} ET (peak {peak:.0f} deg)'))
                    break
        if not rows:
            return 'No amateur satellites are up or rising in the next hour.'
        rows.sort(key=lambda row: (row[0], row[1]))
        return 'Amateur satellites up / rising within 1h:\n' + '\n'.join(row[2] for row in rows)

    @staticmethod
    def _next_pass(name, sat_id, sat, observer, ts, t_now):
        current_alt = float((sat - observer).at(t_now).altaz()[0].degrees)
        t_end = ts.from_datetime(t_now.utc_datetime().replace(tzinfo=timezone.utc) + timedelta(hours=48))
        times, events = sat.find_events(observer, t_now, t_end, altitude_degrees=0.0)
        event_data = [(time_item.utc_datetime().replace(tzinfo=timezone.utc), int(event))
                      for time_item, event in zip(times, events)]
        label = f'{name} (NORAD {sat_id})'
        if current_alt >= 0:
            culmination = next((when for when,event in event_data if event == 1), None)
            setting = next((when for when,event in event_data if event == 2), None)
            if setting is None:
                return f'{label}: up now at {current_alt:.0f} deg; no set in 48h.'
            peak = current_alt
            if culmination:
                peak = float((sat - observer).at(ts.from_datetime(culmination)).altaz()[0].degrees)
            return f'{label}: up now; sets {setting.astimezone(LOCAL):%m/%d %H:%M} ET; peak {peak:.0f} deg.'
        rise_index = next((i for i,(_,event) in enumerate(event_data) if event == 0), None)
        if rise_index is None:
            return f'{label}: no rise in the next 48h.'
        rise = event_data[rise_index][0]
        culmination = next((when for when,event in event_data[rise_index+1:] if event == 1), None)
        setting = next((when for when,event in event_data[rise_index+1:] if event == 2), None)
        peak = float((sat - observer).at(ts.from_datetime(culmination)).altaz()[0].degrees) if culmination else None
        result = f'{label}: rises {rise.astimezone(LOCAL):%m/%d %H:%M} ET'
        if peak is not None: result += f'; peak {peak:.0f} deg'
        if setting is not None: result += f'; sets {setting.astimezone(LOCAL):%H:%M} ET'
        return result + '.'


def _number(value):
    try:
        result = float(value)
        return result if result == result and abs(result) != float('inf') else None
    except (ValueError, TypeError):
        return None

