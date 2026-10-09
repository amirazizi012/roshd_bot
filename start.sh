#!/usr/bin/env bash
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -q
while true; do
  python -m roshd_bot.main
  [ $? -eq 3 ] && exit 3
  echo "Bot stopped. Restarting in 10 seconds... (Ctrl+C to quit)"
  sleep 10
done
