#!/usr/bin/env bash
#
# Stop tracking secrets + runtime state WITHOUT deleting your live files.
#
# Why this exists:
#   .env (bot token), bot_session.session, sessions/*.session and
#   user_configs.json (api_id / api_hash / phone) were committed to this repo.
#   Anyone with read access to the repository therefore had FULL control of the
#   Telegram account. .gitignore alone does not help - already-tracked files
#   stay tracked.
#
# What it does:
#   1. copies the files to secrets_backup_<timestamp>/  (so nothing is lost)
#   2. runs `git rm --cached` (removes from the index, keeps them on disk)
#   3. commits
#
# IMPORTANT - do these two things as well, they cannot be scripted:
#   * Revoke the bot token:  @BotFather -> /revoke
#   * Reset the api_hash:    https://my.telegram.org  (and re-login the user)
#   * History rewrite:       the old blobs are still in git history. If this repo
#                            was ever public/pushed anywhere shared, scrub it
#                            with `git filter-repo` or treat the secrets as dead.
#
# Run from the repo root:  ./scripts/untrack_secrets.sh

set -euo pipefail
cd "$(dirname "$0")/.."

STAMP=$(date +%Y%m%d_%H%M%S)
BACKUP="secrets_backup_${STAMP}"

TARGETS=(
  .env
  bot_session.session
  bot_session.session-journal
  user_configs.json
  user_filters.json
  user_msg_maps.json
  processed_messages.json
)

mkdir -p "$BACKUP"
echo "Backing up tracked secrets to $BACKUP/"

for f in "${TARGETS[@]}"; do
  [ -f "$f" ] && cp -p "$f" "$BACKUP/" && echo "  backed up $f"
done
if [ -d sessions ]; then
  cp -rp sessions "$BACKUP/sessions" && echo "  backed up sessions/"
fi

echo
echo "Untracking (files stay on disk)..."
for f in "${TARGETS[@]}"; do
  git rm --cached --ignore-unmatch "$f" >/dev/null && echo "  untracked $f"
done
git rm -r --cached --ignore-unmatch sessions >/dev/null && echo "  untracked sessions/"

cat >> .gitignore <<'EOF'

# local secret backups
secrets_backup_*/
EOF

git add .gitignore
git commit -m "security: stop tracking secrets, sessions and runtime state"

echo
echo "Done. The files are still on disk; git no longer tracks them."
echo "Backup copy: $BACKUP/"
echo
echo "NEXT (manual, cannot be automated):"
echo "  1. @BotFather -> /revoke  (new bot token, update .env)"
echo "  2. my.telegram.org -> reset api_hash, then re-login via the bot"
echo "  3. If this repo was ever shared/public, scrub history with git filter-repo"
