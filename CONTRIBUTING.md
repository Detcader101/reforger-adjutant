# Contributing to Adjutant

Thanks for pitching in. This file is the short version of everything that will
otherwise bite you. Read the **Rules that aren't negotiable** section even if
you skip the rest — most of them exist because they already cost someone a day.

- **Backlog:** [Issues](https://github.com/Detcader101/reforger-adjutant/issues)
  are the backlog. The board is the view. See [`docs/BACKLOG.md`](docs/BACKLOG.md)
  for how work is picked up and how it moves.
- **Design:** [`docs/SPEC.md`](docs/SPEC.md) — product principles and architecture.
- **Game-server facts:** [`docs/SERVER_INTEGRATION.md`](docs/SERVER_INTEGRATION.md) —
  researched, confirmed, and load-bearing. Don't re-derive them.

---

## Getting set up

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
copy .env.example .env      # then put your own DISCORD_TOKEN in it
python -m pytest
```

You need your **own** Discord application and bot token, and your **own** test
guild. Put its id in `DEV_GUILD_IDS` so commands sync instantly instead of
waiting an hour for global propagation. Never commit `.env`; never paste a
token into an issue, a PR, or a log excerpt.

The bot needs exactly three permissions: **Manage Roles**, **Manage Channels**,
**Manage Server**. Administrator is deliberately *not* required, and any change
that starts needing it is a design bug, not a setup step.

**On Windows/WSL:** WSL has no Python here. Run everything through the Windows
interpreter — `cmd.exe /c ".venv\Scripts\python.exe -m pytest -q"` from the repo
root. Nested quoting of `python -c` breaks under `cmd.exe`; write a script file
instead (see `tests/import_check.py`).

## Running the bot without leaving orphans

Use `tools/run_bot_briefly.ps1` for a bounded run. Do **not** wrap the bot in
WSL `timeout` — it orphans the Windows Python process, which keeps holding the
SQLite lock, the log file, and a live gateway session. `tools/list_bots.ps1`
finds strays; `taskkill /F /T /PID <pid>` clears them.

Other tools worth knowing:

| Tool | What it does |
|---|---|
| `tools/probe_guild.py` | Read-only diagnostic: bot role position, missing perms, registered commands |
| `tools/dump_db.py` | Row counts + the `guilds` table |
| `python -m adjutant.selftest` | Live harness against your test guild (~20s, cleans up after itself) |
| `tools/cleanup_test_guild.py` | Removes leftovers a failed run left behind |

See [`docs/SELFTEST.md`](docs/SELFTEST.md) for the live harness in detail.

---

## Rules that aren't negotiable

**1. Logic goes in `adjutant/services/`, and it's test-driven.**
Rank maths, expiry scheduling, coordinate transforms, permission matrices,
parsing — all Discord-free functions in `services/`, written test-first.
Cogs in `adjutant/cogs/` are thin adapters: resolve the interaction, call a
service, render with `voice.py`. If you find yourself wanting to test a cog,
that's the signal to extract a service.

**2. A harness imports production code. It never mirrors it.**
The self-test once replicated team creation inline; after the real command was
fixed the harness still failed against its own stale copy — and would equally
have passed while the shipped code stayed broken. If you're tempted to paste cog
logic into a check, extract a helper and call it from both.

**3. The command surface is eight top-level commands.**
`/adjutant` `/team` `/event` `/map` `/rank` `/server` `/setup` `/admin`.
`tests/test_command_metadata.py` asserts that exact set, so adding a ninth is a
decision to be argued in an issue first — not something a PR does in passing.
New functionality attaches to an existing command as an argument, a button, or a
modal. Each command acts bare and does the obvious thing.

**4. Every user-facing string goes through `adjutant/voice.py`.**
The persona — courteous, dry, unflappable, brief — is tunable in one place, and
only stays consistent if nothing bypasses it. That includes error text.

**5. Set `group_description=` explicitly on every GroupCog.**
A GroupCog's description defaults to its class docstring, Discord caps it at 100
characters, and exceeding the cap **400s the entire command upload** — every
command silently fails to register while the unit suite stays green. This is
guarded by `tests/test_command_metadata.py`; don't route around it.

**6. Buttons and modals must re-check permissions and rate limits inline.**
`@rate_limited()` and `require_permission()` are `app_commands.check`s keyed on
`interaction.command`, which is `None` for a click or a modal submit. Converting
a slash command to a button silently drops both. Call
`admin.check_rate_limit(interaction, key)` and `roles.member_has_permission(...)`
in the handler — and re-check at click time, because a button is a fresh
interaction and the clicker may not be the original invoker.

**7. Never create a channel whose overwrites deny the bot `view_channel`.**
It becomes undeletable by the bot (50001 Missing Access) and can't re-grant
itself access without Administrator. Related: a permission overwrite may not
*grant* the `manage_roles` bit unless the acting member is a full guild
Administrator — a hardcoded Discord anti-escalation carve-out. Guild-wide Manage
Roles already applies in channels the bot can see, so you never need that bit.

**8. Ask Discord first, change the record second.**
Remove the role, *then* delete the grant row. The reverse order leaves a member
holding an untracked role when Discord refuses, and in the expiry sweep it
quietly turns a temp rank permanent.

**9. Background loops must survive their own exceptions.**
`discord.ext.tasks` stops a loop *for good* on an escaped exception. Every loop
logs, waits a cycle, and restarts. Copy the pattern from an existing one.

**10. No leaks between teams.**
Team channels are locked at the Discord permission layer, not by convention. The
bot never repeats one team's content in another's channel — including in errors
and in anything surfaced to users.

---

## Migrations

`adjutant/migrations/` holds numbered `.sql` files applied in order at startup.
They are **single-owner**: two contributors both writing `0005_*.sql` is a merge
conflict that silently applies only one of them.

**Claim your number in the issue before you write it.** Say so in a comment —
"taking 0005" — and check the issue thread plus `main` before you start. Highest
number currently on `main`: **0004**.

Migrations are forward-only. Never edit one that's already on `main`; add
another.

## Tests

- New logic in `services/` arrives with tests, written first. Red, green, refactor.
- Test names describe behaviour, not implementation — `test_temp_grant_expires_at_its_deadline`,
  not `test_expiry_returns_true`.
- Don't assert on constants or internals. Assert on what a user of the function gets.
- Cogs get integration-style tests via `tests/fakes.py` where it's cheap to do so.
- **Never patch stdlib `time.monotonic` globally.** It freezes asyncio's clock and
  deadlocks the suite. Patch a stub onto the importing module instead — see
  `tests/test_cache.py::_fake_clock`.

Run the whole suite before you open a PR. It's ~413 tests and takes seconds.

## Style

`ruff` config lives in `pyproject.toml` (line length 100, target py312). Run
`ruff check .` and `ruff format .` if you have it. Match the surrounding code —
type hints on public functions, docstrings that say *why* rather than restating
the signature.

---

## Branching, PRs, and the deploy footgun

> **`main` deploys to production.** A poll timer on the ShedNet CT pulls
> `origin/main` every ~2 minutes and restarts the live bot. There is no staging
> gate. A merge to `main` is a release.

So:

1. **Branch off `main`.** Name it for the work: `feat/live-map-overlay`,
   `fix/rcon-players-parse`, `chore/ruff-in-ci`.
2. **One issue, one branch, one PR.** If the PR grew a second concern, split it.
3. **Open a PR — don't push to `main` directly**, even if you have the access.
4. Fill in the PR template honestly. "I didn't test this against a live guild"
   is a useful thing to write down; a silently unchecked box isn't.
5. A maintainer reviews and merges. Expect to be argued with; argue back if
   you think you're right.

Commit messages: imperative mood, no `feat:`/`fix:` prefixes, describing the
effect rather than the mechanism. Match what's already in `git log`:

```
Fold /setup into one command; final surface is eight
Keep background loops alive after an unhandled error
Roll back partially-built teams instead of stranding them
```

## Filing issues

Use the templates — they ask for the things that turn out to matter (which cog,
whether a game server is linked, what the bot said versus what you expected).
If you're about to file something that's really a question, open a Discussion or
ask in the thread of a related issue instead.

Good first issues are labelled [`good first issue`](https://github.com/Detcader101/reforger-adjutant/labels/good%20first%20issue).
They're scoped to be finishable in one sitting and mostly live in `services/`,
where the tests tell you when you're done.
