#!/usr/bin/env bash
# Update the installed agent from this checkout. Node identity (config.yaml, agent.env) and state are never touched.
#   git pull && sudo bash agent/update.sh
#
# Safety: the new code (and, only if requirements.txt changed, a NEW virtualenv) is built beside the live one and must
# import AND accept your existing config.yaml before anything is swapped. If the service does not come up afterwards,
# the previous code and unit are restored and restarted. A bad config aborts the update before anything changes.
# Paths: taken from HCLUSTER_* variables, else from $INSTALL_DIR/layout.env recorded by install.sh, else the defaults.
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SRC/lib/common.sh"
[[ "${1:-}" != "-h" && "${1:-}" != "--help" ]] || { sed -n '2,9p' "$0"; exit 0; }

require_root; check_layout; need_python
[[ -x "$INSTALL_DIR/venv/bin/python" ]] || die "agent not installed at $INSTALL_DIR (run install.sh first; custom paths need HCLUSTER_INSTALL_DIR)" 2
[[ -f "$CONF" && -f "$ENVF" ]] || die "missing $CONF or $ENVF: this is not a complete installation (re-run install.sh)" 2
take_lock

say "updating agent in $INSTALL_DIR (config: $CONF_DIR, state: $STATE_DIR)"
deploy_code "$CONF" "$ENVF" || die "update aborted; the installed agent was left exactly as it was" 1
write_layout; write_wrapper
NEWVER="$(PYTHONPATH="$INSTALL_DIR/app" "$INSTALL_DIR/venv/bin/python" -c 'import hcluster_agent; print(hcluster_agent.__version__)')"

if [[ $USE_SYSTEMD -eq 1 ]]; then
  UNIT_BAK=""
  if [[ -f "$UNIT_FILE" ]]; then UNIT_BAK="$INSTALL_DIR/.unit.bak"; cp -p "$UNIT_FILE" "$UNIT_BAK"; fi
  mkdir -p "$UNIT_DIR"; render_unit > "$UNIT_FILE"
  if sc is-active --quiet hcluster-agent.service || sc is-enabled --quiet hcluster-agent.service; then
    restart_or_rollback "$UNIT_BAK" || die "update failed: the new version did not start; the previous version was restored and restarted (see: journalctl -u hcluster-agent)" 1
  else
    sc daemon-reload
    warn "the service is neither running nor enabled; not starting it (sudo systemctl enable --now hcluster-agent)"
  fi
  rm -f "$UNIT_BAK"
  sc --no-pager --lines=5 status hcluster-agent.service || true
fi
commit_code
say "agent updated to version $NEWVER (previous code kept in $INSTALL_DIR/app.prev)"
