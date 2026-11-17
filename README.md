# infobot — stage 2 / v0.5.3

A from-scratch MeshCore bot for tarratt.net infrastructure. It connects by TCP
to the companion at `10.0.0.10:5051`, identifies as `infobot`, and listens only
on `#infobot`. Scanner and transcript commands remain reserved for a later
stage. The separate GOES API project serves local EMWIN weather and received
weather maps at `10.0.0.10:8097`.

## Deploy

Update the GOES API first because this release adds the full Detroit/Pontiac
forecast area. Extract the corresponding API release into `~/infobot-goes`:

```bash
cd ~/infobot-goes
docker compose build goes-api
docker compose up -d --no-build --pull never goes-api
```

Then extract this archive into `~/infobot`:

```bash
cd ~/infobot
cp .env.example .env
# Adjust AIS_URL / ADSB_URL if your local service addresses differ.
docker compose build
docker compose run --rm infobot python bot.py --check-sources
```

The source check does not open the radio connection or transmit. It uses a
synthetic Huron County repeater location to exercise the local GOES, ADS-B and
AIS sources. Defaults are:

- `GOES_URL=http://10.0.0.10:8097`
- `AIS_URL=http://10.0.0.10:8100/api/ships_full.json`
- `ADSB_URL=http://44.31.120.1/tar1090/data/aircraft.json` (Hinton)

The ADS-B endpoint must return readsb/tar1090 JSON with `now` and an
`aircraft` list. AIS expects `ships` as a list of objects with `last_signal`
in seconds. No remote tracking feed is used.

Stop the old meshcore-bot service before connecting this bot; only one program
should own the companion TCP stream. Keep the GOES API running. Then inspect
and configure the companion:

```bash
docker compose run --rm infobot python bot.py --inspect-radio
docker compose run --rm infobot python bot.py --setup-radio
docker compose up -d --no-build --pull never
docker compose logs -f --tail=50
```

One-time setup names the companion `infobot`, creates `#infobot` in an empty
non-Public slot if needed, enables two-byte path hashes, and sets default flood
scope to `mi`. It refuses duplicate channels or a nonstandard channel key. It
does not change RF settings, existing channel keys, or contacts. Normal startup
checks the name, channel and path width without rewriting settings.

## Commands

| Command | Behavior |
|---|---|
| `about` | Version and tarratt.net HQ attribution |
| `help [command]` | Command list or command-specific help |
| `ping` | `Pong!` |
| `ping <IPv4\|IPv6\|hostname>` | Three bounded ICMP probes; reports packet loss and latency |
| `path` | One cleaned, numbered hop per line for the received route |
| `path aabb,ccdd` | Decode supplied hop hashes using local contacts |
| `hashsize` / `region` | One-line path-hash size and packet region tag |
| `wx [county]` | Today/tomorrow for the containing supported county; also a different nearest-center county within 50 mi. Outside coverage, nearest forecast only within 50 mi |
| `aurora` / `spacewx` | GOES Forecast Discussion (`DAYDIS`) forecast text |
| `sat [NORAD ID\|name[,name...]]` | Satellites overhead/rising soon, or next pass for selected satellites |
| `adsb` / `planes` | Fresh positioned aircraft from Hinton, nearest-first |
| `ais` / `boats` | Recently heard vessels from tarratt.net, nearest-first |
| `internet` | Check DNS reachability to several DNS root servers |
| `get <HTTP(S) URL>` | Return unformatted fetched text only when it fits one packet |
| `status` | Bot uptime, GOES state and local-source request status |
| `more` | Continue the last paginated response |
| `repeat <page>` | Resend a page without advancing the `more` cursor |

`path` also accepts `decode`, `route`, and `p`. Hop names are resolved from
local contacts; unknown hashes and collisions are marked instead of guessed.
`wx` accepts any of the 17 county slugs: Midland, Bay, Huron, Saginaw,
Tuscola, Sanilac, Shiawassee, Genesee, Lapeer, St. Clair (`stclair`),
Livingston, Oakland, Macomb, Washtenaw, Wayne, Lenawee, and Monroe. A new
command replaces that sender's saved pagination. Prefix `!` is optional.

## Per-request location

For `wx`, `sat`, `adsb`, and `ais`, the bot resolves hop 1 from the incoming
flood packet path against local repeater contacts and uses that repeater's
advertised latitude and longitude. Direct-route packets, and any packet with
no captured hop path, use Pinnebog as the reference location: the bot prefers
its advertised coordinates in local contacts, then falls back to Pinnebog's
fixed coordinates. A captured hop with a missing, ambiguous, or
coordinate-free contact still returns a clear location-unavailable response.
No user location database or internet geolocation is involved.

Weather county matching is a local estimate using county-center distances
inside an approximate DTX coverage envelope, not official county polygon
boundaries. The home county is shown first when it falls in the coverage
estimate; otherwise all 17 DTX counties are ordered by distance from hop 1.
The API may not have a forecast section for every county in every received
bulletin, and the bot reports missing sections. Full forecasts paginate as
needed. For a single forecast use `wx huron` or another county slug.
The API must have scanned within ten minutes, and the county forecast is
accepted for up to 24 hours from its issue time; each reply shows that time.

