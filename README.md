# Adjutant

A Discord adjutant for **Arma Reforger** communities — vanilla or modded.

Guild setup, ranked roles (perma / temp / event), locked team channels that
don't leak, an event organiser, Pillow-rendered live maps, and **optional**
dedicated-server integration. Every feature is opt-in: run the full adjutant,
or just the bits you want. No game server required.

## Quick start (development)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
copy .env.example .env   # then put your DISCORD_TOKEN in .env
python -m adjutant
```

Tests: `python -m pytest`

## Production

Runs as systemd unit `reforger-adjutant.service` from `/opt/reforger-adjutant`
with a git-poll auto-deploy timer — see `deploy/README.md`. Push to `main`
and the server updates itself within ~2 minutes.

## Design

See `docs/SPEC.md` for the feature spec, architecture, and the rules the bot
lives by (opt-in everything, leak-proof team channels, courteous refusals,
plain-spoken about problems).
