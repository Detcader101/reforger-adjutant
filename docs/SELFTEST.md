# Self-test harness

`adjutant/selftest.py` is a **live** end-to-end test: it logs into Discord as
the real bot, loads the real cogs, and drives them against a real Discord
guild — no mocks, no human clicking buttons. It exists because the cog code
had never been executed against real Discord before this harness; unit tests
under `tests/` cover the Discord-free `services/` logic, but nothing had run
the actual Discord API calls (role hierarchy, channel overwrites, message
components, attachment uploads, ...) until now.

## Running it

```
cmd.exe /c ".venv\Scripts\python.exe -m adjutant.selftest > selftest.log 2>&1"; tr -d '\r' < selftest.log
```

(Windows-side Python under WSL — see the repo's environment notes for why.
The `tr -d '\r'` strips the CRLF line endings Windows Python writes so the
log reads cleanly in a Unix shell.)

Flags:

- `--guild <id>` — target guild id. Defaults to the first entry in
  `DEV_GUILD_IDS` from `.env`.
- `--keep` — skip cleanup, leaving everything the harness created in place
  for manual inspection in Discord / the selftest DB.
- `--db <path>` — override the isolated database path (default
  `data/selftest.db`).

Exit code is `0` if every check passed, `1` otherwise — safe to wire into a
CI gate or a pre-deploy check once the guild permission prerequisites below
are met.

### Prerequisites

- A working `.env` with `DISCORD_TOKEN` and `DEV_GUILD_IDS` (containing the
  scratch guild's id).
- The bot invited to that guild with, at minimum, **Manage Roles** and
  **Manage Channels**.
- Nothing else — the harness creates and tears down every Discord object it
  needs (a category, a few channels, a handful of roles).

## Isolation guarantees

- **Database**: the harness forces `ADJUTANT_DB=data/selftest.db` before
  anything else runs (`os.environ.setdefault` at import time, then an
  explicit override from `main()`), so it never touches
  `data/adjutant.db` even if `.env` points there. `data/` is already
  gitignored.
- **Discord objects**: every channel and role the harness creates is named
  with a `selftest-` prefix (team/event roles keep their cogs' natural
  naming convention, e.g. `Team selftest-team`, but are always created and
  tracked by this run only). Before deleting anything, the harness asserts
  the object's id is in its own creation registry (`Harness.created_role_ids`
  / `created_channel_ids`) — it will never touch a role or channel it didn't
  create itself.
- **Members**: the only "test subject" ever granted/revoked a role is the
  bot's own guild member. No human member is ever touched.
- **Channels**: everything posts into a dedicated `selftest-harness`
  category and its child channels (`selftest-events`, `selftest-map`,
  `selftest-audit`), created fresh each run and deleted at the end. Nothing
  is ever posted into a pre-existing channel.

## What each check covers

Checks run in sequence; one failing check does not stop the rest. Each is an
independent async function returning `(name, passed, detail, duration)`.

| # | Check | What it exercises |
|---|---|---|
| 1 | `connectivity` | Logs in, resolves the guild, reports the bot's top role position and whether it holds Manage Roles/Manage Channels. This is checked first and reported loudly because a missing permission or too-low role position is the most likely real-world setup mistake, and it explains almost every downstream failure if wrong. |
| 2 | `database` | Confirms `schema_version` in the isolated DB matches the highest migration file present under `adjutant/migrations/`. |
| 3 | `guild_config` | Calls `cogs/setup.py`'s `_save_guild_config` (the same function `/setup` uses) to write a `guilds` row, then reads it back and verifies it round-tripped. |
| 4 | `rank_ladder` | Calls `_create_default_ladder` (same as `/setup`'s default-ladder option) to create the five default rank roles in the real guild, verifies the Discord roles exist and the `ranks` table matches, then cleans up. |
| 5 | `role_grant_lifecycle` | Grants a temp role to the bot's own member with an already-past `expires_at`, then invokes `RolesCog.expire_grants()` directly — the exact coroutine the 60-second background loop calls (bypassing the loop's scheduling, not reimplementing its logic) — and verifies the role and the DB row are both gone afterward. |
| 6 | `team_lifecycle` | Creates a team (role + locked category + text/voice channels + overwrites), verifies `@everyone` is denied `view_channel` on the category and the team role is allowed, verifies the `teams` DB row, then disbands via the shared `_disband_team` function and verifies everything — role, category, both channels, DB row — is gone. |
| 7 | `event_lifecycle` | Creates an event via `services/events.py`, posts the real announce embed with the persistent `SignupView` attached, verifies the message carries components, simulates a signup by calling the exact service functions the button callback calls (`add_signup` + `grants_service.record_grant`), then runs `EventsCog._teardown` and verifies grants were released and the Op role deleted. |
| 8 | `map_render` | Renders a multi-marker PNG via `services/mapping.py`, uploads it as a real attachment, then re-renders and edits the message in place via `MapCog._refresh_message` — the same in-place-edit path `/map mark`/`/map clear` use — and verifies the new attachment landed. |
| 9 | `serverlink_null` | Confirms `NullLink` reports unreachable with the expected courteous detail text, and that `/server status`'s unreachable-formatting branch builds an embed without raising. |
| 10 | `audit_and_incident` | Posts an audit note via `note_audit` and logs an incident via `log_incident` (both from `cogs/admin.py`) against the harness's audit channel, and verifies both landed — the message in Discord and the row in `incidents`. |

