#!/usr/bin/env bash
# Remove the agent. Keeps config + token unless --purge is given.
#   sudo ./agent/uninstall.sh [--purge]
set -euo pipefail
INSTALL_DIR="${HCLUSTER_INSTALL_DIR:-/opt/hcluster-agent}"
CONF_DIR="${HCLUSTER_CONF_DIR:-/etc/hcluster-agent}"
STATE_DIR="${HCLUSTER_STATE_DIR:-/var/lib/hcluster-agent}"
UNIT_DIR="${HCLUSTER_UNIT_DIR:-/etc/systemd/system}"
BIN_LINK="${HCLUSTER_BIN_LINK:-/usr/local/bin/hcluster-agent}"
SKIP_SYSTEMD="${HCLUSTER_SKIP_SYSTEMD:-0}"
PURGE=0; [[ "${1:-}" == "--purge" ]] && PURGE=1
[[ "$SKIP_SYSTEMD" == "1" || $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

if [[ "$SKIP_SYSTEMD" != "1" ]]; then
  systemctl disable --now hcluster-agent.service 2>/dev/null || true
  rm -f "$UNIT_DIR/hcluster-agent.service"
  systemctl daemon-reload
fi
rm -rf "$INSTALL_DIR" "$BIN_LINK"
if [[ $PURGE -eq 1 ]]; then
  rm -rf "$CONF_DIR" "$STATE_DIR"
  [[ "$SKIP_SYSTEMD" == "1" ]] || userdel hcluster 2>/dev/null || true
  echo "purged config, state and user."
else
  echo "kept $CONF_DIR and $STATE_DIR (use --purge to remove). Remember to delete the node in the dashboard / revoke its token."
fi
echo "agent removed."
