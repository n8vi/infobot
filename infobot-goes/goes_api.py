#!/usr/bin/env python3
"""Infobot GOES/EMWIN API. Python 3.10+, standard library only; no outbound I/O."""
import argparse
import copy
import heapq
import json
import logging
import shutil
from maps import MapIndex
import os
from pathlib import Path
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

UTC = timezone.utc
LOCAL = ZoneInfo('America/Detroit')
COUNTIES = {
    'midland': 'MIZ047', 'bay': 'MIZ048', 'huron': 'MIZ049', 'saginaw': 'MIZ053',
    'tuscola': 'MIZ054', 'sanilac': 'MIZ055', 'shiawassee': 'MIZ060',
    'genesee': 'MIZ061', 'lapeer': 'MIZ062', 'stclair': 'MIZ063',
    'livingston': 'MIZ068', 'oakland': 'MIZ069', 'macomb': 'MIZ070',
    'washtenaw': 'MIZ075', 'wayne': 'MIZ076', 'lenawee': 'MIZ082', 'monroe': 'MIZ083',
}
WEEKDAYS = ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY', 'SUNDAY']
STAMP = re.compile(r'_C_KWIN_(\d{14})_')
ISSUE = re.compile(r'(?:Issue Time:|:Issued:)\s*(\d{4} [A-Za-z]{3} \d{2} \d{4})\s+UT(?:C)?', re.I)
LOCAL_ISSUE = re.compile(r'(?m)^\s*(\d{3,4})\s+(AM|PM)\s+(EST|EDT)\s+\w{3}\s+(\w{3})\s+(\d{1,2})\s+(\d{4})\s*$')
PERIOD = re.compile(r'^\.([A-Z][A-Z /0-9-]*?)\.\.\.(.*?)(?=^\.[A-Z][A-Z /0-9-]*?\.\.\.|\Z)', re.M | re.S)


def iso(dt):
    return dt.astimezone(UTC).isoformat()


def clean(s):
    return ' '.join(s.split())


def local_issue(text):
    m = LOCAL_ISSUE.search(text)
    if not m:
        raise ValueError('missing local issue timestamp')
    clock, ap, tz, month, day, year = m.groups()
    t = datetime.strptime(f'{clock.zfill(4)} {ap} {month} {day} {year}', '%I%M %p %b %d %Y')
    # Explicit bulletin abbreviation avoids ambiguity at the autumn clock change.
    return t.replace(tzinfo=timezone(timedelta(hours=-5 if tz == 'EST' else -4)))


def zone_codes(text):
    """Expand wrapped UGC zone headers, including MIZ049-054>055-... ."""
    m = re.search(r'(?m)^(MIZ\d{3}[-\d>\sA-Z]*?\d{6}-)', text)
    if not m:
        return set()
    tokens = re.sub(r'\s+', '', m.group(1)).split('-')
    result, prefix = set(), 'MIZ'
    for token in tokens:
        if not token or re.fullmatch(r'\d{6}', token):
            continue
        match = re.fullmatch(r'([A-Z]{3})?(\d{3})(?:>(\d{3}))?', token)
        if match:
            prefix = match[1] or prefix
            start, end = int(match[2]), int(match[3] or match[2])
            if end >= start:
                result.update(f'{prefix}{n:03d}' for n in range(start, end + 1))
    return result


def period_date(label, issued, cursor):
    """Conservative mapping: unknown/combined labels remain unassigned."""
    day = issued.astimezone(LOCAL).date()
    if label in ('TODAY', 'THIS AFTERNOON', 'REST OF TODAY', 'THIS MORNING'):
        return day
    if label in ('TONIGHT', 'THIS EVENING'):
        return day
    if label in ('REST OF TONIGHT', 'OVERNIGHT', 'EARLY THIS MORNING'):
        return day - timedelta(days=1) if issued.hour < 6 else day
    name = label.removesuffix(' NIGHT')
    if name in WEEKDAYS:
        candidate = day + timedelta(days=(WEEKDAYS.index(name) - day.weekday()) % 7)
        while candidate < cursor:
            candidate += timedelta(days=7)
        return candidate
    # Only explicit, fixed-date holidays are assigned here.
    if name == 'VETERANS DAY':
        return datetime(day.year, 11, 11).date()
    if name == 'CHRISTMAS DAY':
        return datetime(day.year, 12, 25).date()
    if name == 'NEW YEARS DAY':
        return datetime(day.year + (day.month == 12), 1, 1).date()
    return None


