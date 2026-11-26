#!/usr/bin/env bash
set -euo pipefail

# $HOME is on the sandbox's PVC: seed it only once, so a token refreshed by Claude
# and Claude's own state survive pod restarts.
mkdir -p "$HOME/.claude"
if [[ ! -f "$HOME/.claude/.credentials.json" ]]; then
  install -m 600 /secrets/claude/.credentials.json "$HOME/.claude/.credentials.json"
fi

# oauthAccount: without it remote-control fails with "Unable to determine your organization".
# remoteDialogSeen: pre-answers the one-time "Enable Remote Control? (y/n)" prompt.
# jq fails (and nothing is written) if the Secret's oauthAccount.json is missing or invalid.
if [[ ! -f "$HOME/.claude.json" ]]; then
  jq -n --slurpfile account /secrets/claude/oauthAccount.json --arg dir "$PWD" \
    '{remoteDialogSeen: true, oauthAccount: $account[0], projects: {($dir): {hasTrustDialogAccepted: true}}}' \
    > "$HOME/.claude.json.tmp"
  mv "$HOME/.claude.json.tmp" "$HOME/.claude.json"
fi

# From spec.git, applied on every start so edits take effect; ~/.gitconfig is on the PVC,
# so SSH sessions see it too.
git config --global user.name "$GIT_USER_NAME"
git config --global user.email "$GIT_USER_EMAIL"

rc=(claude remote-control --name "$SANDBOX_NAME" --permission-mode "${PERMISSION_MODE:-auto}")

# Reattach to the session a previous pod recorded on the PVC. --continue exits with an error
# right away if there is none (or it expired); then start a new one.
# While claude runs as our child, forward SIGTERM: bash as PID 1 would otherwise ignore it.
"${rc[@]}" --continue &
child=$!
trap 'kill -TERM "$child"; wait "$child"; exit $?' TERM INT
if wait "$child"; then exit 0; fi
trap - TERM INT
echo "entrypoint: no session to resume, starting a new one" >&2

# --capacity 1: one session per sandbox; extra sessions would share the same working tree.
exec "${rc[@]}" --capacity 1
