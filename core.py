"""Infobot command logic; network access is confined to documented commands."""
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import socket
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo
from internet_features import SatellitePasses, fetch_one_packet, internet_status, web_answer, _PinnedHTTP, _PinnedHTTPS
import ssl

VERSION = '0.6.0'
LOCAL = ZoneInfo('America/Detroit')
PINNEBOG_COORDINATES = (43.935292, -83.103839)
with open(os.path.join(os.path.dirname(__file__), 'dtx_counties.json'), encoding='utf-8') as _county_file:
    COUNTY_DATA = json.load(_county_file)['counties']
DTX_COUNTIES = {slug: (entry['name'], *entry['center']) for slug, entry in COUNTY_DATA.items()}
CIRCLED_HOP_NUMBERS = '❶❷❸❹❺❻❼❽❾❿⓫⓬⓭⓮⓯⓰⓱⓲⓳⓴㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚㉛㉜㉝㉞㉟㊱㊲㊳㊴㊵㊶㊷㊸㊹㊺㊻㊼㊽㊾㊿'
ALIASES = {'planes': 'adsb', 'boats': 'ais', 'spacewx': 'aurora', 'decode': 'path', 'route': 'path', 'p': 'path',
           'region': 'hashsize'}
COMMANDS = {'help', 'about', 'ping', 'path', 'wx', 'aurora', 'adsb', 'ais', 'status', 'more', 'repeat', 'hashsize', 'internet', 'get', 'sat'}
HELP = ('Commands: ping [host]; path [hex]; wx [county/ZIP]; aurora/spacewx; '
        'adsb/planes; ais/boats; hashsize/region; internet; get <URL>; sat [ID|name[,name...]]; '
        'bot, <question>; status; more; repeat <page>; about. Prefix ! optional. help <command> for details.')
HELP_DETAIL = {
 'wx': 'wx <Michigan county> or wx <5-digit US ZIP> finds local GOES forecasts. Bare wx uses your estimated location in the Detroit/Pontiac area or within 50 mi.',
 'path': 'path: repeaters on this message route. path aabb,ccdd: decode supplied hashes. Bare hex defaults to 2-byte hops. Unknown/colliding hashes are marked.',
 'hashsize': 'hashsize/region: path hash bytes per hop and the flood region tag on this command packet. Region names are matched against configured local hashtag regions.',
 'aurora': 'aurora/spacewx: GOES Forecast Discussion (DAYDIS), with forecast text for solar activity, particles, solar wind and geospace.',
 'adsb': 'adsb <registration|ICAO address|flight ID> searches the local feed. Bare adsb lists nearby aircraft.',
 'ais': 'ais <MMSI|IMO|vessel name|radio call sign> searches the local feed. Bare ais lists nearby vessels.',
 'ping': 'ping: no argument replies Pong! ping <IPv4|IPv6|hostname> sends 3 ICMP probes and reports loss/latency.', 'status': 'status: uptime and last data-source request results; GOES discussion freshness.',
 'about': 'about: version and operating notes for infobot.',
 'more': 'more: next page of your last long reply. Expires after 10 min. Channel names are not authenticated identities.',
 'repeat': 'repeat <page>: resend a page from the last reply without advancing the more cursor.',
 'internet': 'internet: test outbound DNS reachability by querying several DNS root servers directly.',
 'bot': 'bot, <question>: search the public web and return a short answer. The query goes to public search engines.',
 'get': 'get <http(s) URL>: fetch public text and return it unchanged only if it fits in one MeshCore packet.',
 'sat': 'sat: amateur satellites up or rising within an hour for hop 1; DMs use the learned sender-side route; direct/pathless channel requests use Pinnebog. sat <NORAD ID|name[,name...]>: next pass.',
}