def parse_weather(text):
    if 'ZFPDTX' not in text or '$$' not in text:
        raise ValueError('not a complete ZFPDTX bulletin')
    overall = local_issue(text)
    counties = {}
    for block in text.split('$$')[:-1]:
        codes = zone_codes(block)
        targets = [name for name, code in COUNTIES.items() if code in codes]
        if not targets:
            continue
        issued = local_issue(block)
        cursor = issued.astimezone(LOCAL).date()
        periods, unassigned = [], []
        for label, prose in PERIOD.findall(block):
            date = period_date(label, issued, cursor)
            item = {'label': label, 'text': clean(prose), 'date': date.isoformat() if date else None,
                    'night': any(x in label for x in ('NIGHT', 'EVENING', 'OVERNIGHT'))}
            if date:
                cursor = max(cursor, date)
                periods.append(item)
            else:
                unassigned.append(item)
        if not periods:
            raise ValueError('no dated periods for ' + ', '.join(targets))
        for name in targets:
            counties[name] = {'zone': COUNTIES[name], 'issued_at': iso(issued),
                              'periods': periods, 'unassigned_periods': unassigned}
    if not counties:
        raise ValueError('no DTX county sections found')
    return overall, {'counties': counties}


def parse_space(text):
    m = ISSUE.search(text)
    if not m:
        raise ValueError('missing SWPC issue timestamp')
    issued = datetime.strptime(m[1], '%Y %b %d %H%M').replace(tzinfo=UTC)
    code_match = re.search(r'Space Weather Message Code:\s*(\w+)', text)
    if not code_match:
        code_match = re.search(r'(?m)^\s*([A-Z]{3}[A-Z0-9]{3})\s*$', text)
    if not code_match:
        raise ValueError('missing SWPC product code')
    code = code_match[1]
    data = {'product_code': code, 'text': text.strip(), 'kind': 'bulletin'}
    if code.startswith('ALT'):
        data['kind'] = 'threshold_alert'
    elif code.startswith(('WAT', 'WAR')):
        data['kind'] = 'watch' if code.startswith('WAT') else 'warning'
    elif code in ('DAYTDF', 'DAYDIS'):
        data['kind'] = 'forecast' if code == 'DAYTDF' else 'discussion'
    elif code == 'DAYIND':
        data['kind'] = 'daily_indices'
    km = re.search(r'ALERT:\s*Geomagnetic K-index of (\d+)', text)
    if km:
        data['k_threshold'] = int(km[1])
        tm = re.search(r'Threshold Reached:\s*(\d{4} [A-Za-z]{3} \d{2} \d{4})\s+UTC', text)
        data['threshold_reached_at'] = iso(datetime.strptime(tm[1], '%Y %b %d %H%M').replace(tzinfo=UTC)) if tm else None
        sm = re.search(r'NOAA Scale:\s*(G\d)\s*-\s*([^\n]+)', text, re.I)
        data['noaa_scale'] = clean(sm[0]) if sm else None
    # Preserve Bz and solar wind prose under its true retrospective heading.
    if code == 'DAYDIS':
        sm = re.search(r'Solar Wind\s+\.24 hr Summary\.\.\.(.*?)\.Forecast\.\.\.(.*?)(?=\n\s*Geospace|\Z)', text, re.S)
        if sm:
            data['solar_wind'] = {'past_24h_summary': clean(sm[1]), 'forecast': clean(sm[2]), 'current_bz_nt': None}
    return issued, data


def age_fields(record, now, limit_hours):
    result = copy.deepcopy(record)
    age = (now - datetime.fromisoformat(record['issued_at'])).total_seconds()
    result['age_seconds'] = round(age)
    result['stale'] = age > limit_hours * 3600
    result['future_timestamp'] = age < -300
    return result


