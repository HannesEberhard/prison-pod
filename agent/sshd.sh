#!/usr/bin/env bash
# Entrypoint of the `ssh` sidecar.
set -euo pipefail

dir=/data/sshd
mkdir -p "$dir" "$HOME"
chmod 700 "$dir"
# Kept on the PVC so the host key survives pod restarts.
if [[ ! -f "$dir/host_ed25519" ]]; then
  ssh-keygen -q -t ed25519 -N '' -C "$SANDBOX_NAME" -f "$dir/host_ed25519"
fi
printf '%s\n' "$SSH_AUTHORIZED_KEYS" > "$dir/authorized_keys"
chmod 600 "$dir/authorized_keys"

exec /usr/sbin/sshd -D -e -f /etc/ssh/sandbox_sshd_config
