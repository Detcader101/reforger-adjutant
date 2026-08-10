# Adjutant — Arma Reforger community bot

A Discord adjutant for Arma Reforger communities: server setup, ranked roles,
locked team channels, event organising, live maps, and optional dedicated-server
integration. Works fully Discord-only; game-server links are opt-in per guild.

## Product principles

1. **Optional everything.** Every feature is opt-in at `/setup`. A guild can run
   the full adjutant, or just the bits it wants. No feature assumes another is on,
   and none assume a game server exists.
2. **Sleek by default, no fluff on demand.** Commands are concise; embeds are
   clean; a "minimal mode" per guild strips decorative output.
3. **Hard to abuse.** Every mutating command is permission-gated by role rank.
   Rate limits on map edits and event spam. Abuse responses are suave, not
   snarky — the bot declines politely, logs the attempt, and never escalates.
4. **No information leaks.** Team channels are locked at the Discord permission
   layer, not just by convention. The bot never repeats content from one team's
   channels in another's, including in errors and logs surfaced to users.
   Where in-game comms would be more appropriate, the bot says so and nudges
   players to use them.
5. **Stiff upper lip, transparent about problems.** When something breaks or a
   dependency is missing, the bot states it plainly in-channel with what the
   user can do about it. Persona: a courteous British adjutant — dry, unflappable,
   brief. All user-facing strings go through `adjutant/voice.py` so tone stays
   consistent and is tunable in one place.

## Architecture

- Python 3.12, discord.py ≥ 2.4, slash commands only (guild-synced).
- `adjutant/` package, cogs under `adjutant/cogs/`, one feature per cog.
- SQLite (aiosqlite) at `data/adjutant.db`; migrations in `adjutant/migrations/`
  as numbered `.sql` files applied at startup.
- Secrets in `.env` (`DISCORD_TOKEN`); all per-guild settings live in the DB and
  are managed via `/setup` — no per-guild config files.
- Pure logic (rank math, expiry scheduling, coordinate transforms, permission
  matrices) lives in `adjutant/services/` as Discord-free functions/classes —
  that is the TDD surface. Cogs are thin adapters.
- Background work (role expiry, event reminders, server polling) runs on
  `discord.ext.tasks` loops owned by the relevant cog.

### Cogs (v1)

| Cog | Purpose |
|---|---|
| `setup` | Guided `/setup` wizard: pick features, scaffold categories/channels/roles, set admin roles. Also `/teardown` (guarded). |
| `roles` | Rank ladder + perma/temp/event role grants with DB-backed expiry. |
| `teams` | Create/destroy team units: role + locked category (text/voice), leak-safe permission overwrites. |
| `events` | Ops/events: create, announce, button signup, temp event role grant, reminders, teardown. |
| `map` | Pillow-rendered map messages with markers; rank-gated `/map mark|clear`; re-render + edit message in place. |
| `admin` | Moderation helpers, audit log channel, abuse/rate-limit responses. |
| `serverlink` | Optional game-server status/integration surface (see below). |

### GameServerLink

`adjutant/serverlink/base.py` defines the interface:
`status()`, `players()`, capability flags (`can_rcon`, `can_live_state`), and an
event hook for future live-state feeds (modded-client link).

Backends:
- `null` — always-on default; reports "no server linked" gracefully.
- `a2s` — Steam query: server name, player count, scenario. (Details per research.)
- `rcon` — BattlEye RCON where supported: player list, kick, say. (Details per research.)
- future: `feed` — websocket/HTTP push from a server-side Enfusion mod for live
  positions → map overlay.

A guild picks a backend in `/setup`; the bot behaves identically minus
capabilities when on `null`.

### Rank & permission model

- Configurable rank ladder per guild (default: military-flavoured, e.g.
  Recruit → Private → NCO → Officer → Command); each bot permission (map edit,
  event create, team manage, setup) maps to a minimum rank, overridable.
- Role grants: `perma` (until removed), `temp` (expiry timestamp, scheduler
  removes), `event` (bound to an event id, removed at teardown).
- Guild owner + configured admin roles bypass rank checks.

## Deploy (ShedNet pattern)

Mirrors tekken-bot: systemd unit `reforger-adjutant.service` under its own CT,
`/opt/reforger-adjutant`, update timer polling `origin/main` (~2 min) →
`git pull --ff-only`, reinstall requirements, stamp `BOT_GIT_SHA`, restart.
Files in `deploy/`.

## Out of scope for v1

Dashboards/web UI, cross-guild federation, stats/ELO, voice features.
