# Game-server integration — facts and design (researched Aug 2026)

Condensed from a sourced research pass; key URLs at the bottom. The bot's
server integration is built as **capability tiers**, negotiated at setup, with
every feature degrading gracefully when its tier is absent.

## Tiers

| Tier | Transport | Gives us | Needs from the community |
|---|---|---|---|
| 0 — none | — | Everything social (events, roles, teams, maps by hand) | Nothing |
| 1 — A2S | UDP 17777 poll | Server name, scenario, player count/max | Port usually already open; no credentials |
| 2 — RCON | UDP 19999, BattlEye RCon protocol | `#players` (names + ids), kick/ban, scenario `#restart`, shutdown | Config edit + restart, open port, password |
| 3 — feed | **Inbound** HTTPS POST | Live player positions, factions, Conflict base ownership → live map | PlayerTelemetry mod on the server + our endpoint URL/token |
| 4 — colocated | SSH/filesystem | Scenario/mod switching (config rewrite + process restart), log tailing | Shell access; genuinely can't be done remotely |

## Hard facts to honour in code

- **RCON is Bohemia's own game RCON** (config.json `rcon` block, default port
  19999, UDP, BattlEye RCon *wire protocol*) — distinct from the BattlEye
  anti-cheat's RCon in `BEServer_x64.cfg`. `permission: admin|monitor`;
  monitor gets `#players` only. `blacklist`/`whitelist` are COMMAND lists,
  not IP lists.
- Commands: `#players`, `#kick <playerId>`, `#ban create|remove|list`,
  `#restart` (current scenario only, keeps clients), `#shutdown`.
  **There is no say/broadcast** — never promise in-game announcements via RCON.
- Slots: `maxClients` ≤ 16, no client disconnect in the protocol — dead
  connections squat their slot 30–45 s. Hold ONE long-lived connection with
  keepalives; send BI's custom `@logout` on shutdown (added 1.2.1).
- Python: `berconpy` v3+ (v3 required for non-Arma-3 games). Small project —
  be prepared to vendor. Send raw `#...` strings, parse `#players` ourselves.
- **A2S**: standard Valve protocol, `python-a2s` works. Trust `A2S_INFO`
  (name, map, counts); player *names* via `A2S_PLAYER` are unverified for
  Reforger — get names from RCON.
- **Scenario/mod changes need a config rewrite + process restart** (config is
  read at startup only; no mission rotation exists). Tier 4 only.
- **PlayerTelemetry mod** (Workshop `1CDFD252B4101366`, APL-SA — forkable):
  POSTs a JSON snapshot of players (name, UUID, faction, position,
  alive/vehicle) and Conflict bases (owner, position, flags) to any HTTPS
  endpoint with a bearer token, default every 5 s. This is the live-map feed.
  Schema is young (~2k downloads) — validate defensively, version-tolerant.
- Server Admin Tools (1.87M downloads) exposes nothing externally and its
  licence forbids adaptation — coexist, don't integrate.

## Terrain / coordinates (bake these in)

| Terrain | Extent |
|---|---|
| Everon | 12 800 × 12 800 m |
| Arland | **4 096 × 4 096 m** (the circulating 5 942 figure is screenshot pixels, not metres) |
| Kolguyev | 12 800 × 12 800 m |

Enfusion: right-handed, +X east, +Y up, +Z north, world origin at the
**south-west corner**. Plot `(X, Z)` with a row flip for top-left images:
`px = X·P/S`, `py = P − Z·P/S`. Grid: 100 m squares, easting before
northing, 4/6/8/10-digit precision.

## Architectural decision (made): the feed listener

Tier 3 makes the bot a small web service as well as a Discord client: an
aiohttp listener (shares the runtime with `bot_health.py`), per-guild bearer
tokens stored in `server_links.secret`, TLS terminated by a reverse proxy in
front (ShedNet: nginx/caddy on the CT or the estate's existing proxy — an open
deployment question flagged to Jay Jay). The listener is only started when at
least one guild has the `feed` backend configured.

Ports summary for firewalls: game 2001/udp, A2S 17777/udp, RCON 19999/udp —
all UDP, all outbound from the bot except the Tier-3 inbound HTTPS.

## Key sources

- Official wiki mirror: https://github.com/burn0ut7/reforger-script-tools
- BattlEye RCon spec: https://www.battleye.com/downloads/BERConProtocol.txt
- berconpy: https://github.com/thegamecracks/berconpy
- PlayerTelemetry: https://reforger.armaplatform.com/workshop/1CDFD252B4101366
- A2S reference impl: https://github.com/dturovskiy/armactl
- Coordinate/tiles writeup: https://nick.recoil.org/articles/making-maps-without-getting-lost/
- Map asset extraction: https://github.com/nickludlam/EnfusionMapMaker
