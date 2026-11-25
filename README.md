# infobot — v0.6.0

A MeshCore companion bot for tarratt.net. The bot connects over TCP, serves
configured hashtag channels and private messages, and replies with local GOES
weather, aircraft, vessel and route information. Satellite predictions run
locally using cached CelesTrak elements. No LLM or online weather fallback is
used. Scanner/transcript integration and active weather warnings are not implemented.

## Upgrade and deploy

This repository contains Infobot, the GOES API, and SearXNG. Run Compose from this directory;
there is no separate Compose project inside `infobot-goes/`.
Existing `.env` files work without edits: all existing keys retain their
meaning. Missing new keys use the defaults described below. Version 0.6.0
adds DMs by default; set `DM_ENABLED=false` to disable them. No additional
channels are enabled automatically.

```bash
cp .env.example .env  # first installation only; retain an existing .env on upgrade
# Adjust source addresses and GOES_ROOT to match your host.
docker compose config --quiet
docker compose build
docker compose pull searxng
docker compose up -d --no-build --pull never infobot-goes
docker compose run --rm infobot python bot.py --check-sources
```

Defaults are TCP `10.0.0.10:5051`, GOES `http://10.0.0.10:8097`, AIS
`http://10.0.0.10:8100/api/ships_full.json`, and ADS-B
`http://hinton/tar1090/data/aircraft.json`. The existing example `.env` uses
`http://44.31.120.1/tar1090/data/aircraft.json` for ADS-B; both addresses must
resolve/route to your local infrastructure.

Only one program may own the companion TCP connection. Stop the old bot
before running radio diagnostics or starting this service:

```bash
docker compose run --rm infobot python bot.py --inspect-radio
docker compose run --rm infobot python bot.py --setup-radio
docker compose up -d --no-build --pull never infobot
docker compose logs -f --tail=50
```

Setup creates configured hashtag channels in empty non-Public slots, names
the companion `infobot`, selects two-byte path hashes, and sets flood scope
`mi`. It refuses duplicate channels and nonstandard hashtag keys. It leaves
RF settings and existing channel keys unchanged. Normal startup validates
settings rather than rewriting them. Channel count is limited by companion
slots. DM delivery may reset an individual contact route to flood after missed
receipts; it does not clear the contact list.

## Channels, mentions and DMs

The default is `BOT_CHANNELS='#infobot=bare'`. Configure only channels where
the bot should respond. Quote this dotenv value because it starts with `#`:

```dotenv
BOT_CHANNELS='#infobot=bare,#weather=mention,#tools=either,#commands=bang'
DM_ENABLED=true
```

| Policy | Activation |
|---|---|
| `bare` | `wx`, `!wx`, `@[infobot] wx`; legacy `infobot: wx` also works |
| `bang` | `!wx` |
| `mention` | `@[infobot] wx` or `@[infobot]: wx` |
| `either` | `!wx` or `@[infobot] wx` |

Mentions use the literal MeshCore client syntax **`@[infobot]`**, including
brackets, case-insensitively. Unconfigured channels are ignored. Only standard
hashtag channels are supported; custom private channel keys are not configured
through this interface.

DMs accept bare commands and reply privately. The sender's six-byte prefix
must uniquely resolve to a full public key in companion contacts/pending
contacts; ambiguous or unknown senders are ignored. Only ordinary text DMs
are processed, not CLI/signed-room messages. DMs use their public key as identity;
channel display names are unauthenticated and can be impersonated.

Each channel has one sequential request queue, and each DM sender has another.
A reply batch finishes before the next request in that conversation. Other
conversations can make progress, including while one DM awaits a receipt or
one command waits on a data source. The shared radio serializes actual send
commands; packet order is preserved within each conversation.

## Discovery adverts

Normal startup sends a flood advert so clients can discover the bot for DMs.
Adverts repeat every four hours by default. Optional `ADVERT_INTERVAL` is in
seconds: `14400` by default, at least `60`, or `0` to disable. Existing `.env`
files need no changes. Setup, inspect, preview and source-check modes do not
send adverts.

Adverts share the channel transmission gap and radio arbitration, and use the
companion's configured flood scope. Reconnects retain the running schedule;
a process restart advertises again. A failed send is logged and retried after
60 seconds rather than waiting four hours. Successful radio acceptance is
logged, without claiming every client received the advert.

