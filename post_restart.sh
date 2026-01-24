#!/usr/bin/env bash
# post_restart.sh
# Sends a Discord message to CHANGELOG_CHANNEL_ID when the QRLS bot restarts

set -e

# Move to bot directory
cd /root/qrls_bot || exit 0

# Load environment variables from .env
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

# Required variables
# DISCORD_TOKEN
# CHANGELOG_CHANNEL_ID
if [ -z "$DISCORD_TOKEN" ] || [ -z "$CHANGELOG_CHANNEL_ID" ]; then
  # Missing config — silently exit so systemd does not fail
  exit 0
fi

HOSTNAME_STR=$(hostname)
TIMESTAMP=$(date -u +"%Y-%m-%d %H:%M:%S UTC")

MESSAGE="⚙️ **QRLS Bot Restarted**  
🖥️ Host: \`$HOSTNAME_STR\`  
🕒 Time: \`$TIMESTAMP\`"

# Send message via Discord REST API
curl -s -X POST "https://discord.com/api/v10/channels/$CHANGELOG_CHANNEL_ID/messages" \
  -H "Authorization: Bot $DISCORD_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(jq -nc --arg content "$MESSAGE" '{content:$content}')" \
  >/dev/null 2>&1 || true

# Always exit successfully so systemd is happy
exit 0