@dataclass
class Config:
    goes: str = 'http://10.0.0.10:8097'
    ais: str = 'http://10.0.0.10:8100/api/ships_full.json'
    adsb: str = 'http://hinton/tar1090/data/aircraft.json'
    max_aircraft_age: int = 60
    max_ship_age: int = 1800
    region_names: tuple = ('mi',)
    byte_limit: int = 130
    parts: int = 3
    cooldown: float = 15
    gap: float = 6
    state: str = '/state/infobot.sqlite3'
    sat_alt_m: float = 200
    sat_tle_refresh_hours: float = 12
    searxng: str = "http://searxng:8080"
    llama: str = "http://10.0.0.10:8080"
    channels: tuple = (('#infobot', 'bare'),)
    dm_enabled: bool = True
    dm_ack_timeout: float = 0  # 0: companion suggestion, with a 5-second floor
    dm_retries: int = 3
    dm_flood_after: int = 2
    command_timeout: float = 45
    conversation_limit: int = 32
    queue_limit: int = 8
    global_queue_limit: int = 64
    command_workers: int = 4
    advert_interval: float = 14400

    @classmethod
    def env(cls):
        c = cls()
        for name, key in [('goes','GOES_URL'), ('ais','AIS_URL'), ('adsb','ADSB_URL'), ('state','STATE_DB')]:
            setattr(c, name, os.getenv(key, getattr(c, name)).rstrip('/') if name == 'goes' else os.getenv(key, getattr(c, name)))
        c.max_aircraft_age = int(os.getenv('AIRCRAFT_MAX_AGE', '60'))
        c.max_ship_age = int(os.getenv('SHIP_MAX_AGE', '1800'))
        c.region_names = tuple(x.strip().lstrip('#') for x in os.getenv('REGION_NAMES', 'mi').split(',') if x.strip())
        c.byte_limit = int(os.getenv('REPLY_BYTES', '130'))
        c.parts = int(os.getenv('REPLY_PARTS', '3'))
        c.cooldown = float(os.getenv('USER_COOLDOWN', '15'))
        c.gap = float(os.getenv('TX_GAP', '6'))
        c.sat_alt_m = float(os.getenv('SAT_ALT_M', '200'))
        c.sat_tle_refresh_hours = float(os.getenv('SAT_TLE_REFRESH_HOURS', '12'))
        c.searxng = os.getenv('SEARXNG_URL', c.searxng)
        c.llama = os.getenv('LLAMA_URL', c.llama)
        raw_channels = os.getenv('BOT_CHANNELS', '#infobot=bare')
        channels = []
        for entry in raw_channels.split(','):
            name, sep, policy = entry.strip().partition('=')
            if not sep or not re.fullmatch(r'#[^\s,=]{1,30}',name) or len(name.encode()) > 31 or policy not in ('bare', 'bang', 'mention', 'either'):
                raise ValueError('BOT_CHANNELS requires #name=bare|bang|mention|either entries')
            if name in dict(channels):
                raise ValueError('Duplicate BOT_CHANNELS name')
            channels.append((name, policy))
        c.channels = tuple(channels)
        enabled = os.getenv('DM_ENABLED', 'true').lower()
        if enabled not in ('true', 'false', '1', '0'):
            raise ValueError('DM_ENABLED must be true or false')
        c.dm_enabled = enabled in ('true', '1')
        for attr, key in [('dm_ack_timeout','DM_ACK_TIMEOUT'), ('command_timeout','COMMAND_TIMEOUT'),
                          ('advert_interval','ADVERT_INTERVAL')]:
            setattr(c, attr, float(os.getenv(key, str(getattr(c, attr)))))
        for attr, key in [('dm_retries','DM_RETRIES'), ('dm_flood_after','DM_FLOOD_AFTER'),
                          ('conversation_limit','CONVERSATION_LIMIT'), ('queue_limit','QUEUE_LIMIT'),
                          ('global_queue_limit','GLOBAL_QUEUE_LIMIT'), ('command_workers','COMMAND_WORKERS')]:
            setattr(c, attr, int(os.getenv(key, str(getattr(c, attr)))))
        if any(not math.isfinite(v) for v in (c.gap,c.cooldown,c.sat_alt_m,c.sat_tle_refresh_hours,c.dm_ack_timeout,c.command_timeout,c.advert_interval)):
            raise ValueError('Numeric settings must be finite')
        if c.advert_interval != 0 and c.advert_interval < 60:
            raise ValueError('ADVERT_INTERVAL must be 0 (disabled) or at least 60 seconds')
        if not 0 <= c.dm_retries <= 3 or not 1 <= c.dm_flood_after <= 4 or not 0 <= c.dm_ack_timeout <= 300:
            raise ValueError('DM_RETRIES 0..3, DM_FLOOD_AFTER 1..4, DM_ACK_TIMEOUT 0..300 required')
        if not 1 <= c.command_timeout <= 300 or not 1 <= c.command_workers <= 16:
            raise ValueError('COMMAND_TIMEOUT 1..300 and COMMAND_WORKERS 1..16 required')
        if not 1 <= c.conversation_limit <= 128 or not 1 <= c.queue_limit <= 64 or not 1 <= c.global_queue_limit <= 512:
            raise ValueError('Conversation/queue limits outside supported ranges')
        if not 80 <= c.byte_limit <= 130 or not 1 <= c.parts <= 5 or c.gap < 3 or c.cooldown < 5:
            raise ValueError('REPLY_BYTES 80..130, REPLY_PARTS 1..5, TX_GAP >=3, USER_COOLDOWN >=5 required')
        if c.max_aircraft_age <= 0 or c.max_ship_age <= 0:
            raise ValueError('Source age limits must be positive')
        if c.sat_alt_m < -500 or c.sat_tle_refresh_hours < 1:
            raise ValueError('SAT_ALT_M >= -500 and SAT_TLE_REFRESH_HOURS >= 1 required')
        return c


def clean(value):
    return ' '.join(re.sub(r'[\x00-\x1f\x7f]', ' ', str(value)).split())


def number(value, default=None):
    try:
        n = float(value)
        return n if math.isfinite(n) else default
    except (ValueError, TypeError): return default


def parse_command(text, policy='bare'):
    text = clean(text)
    mentioned = text.lower().startswith('@[infobot]') and (len(text) == 10 or text[10] in ' :')
    if mentioned:
        text = text[10:].lstrip(' :')
    bang = text.startswith('!')
    if policy == 'mention' and not mentioned: return None
    if policy == 'bang' and not bang: return None
    if policy == 'either' and not (bang or mentioned): return None
    if text.lower().startswith('infobot:'): text = text[8:].strip()
    if text.startswith('!'): text = text[1:].lstrip()
    if not text: return None
    if text[:4].lower() == 'bot,':
        question = text[4:].strip()
        return 'bot', [question] if question else []
    words = text.split()
    cmd = ALIASES.get(words[0].lower(), words[0].lower())
    return (cmd, words[1:]) if cmd in COMMANDS else None