## Private delivery and radio pacing

Channel replies are sent once, without delivery confirmation. `TX_GAP`
(default six seconds) is the minimum gap between **channel** transmissions.
DMs are exempt from that artificial gap: the next DM packet is eligible as
soon as the destination acknowledges the previous one. All companion command/
response pairs are serialized, including background message fetching.

DM settings (all optional):

| Setting | Default | Meaning |
|---|---:|---|
| `DM_ACK_TIMEOUT` | `0` | Seconds per attempt; 0 uses 1.2× companion suggestion, bounded to 5–300 seconds |
| `DM_RETRIES` | `3` | Retransmissions after the initial send; allowed 0–3 (attempt field 0–3) |
| `DM_FLOOD_AFTER` | `2` | Reset this destination to flood before attempt index 2, after two unanswered sends |

Retries retain the text and original timestamp and increment the protocol
attempt field. A matching late receipt from an earlier attempt is accepted;
unrelated receipts are ignored. A raw RF ACK can also confirm a pending DM
when its first four wire-order bytes exactly match an expected receipt code.
This supports OpenHop instances that forward RX packets without a companion
receipt notification. Optional ACK extension bytes are ignored, following
MeshCore's four-byte receipt matching. Logs identify confirmation as
`source=raw RF ACK` or `source=companion receipt`. Identical reply text uses
separate timestamps across DM recipients, because the receipt hash does not
include the recipient key; this prevents one receipt completing two replies. The bot subscribes before sending so an early
receipt cannot be missed. A missing receipt can still mean the message arrived
and its receipt was lost; duplicate delivery remains possible.

Known routes are tried first. Flood fallback uses the companion's route reset;
route updates are refreshed after a confirmed delivery for subsequent packets.
If no receipt arrives within the retry budget, the batch stops at the failed
packet. `more` retries the failed packet and remaining batch before advancing
to later saved text. A new content command replaces that saved response.
A companion send error triggers reconnect rather than blind retries.

## Commands

| Command | Behavior |
|---|---|
| `about`, `help [command]` | Version, command list and help |
| `ping` | `Pong!` |
| `ping <IP or hostname>` | Three bounded ICMP probes |
| `path [hex]` | Numbered repeater hops, resolving local contacts; aliases `decode`, `route`, `p` |
| `hashsize`, `region` | Path hash width and region tag |
| `wx [county or ZIP]` | Today/tomorrow forecasts for any Michigan county or five-digit U.S. ZIP in the bundled lookup |
| `aurora`, `spacewx` | Fresh GOES DAYDIS forecast wording, without retrospective summaries |
| `sat [NORAD ID or name[,name...]]` | Complete current/next amateur satellite passes |
| `adsb [registration, ICAO address, or flight ID]`, `planes` | Search recent local aircraft records or list nearby airborne aircraft |
| `ais [MMSI, IMO, vessel name, or radio call sign]`, `boats` | Search recently heard vessels or list them nearest first |
| `internet` | DNS reachability probes to root servers |
| `bot, <question>` | Search the public web and return a short answer |
| `get <HTTP(S) URL>` | Public fetched text, only if it fits one packet |
| `status` | Uptime, GOES index health and conversation's source-request status |
| `more` | Next saved reply batch; on failed DM delivery resumes the failed packet |
| `repeat N` | Resend packet N of the **latest batch**, without advancing `more` |

Replies default to 130 UTF-8 bytes, at most three packets per command.
Numbering restarts within each batch; `repeat` behavior is unchanged.
Saved text expires ten minutes after creation. Pagination and cooldowns are
scoped to channel+sender or to the DM public key, so another conversation
does not overwrite your response. A new content command replaces saved text.

Packets older than 120 seconds or over 30 seconds in the future are ignored.
Keep host and companion clocks synchronized. Duplicate fingerprints persist
24 hours in the state volume; cooldowns and pagination are in memory.

## Locations and freshness