### Every check calls the real code — never a copy of it

Each check calls the cogs' own module-level functions or instance methods
rather than reimplementing their logic, so a real bug surfaces here instead
of being silently worked around: `_save_guild_config`,
`_create_default_ladder`, `build_team`, `_disband_team`,
`EventsCog._teardown`, `MapCog._refresh_message`, `ServerLinkCog._get_link`,
`note_audit`, `log_incident`, and everything in `services/`.

This rule was learned the hard way. Team creation was originally replicated
inline here, because `TeamsCog.create`'s body lived in the slash-command
callback and needed a live `discord.Interaction`. When the real command was
fixed, the harness kept exercising its own stale copy and reported a pass
that told us nothing about the shipped code. Creation is now factored into
`teams.build_team()`, which both the command and this harness call.

If you ever find yourself about to paste cog logic into a check, extract a
helper in the cog instead. A harness that mirrors production code tests the
mirror.

## Known operational quirk: Discord's create/delete-churn throttle

Discord intermittently returns `403 Forbidden` ("Missing Access" /
"Missing Permissions") on channel or role create/delete calls when a lot of
them happen in a short window — confirmed by isolated repro during
development (identical calls, full permissions, succeed once traffic cools
down; this doesn't come with the usual rate-limit headers, so it can't be
predicted or backed off from cleanly). Every Discord-mutating call the
harness makes directly is wrapped in `_retry_forbidden` (exponential
backoff, a few attempts) so this doesn't masquerade as a cog bug. If a check
fails after several minutes with repeated "hit a transient Forbidden,
retrying" warnings in the log followed by a final `Forbidden`, that's a real
permission problem, not the throttle — the retries would have ridden out a
transient one.

## Interpreting a failure

Each failed check prints its name, duration, and a detail string. Three
categories of failure:

1. **A clear remedy is printed** (currently just `connectivity`) — a setup
   problem on the guild/bot side. Fix per the remedy and re-run.
2. **An `AssertionError` message** — the check ran the real code path but
   the resulting Discord/DB state didn't match what was expected. This is
   almost always a genuine bug in the cog/service code being exercised; the
   message names exactly what didn't hold.
3. **A raw exception type and message** (e.g. `discord.errors.Forbidden: ...`)
   — the real Discord/DB call itself raised. Check whether it's the
   create/delete-churn throttle above (repeated retry warnings first) before
   assuming it's a permissions gap.

## Safety rules baked into the harness

- Everything created is named with the `selftest-` prefix (or, for team/event
  roles, keeps the cog's own naming convention but only within a
  harness-created object).
- A dedicated category + channels are created and deleted every run; nothing
  is ever posted into a pre-existing channel.
- Nothing is ever deleted unless its id is in this run's own
  creation registry (`Harness.created_role_ids` / `created_channel_ids`) —
  enforced with an `assert` in `Harness.delete_role` / `delete_channel`.
- No human member is ever touched — the bot's own member is the only test
  subject for role grants.
- The database is fully isolated (`data/selftest.db`), never the production
  `data/adjutant.db`.
- Cleanup is registered at creation time and run in reverse order from a
  `finally` block, so a crash mid-run still tidies up. `--keep` is the only
  way to skip it.