def local_addresses(host, port):
    permitted = [ipaddress.ip_network(x) for x in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','127.0.0.0/8','44.31.120.0/24','::1/128','fc00::/7')]
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos or any(not any(ipaddress.ip_address(i[4][0]) in n for n in permitted) for i in infos):
        raise ValueError('Only LAN/loopback data-source addresses are allowed')
    return infos


class Sources:
    def __init__(self):
        self.health = {}
        self.cache = {}

    def get(self, label, url, ttl=15):
        now = time.monotonic()
        if url in self.cache and now-self.cache[url][0] < ttl:
            return self.cache[url][1]
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('Expected a credential-free LAN HTTP URL')
            port = parsed.port or (443 if parsed.scheme=='https' else 80)
            infos = local_addresses(parsed.hostname, port)
            address = ipaddress.ip_address(infos[0][4][0])
            conn = (_PinnedHTTPS(parsed.hostname, port, address, 5, ssl.create_default_context())
                    if parsed.scheme == 'https' else _PinnedHTTP(parsed.hostname, port, address, 5))
            try:
                conn.request('GET', (parsed.path or '/') + ('?' + parsed.query if parsed.query else ''),
                             headers={'Accept':'application/json','Cache-Control':'no-cache','User-Agent':f'infobot/{VERSION}'})
                response = conn.getresponse()
                if response.status != 200: raise ValueError(f'Source HTTP {response.status}; redirects are not followed')
                body = response.read(5_000_001)
                if len(body) > 5_000_000: raise ValueError('JSON exceeds 5 MB')
            finally:
                conn.close()
            data = json.loads(body)
            if not isinstance(data, dict): raise ValueError('Expected a JSON object')
            if ttl > 0:
                self.cache[url] = (now, data)
            self.health[label] = ('OK', time.time())
            return data
        except Exception:
            self.health[label] = ('unavailable', time.time())
            raise


def stamp(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def age_label(seconds):
    if seconds < 60: return f'{int(seconds)}s'
    if seconds < 3600: return f'{int(seconds/60)}m'
    return f'{seconds/3600:.1f}h'


def fresh_record(record, limit, now):
    try:
        age = (datetime.fromtimestamp(now, timezone.utc) - stamp(record['issued_at'])).total_seconds()
        return not record.get('stale') and not record.get('future_timestamp') and -300 <= age <= limit
    except (KeyError, TypeError, ValueError): return False


def index_fresh(data, now):
    try:
        age = now - stamp(data['last_scan_at']).timestamp()
        return not data.get('index_error') and not data.get('error') and -300 <= age <= 600
    except (KeyError, TypeError, ValueError):
        return False


def validate_source(label, data, config, now):
    """Validate source schemas/freshness independently of display wording."""
    if label == 'GOES':
        if not index_fresh(data, now): raise ValueError('GOES index is not fresh')
        counties = data.get('counties')
        if not isinstance(counties, dict) or not counties: raise ValueError('No county forecasts')
        if not any(isinstance(item,dict) and fresh_record(item,86400,now) and
                   any(p.get('date') == datetime.fromtimestamp(now,LOCAL).date().isoformat()
                       for p in item.get('periods',[])) for item in counties.values()):
            raise ValueError('No fresh forecast periods for today')
    elif label == 'ADSB':
        generated = number(data.get('now'))
        if generated is None or not -5 <= now-generated <= 120 or not isinstance(data.get('aircraft'),list):
            raise ValueError('ADSB schema or receiver timestamp invalid')
    elif label == 'AIS':
        if data.get('error') or not isinstance(data.get('ships'),list) or any(not isinstance(s,dict) for s in data['ships']):
            raise ValueError('AIS schema invalid')


def short_weather(text):
    # Deterministic abbreviations preserve probabilities, units and qualifiers.
    text = clean(text)
    replacements = [('Chance of precipitation', 'Precip'), ('Chance of rain', 'Rain chance'),
                    ('Chance of snow', 'Snow chance'), ('percent', '%'), ('Highs', 'High'), ('Lows','Low'),
                    ('Northwest','NW'), ('Northeast','NE'), ('Southwest','SW'), ('Southeast','SE'),
                    ('North','N'), ('South','S'), ('West','W'), ('East','E'), ('winds','wind'),
                    ('degrees','deg')]
    for before, after in replacements:
        text = re.sub(r'\b'+re.escape(before)+r'\b', after, text, flags=re.I)
    return text.replace(' %','%')


def distance_miles(lat1, lon1, lat2, lon2):
    radius = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2-p1, math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0, 1-a)))


def _ring_relation(lon, lat, ring):
    """Return (inside, on_boundary) for one WGS84 longitude/latitude ring."""
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        cross = (lon-x1)*(y2-y1) - (lat-y1)*(x2-x1)
        scale = max(1.0, abs(x2-x1), abs(y2-y1))
        if abs(cross) <= 1e-10*scale and min(x1,x2)-1e-10 <= lon <= max(x1,x2)+1e-10 and min(y1,y2)-1e-10 <= lat <= max(y1,y2)+1e-10:
            return False, True
        if (y1 > lat) != (y2 > lat):
            crossing_lon = x1 + (lat-y1)*(x2-x1)/(y2-y1)
            if lon < crossing_lon:
                inside = not inside
    return inside, False


def _geometry_relation(lon, lat, geometry):
    """Return (inside, on_boundary), respecting polygon holes and multipart counties."""
    polygons = geometry['coordinates'] if geometry['type'] == 'MultiPolygon' else [geometry['coordinates']]
    for rings in polygons:
        outer, boundary = _ring_relation(lon, lat, rings[0])
        if boundary:
            return False, True
        if not outer:
            continue
        in_hole = False
        for ring in rings[1:]:
            hole, boundary = _ring_relation(lon, lat, ring)
            if boundary:
                return False, True
            in_hole = in_hole or hole
        if not in_hole:
            return True, False
    return False, False


def county_for_location(lat, lon):
    """Find a supported county containing the point; report boundary ambiguity separately."""
    for slug, entry in COUNTY_DATA.items():
        inside, boundary = _geometry_relation(lon, lat, entry['geometry'])
        if boundary:
            return None, True
        if inside:
            return slug, False
    return None, False


def nearest_center_county(lat, lon):
    return min(DTX_COUNTIES, key=lambda slug: distance_miles(
        lat, lon, DTX_COUNTIES[slug][1], DTX_COUNTIES[slug][2]))


def _geometry_rings(geometry):
    polygons = geometry['coordinates'] if geometry['type'] == 'MultiPolygon' else [geometry['coordinates']]
    for polygon in polygons:
        yield from polygon


def distance_to_county_miles(lat, lon, slug):
    """Approximate shortest point-to-boundary distance in a local tangent plane."""
    x_scale = 69.172 * math.cos(math.radians(lat))
    y_scale = 69.0
    best = float('inf')
    for ring in _geometry_rings(COUNTY_DATA[slug]['geometry']):
        points = [((x-lon)*x_scale, (y-lat)*y_scale) for x,y in ring]
        for (x1,y1),(x2,y2) in zip(points,points[1:]):
            dx,dy=x2-x1,y2-y1
            denom=dx*dx+dy*dy
            t=0.0 if denom==0 else max(0.0,min(1.0,-(x1*dx+y1*dy)/denom))
            best=min(best,math.hypot(x1+t*dx,y1+t*dy))
    return best


def nearest_boundary_county(lat, lon):
    distances={slug:distance_to_county_miles(lat,lon,slug) for slug in COUNTY_DATA}
    return min(distances,key=distances.get),min(distances.values())


def _coordinates(contact):
    lat = number(contact.get('adv_lat', contact.get('lat')))
    lon = number(contact.get('adv_lon', contact.get('lon')))
    if lat is None or lon is None:
        return None
    if abs(lat) > 90 or abs(lon) > 180:
        lat, lon = lat/1_000_000, lon/1_000_000
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    return lat, lon


def _route_kind(payload):
    for value in (payload.get('route_type'), payload.get('route_typename')):
        try:
            numeric = int(value)
            if numeric in (2, 3):
                return 'direct'
            if numeric in (0, 1):
                return 'flood'
        except (ValueError, TypeError):
            pass
        name = str(value).upper()
        if 'DIRECT' in name:
            return 'direct'
        if 'FLOOD' in name:
            return 'flood'
    return None