For location-dependent commands, captured flood paths resolve hop 1 against
local repeater contacts. Path and location lookups ignore repeaters whose last
advert is at least 48 hours old. Contacts without a usable advert timestamp
remain eligible, and this filter keeps the contact records on the radio.
Unknown/colliding hashes and missing coordinates are
reported. Radio logs must match message identity; timestamp proximity alone
is insufficient. Pathless/direct channel requests use advertised Pinnebog coordinates
when unambiguous, otherwise the bundled Pinnebog point. Private companion
messages supply hop count but no correlated raw hop hashes. For DMs the bot
uses the **last** repeater in the sender's learned outbound return route,
which is the repeater nearest that sender, rather than always using Pinnebog.
Before every location-dependent DM command, the bot queries the sender's
full public key directly from the companion. The returned record replaces the
confirmed cache, removes pending duplicates, and is copied into the request's
contact snapshot. Confirmed contacts always take precedence over pending
advertisements. A failed lookup reports location unavailable instead of using
a stale route. INFO logs include the fetched hop count, hash mode and path.
Weather labels this as a DM route estimate: cached return paths may be stale
or differ from the incoming route. Missing, malformed, or ambiguous return
routes report location unavailable; `wx <county>` still works. A confirmed
zero-hop return route retains the local Pinnebog fallback. No internet geolocation is used.

Bare `wx` uses bundled land-only Census TIGER/Line 2024 county polygons for the
17 Detroit/Pontiac counties: Midland, Bay, Huron, Saginaw, Tuscola, Sanilac,
Shiawassee, Genesee, Lapeer, St. Clair (`stclair`), Livingston, Oakland,
Macomb, Washtenaw, Wayne, Lenawee, Monroe. The containing county is first;
a different nearest-center county is added if its center is within 50 miles.
Outside/border/water points use the nearest center only when a supported
county boundary is within 50 miles.

Explicit requests such as `wx Marquette` or `wx 02108` work without a requester
location. The bot resolves ZIPs and Michigan county names using bundled
geography and retrieves forecasts from locally received GOES-19 EMWIN zone
bulletins. If a county has multiple forecast zones, the reply identifies each
zone. The bot reports unavailable forecasts when a place has no usable
local bulletin or county mapping.
GOES scans must be within ten minutes; county forecasts may be up to 24 hours
old. Space-weather discussions may be up to 18 hours old. Both reject index
errors and issue times over five minutes in the future.

Satellite passes show rise direction, full-pass maximum elevation, and set
direction, plus rise/set times in Eastern time. Directions use the 16-point
true-north compass. The default lists passes up now or rising within an hour,
but calculates each through setting, including peaks outside that hour.
Underway passes include the original rise and full peak; a 48-hour search
limit is stated when a boundary cannot be found. `SAT_ALT_M` defaults to 200m.
Elements refresh at `SAT_TLE_REFRESH_HOURS` (12h); failed refreshes can use the
cache, with a warning when elements are over three days old.

ADS-B expects readsb/tar1090 `now` and `aircraft[]`. Bare `adsb` lists
airborne records with positions within the age limit (default 60s), sorted
by distance. AIS expects object records in `ships[]`, with `last_signal` age in
seconds (default 30 minutes). AIS heard time is not necessarily position time;
missing coordinates sort last. AIS is fetched without response caching.
Explicit searches use case-insensitive exact matches and work without
requester location. Identifiers come from the local feeds when present.
`ais IMO 1234567` also accepts the numeric IMO without the prefix.

## Limits, diagnostics and health

Optional limits: `COMMAND_TIMEOUT=45` seconds, `COMMAND_WORKERS=4`,
`CONVERSATION_LIMIT=32`, `QUEUE_LIMIT=8` waiting requests per conversation,
and `GLOBAL_QUEUE_LIMIT=64` waiting requests overall. Queued requests expire
at 90 seconds. At capacity, new requests are dropped. Idle conversation workers
can be evicted; saved pagination remains available until expiry.

Each source command runs in a spawned process; small local commands such as
`ping` without a target and `help` execute directly. Its deadline includes waiting
for an execution slot; timed-out or cancelled work is terminated and reaped.
This bounds DNS, HTTP and orbit computation as well as the async wait.

```bash
python -B run_tests.py                 # both suites, including emulated radio
python bot.py --preview 'wx huron'     # no radio connection
python bot.py --check-sources          # structured source schema/freshness checks
```

Preview and source diagnostics may access the network. Preview refuses
`ping <target>` and `bot, <question>`. They do not transmit on MeshCore. The bot's `/state/health.json`
reports radio connection, worker state, and last command error. Its Docker
health check requires recent radio/worker health. GOES `/healthz` requires a
completed recent index with products, no index error, and a recent healthy map
scan; `/v1/status` remains a diagnostic endpoint returning HTTP 200.
Docker does not restart a container merely because it is unhealthy.