class Index:
    def __init__(self, root, interval=120):
        self.root, self.interval = Path(root), interval
        self.lock = threading.Lock()
        self.records = {}
        self.status = {'ready': False, 'last_scan_at': None, 'error': None}
        self.cache = {}

    def scan(self):
        started = time.monotonic()
        candidates, count = {}, 0
        def walk_error(error):
            raise error
        if not self.root.is_dir():
            raise OSError(f'Input directory missing: {self.root}')
        # Filter filenames BEFORE stat/open. Retain only eight recent deliveries
        # per product, so the 475k-file archive need not be read or sorted.
        for directory, _, filenames in os.walk(self.root, onerror=walk_error):
            for filename in filenames:
                count += 1
                upper = filename.upper()
                if not upper.endswith('.TXT'):
                    continue
                if 'ZFPDTX' in upper or 'FPUS53KDTX' in upper:
                    key = 'ZFPDTX'
                elif 'KWNP' in upper:
                    key = upper.rsplit('-', 1)[-1].removesuffix('US.TXT')
                else:
                    continue
                stamp = STAMP.search(upper)
                if not stamp:
                    continue
                item = (stamp[1], str(Path(directory) / filename))
                heap = candidates.setdefault(key, [])
                heapq.heappush(heap, item)
                if len(heap) > 8:
                    heapq.heappop(heap)
        with self.lock:
            records = dict(self.records)
        errors, next_cache = [], {}
        for key, heap in candidates.items():
            for stamp, path in sorted(heap, reverse=True):
                try:
                    p = Path(path)
                    st = p.stat()
                    signature = (st.st_size, st.st_mtime_ns)
                    if time.time() - st.st_mtime < 3:
                        continue
                    if st.st_size > 1_000_000:
                        raise ValueError('bulletin exceeds 1 MB')
                    cached = self.cache.get(path)
                    if cached and cached[0] == signature:
                        record = cached[1]
                    else:
                        text = p.read_text(encoding='utf-8', errors='replace').replace('\r', '')
                        after = p.stat()
                        if (after.st_size, after.st_mtime_ns) != signature:
                            continue
                        issued, data = parse_weather(text) if key == 'ZFPDTX' else parse_space(text)
                        record = dict(data, issued_at=iso(issued), filename=p.name,
                                      delivery_at=iso(datetime.strptime(stamp, '%Y%m%d%H%M%S').replace(tzinfo=UTC)),
                                      file_mtime=iso(datetime.fromtimestamp(st.st_mtime, UTC)),
                                      source='GOES-19 HRIT/EMWIN via local SatDump')
                    next_cache[path] = (signature, record)
                    actual_key = 'ZFPDTX' if key == 'ZFPDTX' else record['product_code']
                    old = records.get(actual_key)
                    if not old or (record['issued_at'], record['delivery_at']) > (old['issued_at'], old['delivery_at']):
                        records[actual_key] = record
                except (OSError, ValueError) as error:
                    errors.append({'file': Path(path).name, 'error': str(error)})
        self.cache = next_cache
        with self.lock:
            self.records = records
            self.status = {'ready': True, 'last_scan_at': iso(datetime.now(UTC)), 'error': None,
                           'scan_seconds': round(time.monotonic() - started, 3),
                           'files_enumerated': count, 'product_count': len(records),
                           'parse_error_count': len(errors), 'parse_errors': errors[:10]}
        logging.info('Indexed %s products from %s filenames in %.2fs', len(records), count, time.monotonic() - started)

    def run(self):
        while True:
            try:
                self.scan()
            except Exception as error:
                logging.exception('Index refresh failed')
                with self.lock:
                    self.status['error'] = str(error)
            time.sleep(self.interval)

    def respond(self, path, query, now=None):
        now = now or datetime.now(UTC)
        with self.lock:
            records, status = copy.deepcopy(self.records), dict(self.status)
        common = {'generated_at': iso(now), 'timezone': 'America/Detroit',
                  'index_error': status['error'], 'last_scan_at': status['last_scan_at']}
        if path in ('/', '/v1/status'):
            return 200, dict(common, **{k: v for k, v in status.items() if k not in common},
                             products={k: {'issued_at': r['issued_at'], 'filename': r['filename']} for k, r in records.items()},
                             endpoints=['/v1/weather', '/v1/spaceweather', '/v1/status'],
                             version='0.3.1')
        if path == '/v1/weather':
            county = query.get('county', ['all'])[0].lower()
            if county != 'all' and county not in COUNTIES:
                return 400, {'error': 'county must be all or one of the 17 NWS Detroit/Pontiac counties'}
            if 'ZFPDTX' not in records:
                return 503, dict(common, error='No validated DTX forecast indexed yet')
            result = age_fields(records['ZFPDTX'], now, 24)
            today = now.astimezone(LOCAL).date()
            dates = [today.isoformat(), (today + timedelta(days=1)).isoformat()]
            selected = {}
            for name, data in result.pop('counties').items():
                if county != 'all' and name != county:
                    continue
                data = age_fields(data, now, 24)
                data['unassigned_labels'] = [p['label'] for p in data.pop('unassigned_periods')]
                data['periods'] = [p for p in data['periods'] if p['date'] in dates]
                data['missing_dates'] = [d for d in dates if not any(p['date'] == d and not p['night'] for p in data['periods'])]
                selected[name] = data
            if county != 'all' and not selected:
                return 404, dict(common, error=f'No forecast section for {county} in the latest DTX bulletin')
            return 200, dict(common, **result, requested_dates=dates, counties=selected)
        if path in ('/v1/spaceweather', '/v1/aurora'):
            products = {k: age_fields(r, now, 36 if k == 'DAYIND' else (3 if r['kind'] == 'threshold_alert' else 18)) for k, r in records.items() if k != 'ZFPDTX'}
            if not products:
                return 503, dict(common, error='No validated SWPC bulletins indexed yet')
            thresholds = [r for r in products.values() if 'k_threshold' in r and not r['future_timestamp']]
            latest = max(thresholds, key=lambda r: r['threshold_reached_at'] or r['issued_at'], default=None)
            return 200, dict(common, products=products, latest_k_threshold_alert=latest,
                             current_bz_nt=None,
                             note='Threshold alerts are historical events, not a live Kp reading. Watches/warnings are latest received bulletins, not an active-alert database. Bz in DAYDIS is retrospective, not current.')
        return 404, {'error': 'Unknown endpoint'}