Satellite passes are calculated locally from cached TLEs. The bot refreshes
TLEs from CelesTrak only as needed (default cache interval 12 hours); if a
refresh fails, it can continue with a stale cache. Repeater elevation defaults
to `SAT_ALT_M` (200 m) where no elevation is available.

ADS-B reads the current local aircraft list, filters receiver and position age
(default 60 seconds), excludes aircraft on the ground and without coordinates,
and sorts by distance from hop 1. There is no history/radius filter. AIS reads
the locally received ships list without response caching, filters last-signal
age (default 30 minutes), and sorts by current listed position; records without
coordinates appear last. Heard time is not necessarily position time.

## Internet use and container permissions

Normal source HTTP requests are restricted to LAN/loopback addresses, with
five-second timeouts, a 5 MB response cap, redirects disabled, and environment
proxies disabled. GOES, AIS, ADS-B, radio, and contact data use local
infrastructure only.

The intentional network exceptions are `internet`, `get`, `sat` TLE refresh,
and `ping <target>`. `internet` sends DNS queries to several root-server IPs.
`get` fetches a user-supplied public HTTP(S) URL and returns it only when the
response fits one packet. `sat` may refresh TLEs from CelesTrak; orbit
calculations run locally. `ping <target>` resolves a hostname if needed and
sends at most three ICMP probes with a bounded timeout. It can reach any target
routable from the container, including LAN targets.

The Compose service drops all Linux capabilities, then grants only `NET_RAW`
for ICMP. `iputils-ping` is installed in the image. This is a container
capability, not an application-level destination firewall. Docker build needs
the base image and Python packages; runtime dependencies above are the only
intentional Internet-using bot features. `--preview 'ping <target>'` is refused
to avoid sending probes from a diagnostic command.

## Reply handling

- Reply bodies are limited to 130 UTF-8 bytes by default, leaving protocol room.
- Up to three parts are sent per command, with a six-second minimum gap.
- One bounded queue holds up to eight commands; queued work expires after
  90 seconds. A single worker sends replies.
- Sender packets older than two minutes or more than 30 seconds in the future
  are ignored. Keep radio and host clocks synchronized.
- User cooldown defaults to 15 seconds; fingerprints are kept for 24 hours in
  the Docker state volume. Names on MeshCore channels are not authenticated.
- Pagination lasts ten minutes. `repeat N` repeats a previously sent page and
  leaves the `more` cursor unchanged.
- Only `#infobot` is processed; Public, other channels and DMs are ignored.

## Offline APIs and web maps

The GOES API reads local SatDump EMWIN text, serves JSON, and indexes available
received weather maps without outbound requests. API routes, map gallery,
reverse-proxy example, and the 17-county forecast contract are documented in
the GOES API README. The map gallery is available at
`http://10.0.0.10:8097/maps.html` on the LAN.

The bot does not use online weather fallbacks, an online LLM, cloud telemetry,
external flight lookups, or online location lookup. There is no scanner or
transcript integration in this release.

## Local diagnostics

```bash
# Check local feeds without connecting to the radio or transmitting
docker compose run --rm infobot python bot.py --check-sources
# Format a command response with a synthetic Huron County location
docker compose run --rm infobot python bot.py --preview 'wx huron'
```

No Docker build, physical radio session, or live AIS/ADS-B validation was
performed for this release. The source check above is available for deployment.

## Rollback and offline image transfer

Do not run both bots against the same companion. For an offline host, build on
a compatible Linux architecture and transfer both Docker images and projects:

```bash
docker save infobot:0.5.3 -o infobot-image.tar
# destination:
docker load -i infobot-image.tar
docker compose up -d --no-build --pull never
```

To roll back, stop infobot, restore prior radio settings if needed, and restart
the old bot. The state volume is retained.

## Release notes

Version 0.5.7: `wx` uses bundled county boundaries to select the repeater's actual
county and an optional nearby county-center forecast, with a 50-mile coverage cutoff.
The bundled land-only county outlines and internal points are from U.S. Census Bureau TIGER/Line 2024;
area-water polygons are removed so water locations use the nearest-center fallback.
Version 0.5.6: `aurora` / `spacewx` returns the GOES `DAYDIS` forecast discussion,
keeping NOAA's forecast wording and omitting the 24-hour summaries and assistant-added commentary.
Version 0.5.5: `aurora` / `spacewx` condenses the GOES daily indices bulletin
into a short solar and geomagnetic data line.
Version 0.5.4: `aurora` / `spacewx` prints the newest GOES-received bulletin
verbatim, without extracting or interpreting K-index or Bz statements.
Version 0.5.3: accepts DTX county forecasts up to 24 hours old (with issue time
shown), replacing the overly strict 18-hour cutoff.
Version 0.5.2: requests with no captured hop path also use Pinnebog, even when
the packet metadata labels the route as flood.
Version 0.5.1: direct-route requests use Pinnebog as their location, preferring
its local advertised coordinates and using a fixed fallback point otherwise.
Version 0.5.0: resolves per-request hop-1 repeater location for weather,
satellite passes, ADS-B and AIS; adds all 17 Detroit/Pontiac counties,
nearest-first aircraft and vessel ordering, and bounded ICMP ping with Docker
`NET_RAW` capability. Updates the GOES API to version 0.3.0 for the expanded
county list.
# infobot
