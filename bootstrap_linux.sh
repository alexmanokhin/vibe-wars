#!/bin/bash
# First-time setup of a fresh Debian/Ubuntu server for the dashboard. Run as root:
#   ssh root@<server> 'bash -s' < bootstrap_linux.sh
#
# Does the parts that need no human: swap, packages, tailscale (not signed in),
# a user with linger and an optional memory ceiling, ssh key, folders. Signing
# in to tailscale and to Claude are separate steps (each needs a link a human
# opens). Idempotent: running it again changes nothing.
set -euo pipefail

RC_USER="${RC_USER:-rc}"
SWAP_GB="${SWAP_GB:-4}"
MEMORY_HIGH="${MEMORY_HIGH:-}"          # e.g. 10G; empty = no ceiling
PUBKEY="${PUBKEY:-}"                    # ssh public key for the new user

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# bsdutils provides script; lsof and iproute2 inspect sockets
apt-get install -y -qq curl git lsof bsdutils iproute2 procps ca-certificates >/dev/null

echo "== swap ${SWAP_GB}G"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l "${SWAP_GB}G" /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon --show

echo "== tailscale"
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh >/tmp/ts-install.log 2>&1
fi
systemctl enable --now tailscaled >/dev/null
tailscale version | head -1

echo "== user $RC_USER"
id "$RC_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$RC_USER"
loginctl enable-linger "$RC_USER"
RC_HOME=$(eval echo "~$RC_USER")
install -d -m 700 -o "$RC_USER" -g "$RC_USER" "$RC_HOME/.ssh"
if [ -n "$PUBKEY" ]; then
  touch "$RC_HOME/.ssh/authorized_keys"
  grep -qF "$PUBKEY" "$RC_HOME/.ssh/authorized_keys" || echo "$PUBKEY" >> "$RC_HOME/.ssh/authorized_keys"
else
  # fall back to root's key (the one the provider installed)
  cp /root/.ssh/authorized_keys "$RC_HOME/.ssh/authorized_keys"
fi
chown "$RC_USER:$RC_USER" "$RC_HOME/.ssh/authorized_keys"; chmod 600 "$RC_HOME/.ssh/authorized_keys"
# rc-work, not home: Claude Code refuses to start in the home folder
sudo -u "$RC_USER" mkdir -p "$RC_HOME/rc-work" "$RC_HOME/.claude/logs" \
  "$RC_HOME/.config/systemd/user" "$RC_HOME/.local/bin"
RCUID=$(id -u "$RC_USER")
if [ -n "$MEMORY_HIGH" ]; then
  systemctl set-property "user-$RCUID.slice" "MemoryHigh=$MEMORY_HIGH"
fi

echo "== claude for $RC_USER"
if [ ! -x "$RC_HOME/.local/bin/claude" ]; then
  sudo -u "$RC_USER" -H bash -c 'curl -fsSL https://claude.ai/install.sh | bash' >/tmp/claude-install.log 2>&1 || tail -5 /tmp/claude-install.log
fi
sudo -u "$RC_USER" -H "$RC_HOME/.local/bin/claude" --version || true

echo "== done"
free -m | head -2
echo "uid=$RCUID linger=$(ls /var/lib/systemd/linger/ | tr '\n' ' ')"
echo "next: tailscale up (opens a sign-in link), clone the repo as $RC_USER, python3 make_units.py, sign in to claude"