Accepted request text, sender, command arguments, and every transmitted reply
packet are logged at INFO level, including DMs. Channel logs distinguish radio
acceptance from unconfirmed delivery; DM logs include attempts, route, receipt
confirmation, timeouts, and flood fallback. View them with
`docker compose logs -f --tail=50 infobot`.

Each private-message event is logged as `RX DM event before admission` before
checking DM enablement, type, timestamp, text, sender or command. This includes
the sender prefix, message type, sender and host timestamps, message age, text
and reported path metadata. Rejected events get a `DROP DM event` with the
specific reason; cooldown, duplicate and capacity drops are also logged.
The later `RX ('dm', ...)` line means a request was actually admitted to a queue.
If OpenHop observes a packet but no pre-admission DM event appears, investigate
its decryption, queue and companion forwarding rather than bot admission.

Receipt diagnostics are also logged at INFO without additional configuration:

- `DM ACK wait`: expected codes (including previous attempts), message timestamp,
  companion-suggested timeout and the actual configured timeout.
- `RX ACK event`: every companion receipt notification, including unmatched or
  late receipts with no active send, round-trip milliseconds and pending peers.
  An early event can show an empty expected list while the send result is pending;
  the existing receipt-confirmed line records a later successful match.
- `RX raw ACK packet`: an RF ACK heard in the radio RX log, its wire-order code,
  payload hex and route metadata. Its first four bytes must match an active
  expected code before it can confirm delivery; unrelated raw ACKs are ignored.
- `RX encrypted PATH packet`: a path return was heard; encrypted contents cannot
  be assumed to contain a matching receipt.
- `RX PATH_UPDATE event`: the companion reports a changed contact route.
- `DM ACK tracking ended`: whether the pending response was confirmed before
  tracking ended, with its expected codes.

Raw packets are available only when the companion supplies RX log events.
A raw ACK without a companion receipt event points to a different failure
stage than a receipt event whose code differs from the pending codes. These
diagnostic logs do not change retry counts or timeout settings. The raw-ACK
fallback uses the same active expected-code set as companion receipts; neither
a path update nor an encrypted path packet is accepted as confirmation.

## Network and containers

Normal GOES/AIS/ADS-B sources allow LAN/loopback and local AMPRNet
`44.31.120.0/24` only. Validated DNS addresses are pinned for HTTP connections;
redirects and environment proxies are disabled, with a 5MB response cap.
`internet`, `get`, satellite element refresh, targeted `ping`, and `bot, <question>` intentionally
use the network. `get` pins validated public addresses and rejects private/local
AMPRNet destinations. `ping` may reach any routable target, including LAN.

Containers run without root, with read-only filesystems. Docker builds set
application files readable explicitly, independent of checkout permissions. The bot receives
`NET_RAW` for ICMP; this is not a destination firewall. Runtime state is in the
`state` volume. Builds need the base images, apt and Python packages.
The GOES service reads SatDump data read-only and makes no outbound requests.
See [the GOES README](infobot-goes/README.md) for API and map details.

Compose starts SearXNG automatically when you start Infobot.
SearXNG listens on the Compose network at `http://searxng:8080` and has no
published host port. Infobot searches using the question and sends the result
snippets to llama.cpp at `http://10.0.0.10:8080` for a single chat completion.
The search request gets up to five seconds within the overall
`COMMAND_TIMEOUT` deadline (45 seconds by default).
Configure the service URLs with `SEARXNG_URL` and `LLAMA_URL`. The model has no
tools, and Infobot never opens result links. Questions from channels and DMs
go to public search engines. Keep private information out of these queries.

For an offline deployment, transfer the Infobot and GOES images from a compatible
Linux architecture. Start those two services with `--no-deps` to skip SearXNG.

```bash
docker save infobot:0.6.0 infobot-goes:0.3.1 -o infobot-images.tar
# On the destination, with this repository and its .env:
docker load -i infobot-images.tar
docker compose up -d --no-build --pull never --no-deps infobot infobot-goes
```

Stop infobot before restoring another bot to the companion. Retain the state
volume. Physical-radio timing, RF route recovery, live feeds, and the host's
SatDump archive still require deployment validation.
