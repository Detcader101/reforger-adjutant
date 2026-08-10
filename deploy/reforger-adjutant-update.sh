#!/usr/bin/env bash
# Poll origin/main and redeploy on new commits. Mirrors the tekken-bot flow.
# Installed on the CT at /usr/local/bin/, driven by reforger-adjutant-update.timer.
set -euo pipefail

REPO=/opt/reforger-adjutant
cd "$REPO"

git fetch origin main --quiet
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
[ "$LOCAL" = "$REMOTE" ] && exit 0

echo "Updating $LOCAL -> $REMOTE"
git pull --ff-only origin main
.venv/bin/pip install -q -r requirements.txt

SHA=$(git rev-parse HEAD)
SUBJECT=$(git log -1 --pretty=%s)

# Atomic .env rewrite: build the new file next to the real one, then
# rename over it. A crash mid-write (disk full, OOM kill) leaves the
# original .env untouched instead of a half-written file the bot then
# fails to parse on its next start.
TMP_ENV=$(mktemp .env.XXXXXX)
grep -v -e '^BOT_GIT_SHA=' -e '^BOT_GIT_SUBJECT=' .env > "$TMP_ENV" || true
printf 'BOT_GIT_SHA=%s\nBOT_GIT_SUBJECT=%s\n' "$SHA" "$SUBJECT" >> "$TMP_ENV"
chmod 600 "$TMP_ENV"
mv "$TMP_ENV" .env

sudo systemctl restart reforger-adjutant.service