def _pinnebog_location(contacts):
    candidates = []
    for contact in contacts.values():
        if not isinstance(contact, dict):
            continue
        try:
            kind = int(contact.get('type', contact.get('adv_type', -1)))
        except (ValueError, TypeError):
            continue
        name = clean(contact.get('adv_name') or contact.get('name') or '')
        if kind not in (2, 3) or not re.search(r'\bpinnebog\b', name, re.I):
            continue
        coords = _coordinates(contact)
        if coords:
            candidates.append((name, *coords))
    if len(candidates) == 1:
        name, lat, lon = candidates[0]
    else:
        lat, lon = PINNEBOG_COORDINATES
        name = 'Pinnebog'
    county, on_boundary = county_for_location(lat, lon)
    return {'lat': lat, 'lon': lon, 'name': name, 'county': county, 'county_boundary': on_boundary}


def dm_location(sender,contacts,now=None):
    """Estimate DM origin from the sender end of the learned OUTBOUND route.

    The companion does not expose a DM's received hop hashes. Its stored path
    runs from us to the sender, so the LAST hop is the sender-side repeater.
    This is a cached route estimate, never claimed as a captured incoming path.
    """
    contact=next((value for key,value in contacts.items()
                  if isinstance(key,str) and key.lower()==sender.lower() and isinstance(value,dict)),None)
    if contact is None:
        return None, 'Location unavailable: DM sender is missing from local contacts.'
    count=contact.get('out_path_len')
    raw=contact.get('out_path')
    if count==0 and raw=='':
        return _pinnebog_location(contacts),None
    mode=contact.get('out_path_hash_mode')
    if not isinstance(count,int) or not 1<=count<=63 or mode not in (0,1,2):
        return None, 'Location unavailable: no usable learned return route for this DM sender. Use wx <county>.'
    width=2*(mode+1)
    if not isinstance(raw,str) or len(raw)!=count*width or not re.fullmatch(r'[0-9a-fA-F]+',raw):
        return None, 'Location unavailable: DM sender return route is malformed.'
    location,error=_repeater_location(raw[-width:].lower(),contacts,'DM sender-side',now=now)
    if location is not None: location['basis']='dm_return_route'
    return location,error


def request_location(payload, contacts, now=None):
    if payload.get('_dm_sender'):
        return dm_location(payload['_dm_sender'],contacts,now=now)
    route_kind = _route_kind(payload)
    if route_kind == 'direct':
        return _pinnebog_location(contacts), None
    raw = payload.get('path')
    count = payload.get('path_len')
    if not raw or count in (None, 0, 255):
        # Some direct and flood RX events arrive without the raw path metadata
        # correlated from RX_LOG_DATA. Treat any pathless request as local to
        # Pinnebog so location-aware commands remain usable for those packets.
        return _pinnebog_location(contacts), None
    mode = payload.get('path_hash_mode')
    if mode in (0, 1, 2):
        width = 2*(mode+1)
    elif isinstance(count, int) and count > 0 and len(raw) % (2*count) == 0:
        width = len(raw)//count
    else:
        return None, 'Location unavailable: hop-1 hash width is unknown.'
    if not isinstance(raw, str) or not re.fullmatch(r'[0-9a-fA-F]+', raw) or len(raw) < width:
        return None, 'Location unavailable: the received path is malformed.'
    hop = raw[:width].lower()
    return _repeater_location(hop,contacts,now=now)


REPEATER_MAX_AGE = 48 * 60 * 60


def _matching_repeaters(hop, contacts, now):
    for key, contact in contacts.items():
        if not isinstance(key, str) or not isinstance(contact, dict) or not key.lower().startswith(hop):
            continue
        try:
            kind = int(contact.get('type', contact.get('adv_type', -1)))
        except (ValueError, TypeError):
            continue
        if kind not in (2, 3):
            continue
        last_advert = number(contact.get('last_advert'), 0)
        if 0 < last_advert <= now - REPEATER_MAX_AGE:
            continue
        yield contact


def _repeater_location(hop,contacts,label='hop-1',now=None):
    matches = list(_matching_repeaters(hop, contacts, time.time() if now is None else now))
    if len(matches) != 1:
        return None, f'Location unavailable: {label} repeater is missing or ambiguous in local contacts.'
    contact = matches[0]
    coords = _coordinates(contact)
    if coords is None:
        return None, f'Location unavailable: {label} repeater has no advertised coordinates.'
    lat, lon = coords
    county, on_boundary = county_for_location(lat, lon)
    name = clean(contact.get('adv_name') or label)
    return {'lat': lat, 'lon': lon, 'name': name, 'county': county, 'county_boundary': on_boundary}, None


def weather_counties(location):
    """Select the containing county and, when close, the nearest-center county."""
    lat, lon = location['lat'], location['lon']
    containing = location.get('county')
    if containing and not location.get('county_boundary'):
        targets = [containing]
        center_county = nearest_center_county(lat, lon)
        if (center_county != containing and
                distance_miles(lat, lon, DTX_COUNTIES[center_county][1], DTX_COUNTIES[center_county][2]) <= 50):
            targets.append(center_county)
        return targets, False

    nearest, boundary_distance = nearest_boundary_county(lat, lon)
    if boundary_distance > 50:
        return [], True
    # Border, water, and points outside supported counties use the nearest
    # county center as the fallback location.
    return [nearest_center_county(lat, lon)], True


def weather_lines(item, title, now, dates):
    if not fresh_record(item, 24*3600, now):
        return [f'{title}: forecast stale/unavailable.']
    local = stamp(item['local_time']).tzinfo if 'local_time' in item else LOCAL
    issued = stamp(item['issued_at']).astimezone(local).strftime('%m/%d %H:%M')
    tz = item.get('time_abbr', 'ET')
    lines = [f'{title} WX issued {issued} {tz}:']
    wanted = [p for p in item.get('periods', []) if p.get('date') in dates]
    if not wanted:
        lines.append('No today/tomorrow periods available.')
    for period in wanted:
        if period['date'] == dates[0]:
            label = 'Tonight' if period.get('night') else 'Today'
        else:
            label = 'Tomorrow night' if period.get('night') else 'Tomorrow'
        lines.append(f'{label}: {short_weather(period["text"])}')
    if item.get('missing_dates'):
        lines.append('Some daytime periods unavailable.')
    return lines


