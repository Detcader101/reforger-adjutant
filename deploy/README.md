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