def serve(index, host, port, maps=None, cors="*"):
    class Handler(BaseHTTPRequestHandler):
        def end_headers(self):
            if cors:
                self.send_header('Access-Control-Allow-Origin', cors)
            self.send_header('X-Content-Type-Options', 'nosniff')
            super().end_headers()

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header('Access-Control-Allow-Methods', 'GET, HEAD, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'If-None-Match')
            self.end_headers()

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path in ('/maps', '/maps/'):
                self.send_response(302)
                self.send_header('Location', '../maps.html' if url.path.endswith('/') else 'maps.html')
                self.end_headers()
                return
            if url.path == '/maps.html':
                body = Path(__file__).with_name('gallery.html').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                if self.command != 'HEAD': self.wfile.write(body)
                return
            if maps and url.path.startswith('/v1/maps/'):
                match = re.fullmatch(r'/v1/maps/([0-9a-f]{24})/image', url.path)
                try:
                    if not match: raise FileNotFoundError()
                    stream, item = maps.open_image(match[1])
                except OSError:
                    self.send_error(404, 'Image unavailable')
                    return
                with stream:
                    if self.headers.get('If-None-Match') == item['etag']:
                        self.send_response(304)
                        self.send_header('ETag', item['etag'])
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header('Content-Type', item['mime_type'])
                    self.send_header('Content-Length', str(item['bytes']))
                    self.send_header('ETag', item['etag'])
                    self.send_header('Cache-Control', 'public, max-age=60')
                    self.end_headers()
                    if self.command != 'HEAD': shutil.copyfileobj(stream, self.wfile, 65536)
                return
            if maps and url.path == '/v1/maps':
                code, data = 200, maps.catalog(parse_qs(url.query))
            else:
                code, data = index.respond(url.path, parse_qs(url.query))
            if maps and url.path in ('/', '/v1/status'):
                with maps.lock: data['maps'] = dict(maps.status)
                data['endpoints'] += ['/v1/maps', '/maps.html']
            body = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            if self.command != 'HEAD': self.wfile.write(body)
        def log_message(self, fmt, *args):
            logging.info(fmt, *args)
    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=index.run, daemon=True).start()
    if maps: threading.Thread(target=maps.run, daemon=True).start()
    logging.info('Listening on %s:%s', host, port)
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--goes-root', default='/home/brad/goes/goes-hrit')
    parser.add_argument('--cors-origin', default='*', help='Allowed web origin; empty string disables CORS')
    parser.add_argument('--root', default='/home/brad/goes/goes-hrit/EMWIN/Text')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8097)
    parser.add_argument('--interval', type=int, default=120)
    args = parser.parse_args()
    if args.interval < 10:
        parser.error('--interval must be at least 10 seconds')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    serve(Index(args.root, args.interval), args.host, args.port, MapIndex(args.goes_root, args.interval), args.cors_origin)


if __name__ == '__main__':
    main()