def weather(data, county, now, location):
    if not index_fresh(data, now): return 'WX unavailable: GOES index is not fresh/ready.'
    if data.get('index_error'): return 'WX unavailable: GOES index error.'
    try:
        if now-stamp(data['last_scan_at']).timestamp() > 600: return 'WX unavailable: GOES index has not refreshed in 10 min.'
    except (KeyError, TypeError, ValueError): return 'WX unavailable: missing index timestamp.'
    if data.get('unavailable'):
        return data['unavailable']
    if 'forecasts' in data:
        title = data['county']
        forecasts = data['forecasts']
        if not any(fresh_record(item, 24*3600, now) for item in forecasts):
            return f'{title}: forecast unavailable from GOES-19.'
        result = []
        for item in forecasts:
            name = item['name'] if len(forecasts) > 1 else title
            if item.get('requested_dates'):
                result.extend(weather_lines(item, name, now, item['requested_dates']))
            else:
                result.append(f'{name}: forecast unavailable from GOES-19.')
        if len(forecasts) > 1:
            result.insert(0, title)
        return '\n'.join(result)
    today = datetime.fromtimestamp(now, LOCAL).date()
    dates = [today.isoformat(), today.fromordinal(today.toordinal()+1).isoformat()]
    result = []
    if county:
        targets = [county]
    else:
        targets, outside = weather_counties(location)
        if outside:
            if not targets:
                return 'Outside the forecast area.'
            result.append('Outside the forecast area; nearest supported county: '+DTX_COUNTIES[targets[0]][0]+'.')
    indexed = data.get('counties', {})
    for name in targets:
        item = indexed.get(name)
        title = DTX_COUNTIES.get(name, (name.title(),))[0]
        if item is None:
            result.append(f'{title}: forecast unavailable from GOES-19.')
            continue
        result.extend(weather_lines(item, title, now, dates))
    return '\n'.join(result) if result else 'WX unavailable: no matching county.'


def spaceweather(data, now):
    if not index_fresh(data, now): return 'Space weather unavailable: GOES index is not fresh/ready.'
    products = data.get('products')
    record = products.get('DAYDIS') if isinstance(products, dict) else None
    if isinstance(record, dict) and not fresh_record(record, 18*3600, now):
        return 'Space weather unavailable: DAYDIS forecast stale or future-dated.'
    text = record.get('text') if isinstance(record, dict) else None
    if not isinstance(text, str) or not text:
        return 'No DAYDIS forecast discussion received.'

    labels = {
        'Solar Activity': 'Solar',
        'Energetic Particle': 'Particles',
        'Solar Wind': 'Wind',
        'Geospace': 'Geospace',
    }
    headings = '|'.join(re.escape(name) for name in labels)
    forecasts = []
    for heading, label in labels.items():
        match = re.search(
            rf'(?ms)^{re.escape(heading)}\s*\n(.*?)(?=^(?:{headings})\s*$|\Z)',
            text,
        )
        if not match:
            continue
        forecast = re.search(r'(?ms)^\.Forecast\.\.\.\s*(.*)$', match.group(1))
        if forecast:
            # Keep NOAA's forecast wording, only joining line wraps and removing
            # blank lines from the received bulletin.
            wording = re.sub(r'\s+', ' ', forecast.group(1)).strip()
            if wording:
                forecasts.append(f'{label}: {wording}')
    if not forecasts:
        return 'No DAYDIS forecast discussion received.'

    try:
        issued = stamp(record['issued_at']).astimezone(timezone.utc).strftime('%m/%d %H%MZ')
        header = issued + '\n'
    except (KeyError, TypeError, ValueError):
        header = ''
    return header + '\n'.join(forecasts)


def tracking_key(value):
    return clean(str(value)).strip('@ ').casefold() if value is not None else ''


def aircraft(data, config, now, location, query=None):
    generated = number(data.get('now'))
    if generated is None or not -5 <= now-generated <= 120: return 'ADSB unavailable: receiver JSON timestamp missing/stale.'
    if not isinstance(data.get('aircraft'), list): raise ValueError('Not a readsb aircraft.json response')
    rows = []
    target = tracking_key(query)
    clock_skew = max(0, now-generated)
    for a in data['aircraft']:
        if not isinstance(a, dict): continue
        lat, lon, seen = number(a.get('lat')), number(a.get('lon')), number(a.get('seen_pos'))
        positioned = lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180
        fresh_position = positioned and seen is not None and 0 <= seen + clock_skew <= config.max_aircraft_age
        alt = a.get('alt_baro', a.get('alt_geom'))
        speed = number(a.get('gs'))
        height = 'ground' if alt == 'ground' else f'{int(number(alt))}ft' if number(alt) is not None else 'alt?'
        if query is not None:
            fields = [a.get(field) for field in ('flight', 'r', 'hex')]
            age = number(a.get('seen'))
            if age is None:
                age = seen
            if (not target or target not in map(tracking_key, fields) or age is None
                    or not 0 <= age + clock_skew <= config.max_aircraft_age):
                continue
            identifiers = ' / '.join(dict.fromkeys(clean(str(value)) for value in fields if tracking_key(value)))
            motion = f' {speed:.0f}kt' if speed is not None else ''
            position = f' at {lat:.3f}, {lon:.3f}' if fresh_position else ''
            rows.append((age, f'{identifiers}: {height}{motion}{position}'))
            continue
        if not fresh_position: continue
        ident = clean(a.get('flight') or a.get('r') or a.get('hex') or '?')
        if alt=='ground': continue
        distance = distance_miles(location['lat'], location['lon'], lat, lon)
        rows.append((distance, seen, f'{ident}: {height}'+(f' {speed:.0f}kt' if speed is not None else '')+f' {distance:.1f}mi'))
    rows.sort()
    if query is not None:
        return '\n'.join(row[1] for row in rows) or f'No fresh aircraft matching {query} in the Hinton feed.'
    if not rows: return f'No fresh positioned aircraft in Hinton feed (<= {config.max_aircraft_age}s).'
    return f'Aircraft near {location["name"]} ({len(rows)}; nearest first):\n'+'\n'.join(x[2] for x in rows[:100]) + ('\nList capped at 100.' if len(rows)>100 else '')


