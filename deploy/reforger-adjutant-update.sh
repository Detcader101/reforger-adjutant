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
sed -i '/^BOT_GIT_SHA=/d;/^BOT_GIT_SUBJECT=/d' .env
printf 'BOT_GIT_SHA=%s\nBOT_GIT_SUBJECT=%s\n' "$SHA" "$SUBJECT" >> .env

systemctl restart reforger-adjutant.service
