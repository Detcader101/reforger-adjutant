# Deploying Adjutant on ShedNet

Same pattern as `shed-tekken` / tekken-bot. One-time CT setup (Debian CT,
suggested hostname `shed-adjutant`):

```bash
# as root on the new CT
apt update && apt install -y git python3 python3-venv
useradd -r -m -d /opt/reforger-adjutant -s /usr/sbin/nologin adjutant
sudo -u adjutant git clone https://github.com/Detcader101/reforger-adjutant /opt/reforger-adjutant
cd /opt/reforger-adjutant
sudo -u adjutant python3 -m venv .venv
sudo -u adjutant .venv/bin/pip install -r requirements.txt
sudo -u adjutant cp .env.example .env   # then edit in DISCORD_TOKEN

install -m 755 deploy/reforger-adjutant-update.sh /usr/local/bin/
cp deploy/reforger-adjutant.service deploy/reforger-adjutant-update.service \
   deploy/reforger-adjutant-update.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now reforger-adjutant.service reforger-adjutant-update.timer
```

After that: push to `main` → live within ~2 minutes. Check what's deployed via
`BOT_GIT_SHA` in `/opt/reforger-adjutant/.env` or `systemctl status reforger-adjutant`.

## Sudoers entry for the update service

`reforger-adjutant-update.service` runs `reforger-adjutant-update.sh` as the
unprivileged `adjutant` user (not root) — the script `git pull`s and `pip
install`s code from the repo, so it shouldn't run with root privileges any
more than it has to. That means the script's final `systemctl restart
reforger-adjutant.service` needs an explicit, narrowly-scoped sudoers grant;
without it the restart silently fails and the timer just keeps re-pulling an
already-current repo every cycle.

Add this on the CT (`visudo -f /etc/sudoers.d/reforger-adjutant`):

```
adjutant ALL=(root) NOPASSWD: /usr/bin/systemctl restart reforger-adjutant.service
```

Scope it to that exact command — do not grant `systemctl` broadly, and do not
add `NOPASSWD:ALL`.