def vessels(data, config, now, location, query=None):
    if data.get('error'): raise ValueError('AIS receiver reported an error')
    if not isinstance(data.get('ships'), list): raise ValueError('Expected AIS-catcher ships[] objects; use /api/ships_full.json')
    rows = []
    target = tracking_key(query)
    imo_match = re.fullmatch(r'(?:imo\s*)?(\d{7})', target)
    for s in data['ships']:
        if not isinstance(s, dict): raise ValueError('Compact AIS arrays unsupported; use /api/ships_full.json')
        mmsi = str(s.get('mmsi', ''))
        # Individual ships and associated craft; exclude coast stations, aids, SAR aircraft.
        if not re.fullmatch(r'(?:[2-7]\d{8}|98\d{7})', mmsi): continue
        age = number(s.get('last_signal'))
        if age is None or not 0<=age<=config.max_ship_age: continue
        name = clean(s.get('shipname') or s.get('callsign') or mmsi).strip('@ ')
        speed = number(s.get('speed'))
        if speed is not None and not 0<=speed<102.3: speed = None
        lat = number(s.get('lat', s.get('latitude')))
        lon = number(s.get('lon', s.get('longitude')))
        positioned = lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180
        if query is not None:
            imo = tracking_key(s.get('imo'))
            if not target or not (target in (mmsi, tracking_key(s.get('shipname')), tracking_key(s.get('callsign')))
                                  or imo_match and imo_match[1] == imo and imo != '0000000'):
                continue
            call = clean(s.get('callsign') or '').strip('@ ')
            identifiers = f'MMSI {mmsi}'
            if re.fullmatch(r'\d{7}', imo) and imo != '0000000':
                identifiers += f', IMO {imo}'
            if call and call != name:
                identifiers += f', {call}'
            motion = f' {speed:g}kt' if speed is not None else ''
            position = f' at {lat:.3f}, {lon:.3f}' if positioned else ''
            rows.append((age, f'{name or mmsi} ({identifiers}){motion}{position}, heard {age_label(age)} ago'))
            continue
        distance = distance_miles(location['lat'], location['lon'], lat, lon) if positioned else float('inf')
        proximity = f' {distance:.1f}mi' if math.isfinite(distance) else ' distance unknown'
        rows.append((distance, age, f'{name or mmsi}'+(f' {speed:g}kt' if speed is not None else '')+proximity+f' (heard {age_label(age)} ago)'))
    rows.sort()
    if query is not None:
        return '\n'.join(row[1] for row in rows) or f'No recently heard vessels matching {query} in the AIS feed.'
    if not rows: return f'No vessels heard by tarratt.net in the last {config.max_ship_age//60} min.'
    return f'Boats near {location["name"]} ({len(rows)}; nearest first):\n'+'\n'.join(x[2] for x in rows[:100])+('\nList capped at 100.' if len(rows)>100 else '')


def ping_host(args):
    if len(args) != 1:
        return 'Use ping <IPv4|IPv6|hostname>; omit target for Pong!'
    raw = args[0].strip()
    if not raw or len(raw) > 253:
        return 'ping: invalid target.'
    if raw.startswith('[') and raw.endswith(']'):
        raw = raw[1:-1]
    try:
        literal = ipaddress.ip_address(raw)
        if literal.is_multicast or literal.is_unspecified or getattr(literal, 'is_link_local', False) and literal.version == 4:
            return 'ping: multicast, unspecified, and IPv4 link-local targets are not supported.'
        address = str(literal)
        family = literal.version
    except ValueError:
        host = raw.rstrip('.')
        try:
            host = host.encode('idna').decode('ascii')
        except UnicodeError:
            return 'ping: invalid hostname.'
        labels = host.split('.')
        if not host or len(host) > 253 or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in labels):
            return 'ping: invalid hostname.'
        address, family = host, None
    command = ['ping', '-n', '-c', '3', '-W', '1', '-w', '4']
    if family is not None:
        command.append('-4' if family == 4 else '-6')
    command.extend(['--', address])
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=6, check=False)
    except FileNotFoundError:
        return 'ping unavailable: install iputils-ping in the container image.'
    except subprocess.TimeoutExpired:
        return f'Ping {address}: timed out.'
    except OSError as error:
        return f'ping failed for {address}: {clean(error)[:75]}'
    output = (result.stdout or '') + '\n' + (result.stderr or '')
    stats = re.search(r'(\d+) packets transmitted,\s*(\d+) (?:packets )?received(?:,\s*\+\d+\s+errors?)?,\s*([\d.]+)% packet loss', output)
    if not stats:
        return f'ping failed for {address}: {clean(result.stderr or result.stdout or "no ping summary")[:75]}'
    sent, received, loss = stats.groups()
    rtt = re.search(r'(?:rtt|round-trip) min/avg/max(?:/mdev)? = ([\d.]+)/([\d.]+)/([\d.]+)', output)
    summary = f'Ping {address}: {sent} sent, {received} received, {loss}% loss'
    if rtt:
        summary += f'; {rtt[1]}/{rtt[2]}/{rtt[3]} ms min/avg/max'
    return summary + '.'


def path_reply(args, payload, contacts, now=None):
    now = time.time() if now is None else now
    if args:
        joined = ' '.join(args).lower()
        tokens = re.split(r'[\s,>:-]+', joined)
        if len(tokens)==1:
            raw = tokens[0]
            if not re.fullmatch(r'(?:[0-9a-f]{4}){1,32}', raw): return 'Use path aabb,ccdd or contiguous 2-byte hashes.'
            hops = [raw[i:i+4] for i in range(0,len(raw),4)]
        else:
            hops = [t for t in tokens if t]
            if not hops or len(hops)>64 or len({len(h) for h in hops})!=1 or any(not re.fullmatch(r'(?:[0-9a-f]{2}){1,3}', h) for h in hops):
                return 'Invalid path. Use equal-width hex hashes, e.g. path aabb,ccdd.'
    else:
        raw = payload.get('path')
        count = payload.get('path_len')
        if raw is None:
            return f'Path hashes unavailable; reported hops: {count if count not in (None,255) else "unknown/direct"}. Try a fresh path message.'
        if not raw: return 'Path: direct (0 repeater hops).' if count==0 else 'Path: no captured repeater hashes (direct-route metadata).'
        mode = payload.get('path_hash_mode')
        if mode in (0,1,2): width = 2*(mode+1)
        elif isinstance(count,int) and 0<count<255 and len(raw)%(2*count)==0: width = len(raw)//count
        else: return 'Path hashes received, but hop width unknown.'
        if not re.fullmatch(r'[0-9a-fA-F]+',raw) or len(raw)%width: return 'Malformed received path.'
        hops = [raw[i:i+width].lower() for i in range(0,len(raw),width)]
    named=[]
    for index, hop in enumerate(hops, 1):
        matches = [clean(c.get('adv_name') or hop) for c in _matching_repeaters(hop, contacts, now)]
        label = matches[0] if len(matches)==1 else ('ambiguous' if matches else '?')
        if len(matches)==1:
            label = re.sub(r'thumbmesh\.com', '', label, flags=re.IGNORECASE).rstrip('- ')
            label = label or '?'
        marker = CIRCLED_HOP_NUMBERS[index-1] if index <= len(CIRCLED_HOP_NUMBERS) else f'[{index}]'
        named.append(f'{marker} {label}/{hop}')
    return '\n'.join(named)


