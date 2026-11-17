# Infobot GOES API + received weather maps — version 0.3.1

Python 3.10+ standard library only. No pip, Docker, internet APIs, external
models, API keys, or outbound network requests. Reads local SatDump EMWIN
text files and serves JSON on your LAN. Does not modify received files or
connect to the MeshCore radio. System clock and installed timezone data
must be correct; neither requires an internet connection at runtime.

## Docker Compose deployment (recommended)

Extract into ~/infobot-goes. Configure the UID/GID to match the reader of
SatDump files. The Compose service reads the entire GOES root read-only,
including EMWIN images and IMAGES, and reads the host timezone database.
It writes nothing to either mount. No pip or apt steps are used.

```bash
cd ~/infobot-goes
cp .env.example .env
# Edit .env if needed. Check the expected UID/GID:
id brad
# If the previous native API service was installed, stop it to release 8097:
sudo systemctl disable --now infobot-goes.service
# Build once, then start (do not run the native install.sh as well):
docker compose build
docker compose up -d --no-build --pull never
docker compose logs --tail=50
```

Skip the systemctl command if that service was never installed. If your user
cannot access Docker, prefix docker commands with sudo.

The initial build needs python:3.12-slim-bookworm available locally, or access
to its registry to obtain it. After building, runtime and restarts need no
internet. `build.network: none` disables network for build RUN steps; it does
not eliminate the initial base-image requirement. To deploy on an already
air-gapped host, build on a compatible Linux architecture elsewhere and use:

```bash
docker save infobot-goes:0.3.1 -o infobot-goes-image.tar
# Transfer image tar + project to the offline host, then:
docker load -i infobot-goes-image.tar
docker compose up -d --no-build --pull never
```

No application code makes outbound requests. The Docker bridge is ordinary
networking, not an enforced egress firewall. Healthcheck probes the service
itself; inspect /v1/status for source/index readiness.

## Weather maps for web pages

Open http://10.0.0.10:8097/maps.html for the self-contained, embeddable gallery.
It uses no external JS, fonts, tiles, CDN, or map service and refreshes every
120 seconds. Images are lazy-loaded and full size is available on click.

- GET /v1/maps: latest received raster per inferred product.
- GET /v1/maps?q=radar: case-insensitive product/path filter.
- GET /v1/maps?source=EMWIN: received EMWIN weather charts/images.
- GET /v1/maps?source=IMAGES: decoded GOES satellite imagery.
- GET /v1/maps/<id>/image: stable latest-image URL; supports HEAD, ETag/304,
  and a 60-second browser cache. Use the actual IDs returned by the catalog.

PNG, JPEG, GIF and WebP are supported, including animated GIFs as received.
The API does not invent charts, crop geographic regions, reproject rasters,
or provide XYZ tiles. Charts are whatever Euclid actually receives; there
is no guarantee that each chart covers Michigan. There are no live-image
samples from Euclid yet, so product grouping uses filename conventions and
needs live verification. Timestamp-only directories are removed from group
keys; EMWIN delivery prefixes are removed while retaining the product token.
No frame-history/animation generator or thumbnail generation is included.
The gallery downloads original images, which may be large full-disk rasters.

Each catalog record includes product, filename, relative_path, source,
image_url, bytes, image_time, time_basis, file_mtime, and age_seconds. A filename
timestamp can mean acquisition or delivery time, not necessarily issue time.
If unavailable, image_time falls back to mtime and says so explicitly.
older_than_24h is an age indicator, not an official product expiry rule.
Symlinks, non-raster files and files over 128 MiB are excluded. Files modified
in the past three seconds are retried next scan; this is a stability heuristic,
not an atomic-write guarantee. A complete scan enumerates the image tree,
skipping EMWIN/Text. Missing EMWIN/IMAGES branches simply yield no images.

