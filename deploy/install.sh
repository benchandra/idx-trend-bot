#!/bin/bash
# One-time setup of the always-on runner on a small Ubuntu VM (Oracle Cloud / Google Cloud free tier).
# Usually started automatically by deploy/cloud-init.yaml. Manual use: bash deploy/install.sh
# Needs deploy/.env with TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GITHUB_REPO (owner/idx-trend-bot),
# GITHUB_TOKEN (fine-grained token, Contents: read & write on that repository) and the settings.
set -e
cd "$(dirname "$0")/.."
ROOT=$(pwd)
sudo apt-get update -q && sudo apt-get install -y -q python3-venv python3-pip git
python3 -m venv .venv && . .venv/bin/activate && pip install -q --upgrade pip && pip install -q -r requirements.txt
set -a; . deploy/.env; set +a
if [ -n "$GITHUB_TOKEN" ] && [ -n "$GITHUB_REPO" ]; then
  git remote set-url origin "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPO}.git"
  git config user.name "idx-trend-bot-vm"; git config user.email "bot@users.noreply.github.com"
  grep -q '^GIT_SYNC=' deploy/.env || echo 'GIT_SYNC=1' >> deploy/.env
fi
sudo tee /etc/systemd/system/idx-monitor.service > /dev/null <<UNIT
[Unit]
Description=IDX Trend Bot always-on runner
After=network-online.target
Wants=network-online.target
[Service]
User=$(whoami)
WorkingDirectory=$ROOT
EnvironmentFile=$ROOT/deploy/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$ROOT/.venv/bin/python -m bot.tasks monitor
Restart=always
RestartSec=30
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now idx-monitor
$ROOT/.venv/bin/python -m bot.tasks heartbeat || true
echo "Runner installed. Status: sudo systemctl status idx-monitor   Logs: journalctl -u idx-monitor -f"