def _packet_bytes(value):
    if isinstance(value, bytes): return value
    if isinstance(value, bytearray): return bytes(value)
    if isinstance(value, str):
        compact = re.sub(r'\s+', '', value)
        try: return bytes.fromhex(compact)
        except ValueError: return value.encode()
    return b''


def packet_metadata_from_raw(value):
    """Extract public route fields from one RX-log packet, if present."""
    raw = _packet_bytes(value)
    if len(raw) < 2: return {}
    header = raw[0]
    route = header & 0x03
    payload_type = (header >> 2) & 0x0f
    result = {'route_type': route, 'payload_type': payload_type}
    offset = 1
    if route in (0, 3):
        if len(raw) < offset+4: return result
        result['transport_code'] = raw[offset:offset+4].hex()
        offset += 4
    if len(raw) <= offset: return result
    path_field = raw[offset]
    result['path_len'] = path_field & 0x3f
    result['path_hash_size'] = ((path_field >> 6) & 0x03)+1
    offset += 1
    path_bytes = result['path_len']*result['path_hash_size']
    if len(raw) < offset+path_bytes: return result
    result['path'] = raw[offset:offset+path_bytes].hex()
    offset += path_bytes
    result['pkt_payload'] = raw[offset:]
    return result


def _region_code(value):
    if isinstance(value, int) and not isinstance(value, bool): return value & 0xffff
    if isinstance(value, (list, tuple)) and value:
        return _region_code(value[0])
    if isinstance(value, str):
        compact = re.sub(r'\s+', '', value)
        if compact.lower().startswith('0x'):
            try: return int(compact, 16) & 0xffff
            except ValueError: return None
        if re.fullmatch(r'[0-9a-fA-F]{4,}', compact):
            try: return int.from_bytes(bytes.fromhex(compact)[:2], 'little')
            except ValueError: return None
    raw = _packet_bytes(value)
    return int.from_bytes(raw[:2], 'little') if len(raw) >= 2 else None


def _region_label(payload, candidates):
    for key in ('region_name', 'region_scope', 'scope_name', 'scope', 'region'):
        value = payload.get(key)
        if isinstance(value, dict): value = value.get('name') or value.get('scope')
        if isinstance(value, str) and value.strip(): return value.strip().lstrip('#')

    route = payload.get('route_type', payload.get('route_typename'))
    route_text = str(route).upper()
    if route in (1, 2) or route_text in ('FLOOD', 'DIRECT', 'ROUTE_TYPE_FLOOD', 'ROUTE_TYPE_DIRECT'):
        return 'unscoped'

    code = _region_code(payload.get('transport_code', payload.get('transport_codes')))
    if code is None: return 'tag unavailable'
    if code == 0: return 'unscoped'

    raw_payload = payload.get('pkt_payload', payload.get('packet_payload'))
    payload_type = payload.get('payload_type')
    packet_bytes = _packet_bytes(raw_payload)
    if isinstance(payload_type, int) and 0 <= payload_type <= 255 and packet_bytes:
        message = bytes((payload_type,)) + packet_bytes
        matches = set()
        for name in candidates:
            region_name = name.strip().lstrip('#')
            if not region_name: continue
            key = hashlib.sha256(('#'+region_name).encode()).digest()[:16]
            actual = int.from_bytes(hmac.new(key, message, hashlib.sha256).digest()[:2], 'little')
            # Firmware reserves zero and 0xffff and shifts those HMAC values inward.
            if actual == 0: actual = 1
            elif actual == 0xffff: actual = 0xfffe
            if actual == code: matches.add(region_name)
        if len(matches) == 1: return matches.pop()
        if matches: return 'ambiguous region tag'
    return f'unknown (0x{code:04x})'


def packet_info_reply(payload, region_names=('mi',)):
    size = payload.get('path_hash_size')
    if not isinstance(size, int) or not 1 <= size <= 3:
        mode = payload.get('path_hash_mode')
        size = mode + 1 if isinstance(mode, int) and mode in (0, 1, 2) else None
    hops = payload.get('path_len')
    if not isinstance(hops, int) or not 0 <= hops < 255: hops = None
    path = payload.get('path')
    path_bytes = len(path) if isinstance(path, (bytes, bytearray)) else None
    if path_bytes is None and isinstance(path, str) and re.fullmatch(r'[0-9a-fA-F]*', path):
        path_bytes = len(path) // 2
    if size is None and path_bytes is not None and hops:
        size = path_bytes // hops if path_bytes % hops == 0 else None
    if size is None:
        hashes = 'Path hash size: unknown'
    elif hops is None:
        hashes = f'Path hashes: {size} B/hop'
    else:
        total = path_bytes if path_bytes is not None else size*hops
        hashes = f'Path hashes: {size} B/hop, {hops} hops/{total} B total'
    # Keep this diagnostic as one plain line. Channel clients do not all render
    # embedded newlines consistently, and the full answer comfortably fits one
    # LoRa message.
    return f'{hashes}; Region: {_region_label(payload, region_names)}'


def chunks(text, limit=130):
    """Keep lines intact where possible while respecting UTF-8 byte limits."""
    out, current = [], ''
    for line in text.splitlines():
        if not line.strip():
            continue
        for part in _line_chunks(line, limit):
            candidate = current + '\n' + part if current else part
            if len(candidate.encode()) > limit:
                out.append(current)
                current = part
            else:
                current = candidate
    if current:
        out.append(current)
    return out or ['No data.']


def _line_chunks(text, limit=130):
    """Split on word boundaries without ever exceeding UTF-8 byte budget."""
    out, current = [], ''
    for word in text.split():
        for part in split_long(word, limit):
            candidate = (current+' '+part).strip()
            if len(candidate.encode())>limit:
                out.append(current);current=part
            else: current=candidate
    if current: out.append(current)
    return out or ['No data.']


def split_long(word, limit):
    current=''
    for ch in word:
        if len((current+ch).encode())>limit:
            yield current;current=''
        current+=ch
    if current: yield current


