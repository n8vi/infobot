"""Local received raster catalog. No maps, tiles, or metadata fetched online."""
import hashlib
import mimetypes
import os
from pathlib import Path
import re
import threading
import time
from datetime import datetime, timezone

EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
TIMESTAMP = re.compile(r'(?<!\d)(?:20\d{6}T?\d{6}Z?|20\d{2}[-_]\d{2}[-_]\d{2}(?:[T _-]\d{2}[-_:]\d{2}(?:[-_:]\d{2})?Z?)?)(?!\d)')


def group_key(relative):
    parts = list(relative.parts)
    if parts[0].upper() == 'EMWIN':
        # SatDump's EMWIN delivery prefix varies; the final product token is stable.
        m = re.search(r'_C_KWIN_\d{14}_.*?-\d-([^.]+)', parts[-1], re.I)
        if m:
            parts[-1] = m[1] + relative.suffix.lower()
    parts = [TIMESTAMP.sub('', part).strip(' _-') for part in parts]
    return '/'.join(part for part in parts if part)


def image_type(header):
    if header.startswith(b'\x89PNG\r\n\x1a\n'): return 'image/png'
    if header.startswith(b'\xff\xd8\xff'): return 'image/jpeg'
    if header.startswith((b'GIF87a', b'GIF89a')): return 'image/gif'
    if header[:4] == b'RIFF' and header[8:12] == b'WEBP': return 'image/webp'
    return None


class MapIndex:
    def __init__(self, root, interval=120):
        self.root = Path(root).resolve()
        self.interval = interval
        self.lock = threading.Lock()
        self.items = {}
        self.status = {'ready': False, 'error': None, 'last_scan_at': None}

    def scan(self):
        start = time.monotonic()
        newest = {}
        files = 0
        errors = []
        def onerror(error):
            raise error
        for branch in ('EMWIN', 'IMAGES'):
            root = self.root / branch
            if not root.is_dir():
                continue
            for directory, dirs, names in os.walk(root, onerror=onerror):
                # Text alone contains nearly half a million files on Euclid.
                dirs[:] = [d for d in dirs if d.lower() != 'text' and not (Path(directory) / d).is_symlink()]
                for name in names:
                    p = Path(directory) / name
                    if p.suffix.lower() not in EXTENSIONS or p.is_symlink(): continue
                    files += 1
                    try:
                        st = p.stat()
                        if st.st_size <= 0 or st.st_size > 128 * 1024 * 1024 or time.time() - st.st_mtime < 3: continue
                        relative = p.relative_to(self.root)
                        key = group_key(relative)
                        # Prefer embedded acquisition/delivery timestamp to mtime.
                        stamp = re.search(r'_C_KWIN_(\d{14})_', name, re.I)
                        stamps = TIMESTAMP.findall(str(relative))
                        embedded = stamp[1] if stamp else (stamps[-1] if stamps else None)
                        timestamp = None
                        if embedded:
                            digits = re.sub(r'\D', '', embedded)
                            for fmt, length in [('%Y%m%d%H%M%S', 14), ('%Y%m%d%H%M', 12), ('%Y%m%d', 8)]:
                                if len(digits) == length:
                                    try: timestamp = datetime.strptime(digits, fmt).replace(tzinfo=timezone.utc).timestamp()
                                    except ValueError: pass
                                    break
                        order = timestamp if timestamp is not None else st.st_mtime
                        if key in newest and order <= newest[key]['sort_time']: continue
                        with p.open('rb') as stream:
                            mime = image_type(stream.read(16))
                        if not mime: continue
                        ident = hashlib.sha256(key.encode()).hexdigest()[:24]
                        newest[key] = {'id': ident, 'product': key, 'source': branch,
                            'filename': name, 'relative_path': str(relative),
                            'file_mtime': datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                            'image_time': datetime.fromtimestamp(order, timezone.utc).isoformat(),
                            'time_basis': 'filename' if timestamp is not None else 'file_mtime',
                            'bytes': st.st_size, 'mime_type': mime,
                            'image_url': f'/v1/maps/{ident}/image', 'sort_time': order,
                            'etag': '"' + hashlib.sha256(f'{relative}:{st.st_mtime_ns}:{st.st_size}'.encode()).hexdigest() + '"'}
                    except OSError as error:
                        errors.append(str(error))
        with self.lock:
            self.items = {item['id']: item for item in newest.values()}
            self.status = {'ready': True, 'error': None, 'last_scan_at': datetime.now(timezone.utc).isoformat(),
                           'scan_seconds': round(time.monotonic()-start, 3), 'files_examined': files,
                           'product_count': len(newest), 'file_errors': errors[:10]}

    def run(self):
        while True:
            try:
                if not self.root.is_dir(): raise OSError(f'Missing GOES root: {self.root}')
                self.scan()
            except Exception as error:
                with self.lock: self.status['error'] = str(error)
            time.sleep(self.interval)

    def catalog(self, query):
        with self.lock:
            items = [dict(x) for x in self.items.values()]
            status = dict(self.status)
        q = query.get('q', [''])[0].lower()
        source = query.get('source', [''])[0].upper()
        result = []
        for item in sorted(items, key=lambda x: (x['source'], x['product'])):
            if q not in item['product'].lower() or (source and source != item['source']): continue
            item['age_seconds'] = round(time.time() - item.pop('sort_time'))
            item['older_than_24h'] = item['age_seconds'] > 86400
            item.pop('etag')
            result.append(item)
        return dict(status, maps=result, note='Received rasters, not georeferenced map tiles. Filename times are not guaranteed issue or observation times; file_mtime is a fallback. Product grouping is inferred from paths.')

    def open_image(self, ident):
        with self.lock:
            item = dict(self.items.get(ident, {}))
        if not item: raise FileNotFoundError('Unknown image ID')
        path = (self.root / item['relative_path']).resolve()
        if not path.is_relative_to(self.root) or path.is_symlink(): raise FileNotFoundError('Invalid image path')
        stream = path.open('rb')
        st = os.fstat(stream.fileno())
        if not image_type(stream.read(16)) or st.st_size > 128*1024*1024:
            stream.close()
            raise FileNotFoundError('Image is unavailable')
        stream.seek(0)
        item['bytes'] = st.st_size
        item['etag'] = '"' + hashlib.sha256(f'{path}:{st.st_mtime_ns}:{st.st_size}'.encode()).hexdigest() + '"'
        return stream, item
