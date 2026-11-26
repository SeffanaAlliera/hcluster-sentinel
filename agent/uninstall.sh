#!/usr/bin/env bash
# Remove the agent.
#   sudo bash agent/uninstall.sh            removes code, venv, service and the CLI wrapper; KEEPS config, token, backups and
#                                           state (including the offline queue), so a reinstall picks the node up again
#   sudo bash agent/uninstall.sh --purge    ALSO deletes $CONF_DIR (config + token + backups), $STATE_DIR and the service user
# Paths: HCLUSTER_* variables, else $INSTALL_DIR/layout.env, else the defaults.
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SRC/lib/common.sh"
PURGE=0
for a in "$@"; do case "$a" in --purge) PURGE=1;; -h|--help) sed -n '2,7p' "$0"; exit 0;; *) die "unknown option: $a" 2;; esac; done

require_root; check_layout
take_lock
if [[ $USE_SYSTEMD -eq 1 ]]; then
  sc disable --now hcluster-agent.service >/dev/null 2>&1 || true
  rm -f "$UNIT_FILE"
  [[ $PURGE -eq 0 ]] || rm -rf "$UNIT_DIR/hcluster-agent.service.d"
  sc daemon-reload || true
fi
rm -rf "$INSTALL_DIR" "$BIN_LINK"
if [[ $PURGE -eq 1 ]]; then
  rm -rf "$CONF_DIR" "$STATE_DIR"
  [[ $MANAGE_SYSTEM -eq 0 ]] || userdel "$SVC_USER" 2>/dev/null || true
  say "purged: code, service, config, token, backups, state and user."
else
  say "kept $CONF_DIR (config, token, backups) and $STATE_DIR (offline queue). Reinstalling with the same node picks them up; --purge removes them."
fi
rm -f "${INSTALL_DIR%/}.lock"
say "agent removed. Remember to delete the node in the dashboard (or rotate its token) if it is gone for good."
