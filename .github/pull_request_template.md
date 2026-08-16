<!--
  Merging to main deploys to the live bot within ~2 minutes. There is no
  staging gate. Fill this in honestly — an unchecked box with a note is far
  more useful than a ticked one that isn't true.
-->

Closes #

## What this changes

<!-- One paragraph. The effect, not the mechanism. -->

## How it was tested

<!-- Which tests you added and why they're the right ones. Say plainly if you
     couldn't test something — e.g. no live guild, no game server to point at. -->

## Checklist

- [ ] `python -m pytest` is green locally
- [ ] New logic lives in `adjutant/services/` and was written test-first
- [ ] Cogs stayed thin — no logic that a service should own
- [ ] User-facing strings go through `adjutant/voice.py`
- [ ] No new top-level command (or: it was agreed in the issue, and `tests/test_command_metadata.py` is updated deliberately)
- [ ] Buttons/modals re-check permissions and rate limits inline — the decorators don't apply to them
- [ ] Any new `discord.ext.tasks` loop logs, waits, and restarts on error
- [ ] Discord is asked first, the DB record changes second
- [ ] No channel is created that denies the bot `view_channel`
- [ ] The bot still needs only Manage Roles + Manage Channels + Manage Server
- [ ] Migration number was claimed in the issue and doesn't collide with `main` (n/a if no migration)
- [ ] No token, `.env`, or private channel content in the diff or the description

## Anything a reviewer should push back on

<!-- Judgement calls you made that could reasonably have gone the other way. -->