CORS_ORIGIN defaults to * for read-only browser fetches and canvas use without
credentials. Set it to an exact origin (e.g. https://thumbmesh.com) or empty to
disable CORS. It is not authentication. For public HTTPS web pages, proxy the
service through your own HTTPS server; browsers may block HTTPS-to-LAN HTTP
requests despite CORS, and remote visitors cannot reach your private LAN.

Example nginx location on a host that can reach Euclid:

```nginx
location /infobot/ {
    proxy_pass http://10.0.0.10:8097/;
}
```

Embed the gallery through that same-origin proxy:

```html
<iframe src="/infobot/maps.html" title="Locally received weather maps"
        width="100%" height="700" style="border:0" loading="lazy"></iframe>
```

Or use JSON directly in your own page:

```javascript
const base = '/infobot/';
const response = await fetch(base + 'v1/maps?source=EMWIN');
if (!response.ok) throw new Error(`HTTP ${response.status}`);
const catalog = await response.json();
for (const map of catalog.maps) {
  const image = document.createElement('img');
  image.src = base + map.image_url.replace(/^\//, '');
  image.alt = map.product;
  image.loading = 'lazy';
  document.querySelector('#weather-maps').append(image);
}
```

Product image_url paths are relative to the API server root; prepend your
proxy prefix as above. Keep that proxy on your LAN for fully offline website
use; a public website can proxy it too, but reaching the public website itself
naturally depends on the client's connectivity.

## Optional native install on Euclid

Extract this archive into /home/brad, then:

```bash
cd ~/infobot-goes
python3 -m unittest -v
sudo bash install.sh
curl -fsS http://10.0.0.10:8097/v1/status | python3 -m json.tool
curl -fsS 'http://10.0.0.10:8097/v1/weather?county=huron' | python3 -m json.tool
curl -fsS http://10.0.0.10:8097/v1/spaceweather | python3 -m json.tool
```

The listener starts immediately; weather/spaceweather return 503 until the
initial index has found usable input. Wait for status.ready=true. Startup
index duration depends on the archive and disk. Refreshes happen every 120
seconds AFTER the previous scan finishes, in a background thread.

The included service runs as brad and binds to 10.0.0.10:8097. Port 8097 is
proposed, not confirmed free on Euclid. It installs code in /opt/infobot-goes
and the unit in /etc/systemd/system. No existing meshcore-bot, SatDump,
AIS, ADS-B, radio, or firewall configuration is changed. This is a trusted
LAN API with no authentication; don't expose it via your public proxy.

Foreground alternative (Ctrl-C to stop):

```bash
python3 goes_api.py --host 10.0.0.10 --port 8097
```

For troubleshooting:

```bash
journalctl -u infobot-goes -n 50 --no-pager
```

To stop/remove the service:

```bash
sudo systemctl disable --now infobot-goes
sudo rm /etc/systemd/system/infobot-goes.service
sudo systemctl daemon-reload
```

Code remains in /opt/infobot-goes for inspection or reuse.

## Inputs and indexing

Default root: /home/brad/goes/goes-hrit/EMWIN/Text

The index recursively enumerates filenames. It filters names before doing
stat or read calls, and retains only eight recent deliveries for each ZFPDTX
or KWNP product. This bounds content work even with 475,000 files. No archive
scan is performed by an HTTP request. Unchanged candidate files are cached
in memory. A restart rebuilds the index; no persistent database is needed.

Files modified in the last three seconds are skipped; changed-during-read
files are retried next scan. Malformed candidates are reported in status;
the last valid product stays available and ages naturally. Stable files
aren't guaranteed free of RF corruption: this parser checks required
structure, not every sentence. No missing prose is synthesized.

Ordering is by parsed bulletin issue time, with EMWIN delivery time as a
tiebreaker. A later delivery of an older bulletin cannot replace a newer
indexed bulletin. The eight-delivery shortlist means an extreme backlog of
replays on a cold start can hide an older delivery of the newest issue.
File mtime is exposed as file_mtime, NOT asserted to be reception time.
delivery_at is from the EMWIN filename, not a local reception measurement.

## API contract

- GET /v1/status: index readiness, latest completed scan, product filenames
  and issue times, scan duration, parse errors. HTTP 200 means the HTTP
  service responds; inspect ready and error for index health.
- GET /v1/weather: forecast sections from the 17-county NWS Detroit/Pontiac area.
  Add ?county=<slug> to select one: Midland, Bay, Huron, Saginaw, Tuscola,
  Sanilac, Shiawassee, Genesee, Lapeer, St. Clair (`stclair`), Livingston,
  Oakland, Macomb, Washtenaw, Wayne, Lenawee, or Monroe. County sections
  present in the received bulletin are returned; absent sections are omitted
  from `all` and return 404 when selected explicitly.
- GET /v1/spaceweather (alias /v1/aurora): latest indexed SWPC bulletin per
  product code, verbatim text, parsed issue time and limited structured
  fields. Includes newest K-threshold alert and solar wind discussion.
- Unknown routes return 404, invalid county returns 400, absent products 503.

Times are ISO 8601 UTC; forecast dates are America/Detroit local dates.
Weather includes today's and tomorrow's day AND night periods when present.
It selects by dates derived from the bulletin issue time; yesterday's TODAY
is never relabeled today. Weekday and common relative labels are supported,
plus explicitly handled fixed-date holidays. Unknown/combined labels are
reported under unassigned_labels and excluded rather than guessed. This
first version has no full holiday calendar. missing_dates indicates absent
daytime forecasts; an evening bulletin may legitimately lack today's day.

Full NWS forecast prose is retained. Mesh-friendly shortening, pagination,
byte limits, and rate limiting belong to the phase-two bot adapter.

Staleness policies: 24 hours for DTX weather forecasts, 18 hours for
space-weather forecast/discussion bulletins, 36 hours for daily indices,
three hours for threshold alerts. These are
conservative display policies, NOT official validity or cancellation rules.
Each county has its own age and stale flag; top-level weather also carries
the bulletin's age. Future issue times more than five minutes ahead are
flagged. Consumers must inspect stale, future_timestamp, index_error,
last_scan_at, and missing_dates before presenting data as current.

A K-index threshold alert is an event, NOT a continuous current Kp reading.
An older ALTK05 can remain the latest received K=5 alert after conditions
fall. latest_k_threshold_alert means the newest threshold event of ANY
indexed level, not the highest level ever received. It is not sufficient
by itself to determine the current geomagnetic storm level.

Bz in DAYDIS is a past-24-hour summary. current_bz_nt is deliberately null.
The forecast discussion preserves Bz prose without converting its extrema
to live measurements. DAYTDF tables remain verbatim in this version; the
bot adapter can summarize the forecast while keeping observation/forecast
distinctions. DAYIND missing-value sentinels remain unparsed in raw text.

Watch and warning text is exposed as received. It does not establish that
a watch/warning remains active. No /v1/alerts endpoint is implemented yet:
local weather warning coverage requires VTEC/UGC expiry, cancellation,
replacement, and geographic tests. Treat it as a separate follow-up rather
than claiming old warning files are currently active.

## Validation and rollout

Seventeen unit tests cover the supplied bulletin structure with compact
fixtures transcribed from the Nov 7 sample; they are not a replay of the
entire archive. Also smoke-tested HTTP with temporary local fixture files.
Live Euclid installation and its actual archive still need verification.
Docker is unavailable in the development environment; the container build
and Compose deployment were not executed here. HTTP image streaming, CORS,
ETag, HEAD and the gallery were exercised using a native test server.

The full project scope remains:

1. Local GOES API; verify live output, then refine compact weather and
   space-weather summaries for radio replies.
2. A from-scratch bot named infobot in #infobot, TCP 10.0.0.10:5051.
   Commands: path and ping matching meshcore-bot, help, wx, aurora/spacewx,
   adsb/planes using Hinton, ais/boats using Euclid. Verify exact ping/path
   semantics and live AIS/ADS-B schemas before implementation. Add status;
   alerts only after lifecycle parsing. Keep runtime entirely local.
3. scanner [county] [number] and transcript [number] from the local backend
   behind scan.tarratt.net, including local transcription. Agree on event
   offset convention; proposed 0=latest and N=N events before latest.

Every source must degrade to unavailable/stale without internet fallback.
Do not require an online LLM for summaries. A brief dated, deterministic
summary is preferable for reliable mesh operation.
