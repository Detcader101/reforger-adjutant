# How the backlog works

The backlog is [**GitHub Issues**](https://github.com/Detcader101/reforger-adjutant/issues).
Not a file, not a chat thread. If it isn't an issue, it isn't on the backlog, and
nobody can claim it or see that you're already doing it.

The **Adjutant Backlog** project board is a *view* over those issues, not a
second source of truth. Never move a card without the issue reflecting it.

---

## The states

| State | Means |
|---|---|
| **Needs triage** | Filed, nobody's read it properly yet. Every new issue starts here. |
| **Ready** | Triaged, scoped, labelled. Anyone can pick it up. |
| **In progress** | Someone has claimed it and is working. |
| **In review** | A PR is open and linked. |
| **Done** | Merged. The issue closes itself via `Closes #n`. |
| **Blocked** | Waiting on something outside the repo — a token, a server, a design call. Say what, in a comment. |

`status:` labels carry the state so it's visible in the issue list too. When
they disagree with the board, the label wins.

## Claiming work

1. Pick something labelled `status: ready`. If it's also `good first issue`,
   it's scoped to be finishable in one sitting.
2. **Comment on it** — "taking this". That's the claim. Don't rely on the board.
3. A maintainer assigns you and flips it to `status: in progress`.
4. Branch, work, open a PR with `Closes #n` in the description.
5. If you stall or lose interest, say so on the issue and unassign. That's a
   normal thing to do, not a failure — a silently parked issue is worse.

**One issue in progress per contributor** unless you've agreed otherwise. The
point is throughput, not allocation.

## Triage

New issues get read and labelled. Triage answers four questions:

- Is it real, and is it one thing? Split it if not.
- Does it survive `docs/SPEC.md`? Some requests are out of scope on purpose.
- Which `area:` does it touch? That's what decides who can safely work in parallel.
- Is it scoped tightly enough that someone else could finish it? If not, it stays
  in needs-triage until it is. Vague issues are the main reason backlogs rot.

---

## Labels

**Type** — what kind of work. `type: bug` · `type: feature` · `type: chore` ·
`type: design` (Jay Jay's call — visual and tone decisions) ·
`type: research` (answer a question, produce a finding, not code).

**Area** — which part of the system, and therefore which files.
`area: setup` · `area: ranks` · `area: teams` · `area: events` · `area: map` ·
`area: serverlink` · `area: admin` · `area: infra` · `area: voice` ·
`area: deploy` · `area: docs` · `area: ci`

**Priority** — `P0` production is broken · `P1` next up · `P2` wanted ·
`P3` someday. Absence means P2.

**Status** — `status: needs triage` · `status: ready` · `status: in progress` ·
`status: in review` · `status: blocked`

**Invitations** — `good first issue` · `help wanted` · `needs decision`
(a maintainer has to choose before anyone can build it).

---

## Working in parallel without colliding

Most of the pain in a multi-contributor repo is two people editing the same
file. Three rules keep it rare:

**1. `area:` labels map to file scopes.** Two issues in different areas can be
worked simultaneously without coordination:

| Area | Owns |
|---|---|
| `setup` | `cogs/setup.py`, `services/templates.py`, `services/config.py` |
| `ranks` | `cogs/roles.py`, `services/ranks.py`, `services/grants.py` |
| `teams` | `cogs/teams.py` |
| `events` | `cogs/events.py`, `services/events.py` |
| `map` | `cogs/map.py`, `services/mapping.py`, `assets/maps/` |
| `serverlink` | `cogs/serverlink.py`, `serverlink/*` |
| `admin` | `cogs/admin.py`, `audit.py`, `services/ratelimit.py` |
| `infra` | `db.py`, `cache.py`, `bot.py`, `bot_health.py`, `view_util.py`, `services/persistence.py`, `services/guilds.py` |
| `voice` | `voice.py` — touched by everything, so changes here are small and deliberate |

**2. Migrations are single-owner.** `adjutant/migrations/` is numbered and
applied in order; two people writing `0005_*.sql` is a conflict that silently
applies one of them. **Claim your number in a comment on the issue** before you
write the file, and check `main` first. Highest on `main` today: **0004**.

**3. `tests/test_command_metadata.py` is a shared contract.** It pins the exact
eight-command surface and every description and parameter. Changing it means the
command surface changed, which is a decision, not an implementation detail.

---

## The deploy footgun

> **`main` is production.** A timer on the ShedNet CT pulls `origin/main` every
> ~2 minutes and restarts the live bot. There is no staging environment.

Consequences worth internalising:

- A merge is a release. Review accordingly.
- A bad merge is live within two minutes, to real communities.
- Revert first, diagnose second. `git revert` and push beats a hotfix under pressure.
- Nothing goes to `main` except through a PR, whatever access you happen to hold.

## Milestones

- **v1.1 — contributor-ready.** Everything needed before strangers can usefully
  send PRs: CI actually running, lint enforced, branch protection, the docs you're
  reading. Getting this done is the highest-value work in the repo right now.
- **v1.2 — live-server depth.** The telemetry feed earning its keep: live map
  overlay, RCON verification against a real server, the public feed endpoint.
- **v1.3 — polish.** Announce-channel config, pagination, real terrain imagery,
  the design pass on `voice.py`.
- **Backlog** — anything real but unscheduled. Not a graveyard: if something sits
  here for months and nobody wants it, close it. Closed isn't dead; it's searchable.

Explicitly out of scope, per `docs/SPEC.md`: dashboards and web UI, cross-guild
federation, stats/ELO, voice features. Issues asking for these get closed with a
pointer here rather than lingering as false hope.
