#!/usr/bin/env bash
# Update the installed agent from this checkout (config + token are left untouched).
#   git pull && sudo ./agent/update.sh
set -euo pipefail
INSTALL_DIR="${HCLUSTER_INSTALL_DIR:-/opt/hcluster-agent}"
CONF_DIR="${HCLUSTER_CONF_DIR:-/etc/hcluster-agent}"
UNIT_DIR="${HCLUSTER_UNIT_DIR:-/etc/systemd/system}"
SKIP_SYSTEMD="${HCLUSTER_SKIP_SYSTEMD:-0}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ "$SKIP_SYSTEMD" == "1" || $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -d "$INSTALL_DIR/venv" ]] || { echo "agent not installed; run install.sh first" >&2; exit 1; }

rm -rf "$INSTALL_DIR/app/hcluster_agent"
cp -r "$SRC/hcluster_agent" "$INSTALL_DIR/app/"
find "$INSTALL_DIR/app" -name __pycache__ -type d -prune -exec rm -rf {} +
cp "$SRC/requirements.txt" "$INSTALL_DIR/app/requirements.txt"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade -r "$INSTALL_DIR/app/requirements.txt"
PYTHONPATH="$INSTALL_DIR/app" HCLUSTER_TOKEN=check "$INSTALL_DIR/venv/bin/python" -c 'import hcluster_agent; print("agent version", hcluster_agent.__version__)'
if [[ "$SKIP_SYSTEMD" != "1" ]]; then
  sed -e "s|/opt/hcluster-agent|$INSTALL_DIR|g" -e "s|/etc/hcluster-agent|$CONF_DIR|g" "$SRC/systemd/hcluster-agent.service" > "$UNIT_DIR/hcluster-agent.service"
  systemctl daemon-reload
  systemctl restart hcluster-agent.service
  sleep 2; systemctl --no-pager --lines=5 status hcluster-agent.service || true
fi