class State:
    def __init__(self,path):
        self.db=sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, at REAL)')
        self.db.execute('DELETE FROM seen WHERE at < ?', (time.time()-86400,));self.db.commit()
        self.pages={};self.cooldowns={}
    def accept_reason(self, payload, sender, cooldown, now):
        fingerprint=hashlib.sha256(json.dumps([payload.get('_conversation',payload.get('channel_idx')),payload.get('pubkey_prefix'),payload.get('sender_timestamp'),payload.get('text')],ensure_ascii=False).encode()).hexdigest()
        remaining=cooldown-(now-self.cooldowns.get(sender,-1e9))
        if remaining>0:return ('cooldown',remaining)
        cursor=self.db.execute('INSERT OR IGNORE INTO seen VALUES (?,?)',(fingerprint,now));self.db.commit()
        if cursor.rowcount!=1:return ('duplicate',0)
        self.cooldowns[sender]=now
        self.db.execute('DELETE FROM seen WHERE at < ?', (now-86400,));self.db.commit()
        self.cooldowns={k:v for k,v in self.cooldowns.items() if now-v<600}
        return None
    def accept(self, payload, sender, cooldown, now):
        return self.accept_reason(payload,sender,cooldown,now) is None
    def page(self,sender,text,config,now):
        self.pages={k:v for k,v in self.pages.items() if v[0]>now}
        if text is not None:
            # Let short replies use the whole packet. Only reserve pagination
            # space when the complete response does not fit in one message.
            page_chunks = ([text] if len(text.encode()) <= config.byte_limit
                           else chunks(text,config.byte_limit-25))
            self.pages[sender]=(now+600,page_chunks,[])
            if len(self.pages)>200:self.pages.pop(next(iter(self.pages)))
        expiry, remaining, _last_sent=self.pages.get(sender,(0,[],[]))
        if not remaining:return ['No more saved text.']
        selected,rest=remaining[:config.parts],remaining[config.parts:]
        response=[f'[{i+1}/{len(selected)}] {line}' if len(selected)>1 else line for i,line in enumerate(selected)]
        if rest:response[-1]+=' | more'
        self.pages[sender]=(expiry,rest,response)
        return response
    def repeat_page(self,sender,page_number,now):
        self.pages={k:v for k,v in self.pages.items() if v[0]>now}
        saved=self.pages.get(sender)
        if not saved:return None
        last_sent=saved[2]
        if page_number<1 or page_number>len(last_sent):return None
        return last_sent[page_number-1]


class Engine:
    def __init__(self,config,sources=None):
        self.config=config;self.sources=sources or Sources();self.started=time.time();self.satellites=SatellitePasses(config)
    def execute(self,cmd,args,payload=None,contacts=None,now=None):
        now=now or time.time();payload=payload or {};contacts=contacts or {}
        c=self.config
        if cmd=='about':return f'Infobot v{VERSION}\nThe homebrew bot at tarratt.net HQ.\nNetwork commands: internet, get, sat refresh, ping <target>, bot, <question>.'
        if cmd=='internet':
            if args:return 'Use internet with no arguments.'
            return internet_status()
        if cmd=='get':
            if not args:return 'Use get <http(s) URL>.'
            return fetch_one_packet(' '.join(args),c.byte_limit)
        if cmd=='bot':
            if not args:return 'Use bot, <question>.'
            reply = web_answer(args[0], c.searxng, c.llama, c.byte_limit, now, c.command_timeout)
            if len(reply.encode()) <= c.byte_limit:
                return reply
            pages = chunks(reply, c.byte_limit - 25)
            if len(pages) > c.parts:
                pages = pages[:c.parts]
                pages[-1] = pages[-1].encode()[:c.byte_limit - 28].decode('utf-8', 'ignore').rstrip() + '…'
            return '\n'.join(pages)
        if cmd=='sat':
            location,error=request_location(payload,contacts,now=now)
            return error or self.satellites.report(args,now,location)
        if cmd=='ping':return ping_host(args) if args else 'Pong!'
        if cmd=='help':return HELP_DETAIL.get(ALIASES.get(args[0].lower(),args[0].lower()),HELP) if args else HELP
        if cmd=='path':return path_reply(args,payload,contacts,now=now)
        if cmd=='hashsize':return packet_info_reply(payload,c.region_names)
        if cmd=='wx':
            url = c.goes+'/v1/weather'
            if args:
                place = ' '.join(args)
                if not re.fullmatch(r"[A-Za-z][A-Za-z .'-]*|[0-9]{5}", place):
                    return 'Use wx <Michigan county> or wx <5-digit US ZIP>.'
                county = re.sub(r'[^a-z0-9]', '', place.lower()).removesuffix('county')
                data = self.sources.get('GOES', url + '?' + urlencode({'place': place}), ttl=0)
                return weather(data, county, now, None)
            location,error=request_location(payload,contacts,now=now)
            if error:return error
            text=weather(self.sources.get('GOES',url),None,now,location)
            if location and location.get('basis')=='dm_return_route':
                text=f'WX near {location["name"]} (DM route estimate):\n'+text
            return text
        if cmd=='aurora':return spaceweather(self.sources.get('GOES',c.goes+'/v1/spaceweather'),now)
        if cmd=='adsb':
            location,error=(None,None) if args else request_location(payload,contacts,now=now)
            return error or aircraft(self.sources.get('ADSB',c.adsb,ttl=3),c,now,location,' '.join(args) if args else None)
        if cmd=='ais':
            location,error=(None,None) if args else request_location(payload,contacts,now=now)
            return error or vessels(self.sources.get('AIS',c.ais,ttl=0),c,now,location,' '.join(args) if args else None)
        if cmd=='status':
            discussion='unavailable'
            try:
                data=self.sources.get('GOES',c.goes+'/v1/status')
                age=now-stamp(data['last_scan_at']).timestamp()
                goes='ready' if data.get('ready') and not data.get('error') and 0<=age<600 else 'not fresh/ready'
                record=data.get('products',{}).get('DAYDIS')
                discussion=('fresh' if fresh_record(record,18*3600,now) else 'stale/future') if isinstance(record,dict) else 'missing'
            except Exception:goes='unavailable'
            return f'infobot up {age_label(now-self.started)}. GOES index {goes}; DAYDIS {discussion}. '+', '.join(f'{k}: {v[0]} ({age_label(max(0,now-v[1]))} ago)' for k,v in self.sources.health.items())+'.'
        raise ValueError('Unknown command')
