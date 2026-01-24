#!/usr/bin/env bash
# post_restart.sh - Notify a Discord channel when QRLS bot restarts

set -e

cd /root/qrls_bot

# Load .env into the environment (DISCORD_TOKEN, CHANGELOG_CHANNEL_ID, etc.)
if [ -f .env ]; then
  # This assumes .env is KEY=VALUE format, which matches how your bot uses it
  set -a
  . ./.env
  set +a
fi

# If we don't have what we need, just exit quietly so systemd doesn't fail
if [ -z "$DISCORD_TOKEN" ] || [ -z "$CHANGELOG_CHANNEL_ID" ]; then
  exit 0
fi

HOSTNAME_STR=$(hostname)
MESSAGE="⚙️ QRLS Bot was restarted on \`$HOSTNAME_STR\`."

# Send a simple message to the configured channel using the Bot token
curl -sS -X POST "https://discord.com/api/v10/channels/$CHANGELOG_CHANNEL_ID/messages" \
  -H "Authorization: Bot $DISCORD_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(printf '{"content": "%s"}' "$MESSAGE")" \
  >/dev/null 2>&1 || true

# Always exit 0 so this hook never marks the service as failed
exit 0
EOF