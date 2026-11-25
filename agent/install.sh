#!/usr/bin/env bash
# hCluster Sentinel agent installer (Ubuntu/Debian + systemd). Run from the repo's agent/ directory:
#   sudo ./install.sh                       # interactive
#   sudo ./install.sh --api-url https://x.vercel.app --node-id <uuid>      # token is prompted (hidden)
#   sudo HCLUSTER_TOKEN=hcs_... ./install.sh --api-url ... --node-id ... --yes   # non-interactive
# Safe to re-run: it never overwrites an existing config.yaml.
set -euo pipefail

INSTALL_DIR="${HCLUSTER_INSTALL_DIR:-/opt/hcluster-agent}"
CONF_DIR="${HCLUSTER_CONF_DIR:-/etc/hcluster-agent}"
STATE_DIR="${HCLUSTER_STATE_DIR:-/var/lib/hcluster-agent}"
UNIT_DIR="${HCLUSTER_UNIT_DIR:-/etc/systemd/system}"
BIN_LINK="${HCLUSTER_BIN_LINK:-/usr/local/bin/hcluster-agent}"
SKIP_SYSTEMD="${HCLUSTER_SKIP_SYSTEMD:-0}"        # 1 = no root/systemd/user changes (used by tests)
SVC_USER="hcluster"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

API_URL=""; NODE_ID=""; ASSUME_YES=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-url) API_URL="$2"; shift 2;;
    --node-id) NODE_ID="$2"; shift 2;;
    --yes|-y)  ASSUME_YES=1; shift;;
    -h|--help) sed -n '2,8p' "$0"; exit 0;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

if [[ "$SKIP_SYSTEMD" != "1" && $EUID -ne 0 ]]; then die "run as root (sudo ./install.sh)"; fi
command -v python3 >/dev/null || die "python3 is required (sudo apt install python3)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' || die "Python >= 3.9 required"

# ---- gather settings --------------------------------------------------------
[[ -n "$API_URL" ]] || { [[ $ASSUME_YES -eq 1 ]] && die "--api-url required"; read -rp "Sentinel API URL (https://<your-app>.vercel.app): " API_URL; }
[[ -n "$NODE_ID" ]] || { [[ $ASSUME_YES -eq 1 ]] && die "--node-id required"; read -rp "Node ID (UUID from the dashboard): " NODE_ID; }
TOKEN="${HCLUSTER_TOKEN:-}"
[[ -n "$TOKEN" ]] || { [[ $ASSUME_YES -eq 1 ]] && die "HCLUSTER_TOKEN required"; read -rsp "Node API token (hidden): " TOKEN; echo; }

API_URL="${API_URL%/}"
[[ "$API_URL" =~ ^https://[A-Za-z0-9._:-]+(/.*)?$ || "$API_URL" =~ ^http://(127\.0\.0\.1|localhost)(:[0-9]+)?$ ]] || die "API URL must be https://..."
[[ "$NODE_ID" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] || die "Node ID must be a UUID"
[[ "$TOKEN" =~ ^hcs_[0-9a-f-]{36}_[A-Za-z0-9_-]{43}$ ]] || die "token does not look like an hCluster token (hcs_<id>_<secret>)"

# ---- system user + dirs ---------------------------------------------------------
if [[ "$SKIP_SYSTEMD" != "1" ]]; then
  if ! id "$SVC_USER" &>/dev/null; then say "creating system user $SVC_USER"; useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER"; fi
  if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then say "installing python3-venv"; apt-get update -qq && apt-get install -y -qq python3-venv; fi
fi
install -d -m 0755 "$INSTALL_DIR/app" "$CONF_DIR"
install -d -m 0750 "$STATE_DIR"
[[ "$SKIP_SYSTEMD" == "1" ]] || chown "$SVC_USER:$SVC_USER" "$STATE_DIR"

# ---- code + venv ----------------------------------------------------------------
say "installing agent code to $INSTALL_DIR"
rm -rf "$INSTALL_DIR/app/hcluster_agent"
cp -r "$SRC/hcluster_agent" "$INSTALL_DIR/app/"
find "$INSTALL_DIR/app" -name __pycache__ -type d -prune -exec rm -rf {} +
cp "$SRC/requirements.txt" "$INSTALL_DIR/app/requirements.txt"
[[ -d "$INSTALL_DIR/venv" ]] || python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/app/requirements.txt"

# ---- config (never overwritten) + secret env file ---------------------------------
if [[ -f "$CONF_DIR/config.yaml" ]]; then
  say "keeping existing $CONF_DIR/config.yaml"
else
  say "writing $CONF_DIR/config.yaml from template"
  sed -e "s|00000000-0000-0000-0000-000000000000|$NODE_ID|" -e "s|https://YOUR-PROJECT.vercel.app|$API_URL|" \
      -e "s|^state_dir: .*|state_dir: $STATE_DIR|" "$SRC/config.example.yaml" > "$CONF_DIR/config.yaml"
  chmod 0644 "$CONF_DIR/config.yaml"
fi
say "writing token to $CONF_DIR/agent.env (0640)"
( umask 027; printf 'HCLUSTER_TOKEN=%s\n' "$TOKEN" > "$CONF_DIR/agent.env" )
if [[ "$SKIP_SYSTEMD" == "1" ]]; then chmod 0600 "$CONF_DIR/agent.env"; else chown "root:$SVC_USER" "$CONF_DIR/agent.env"; chmod 0640 "$CONF_DIR/agent.env"; fi
unset TOKEN

# ---- CLI wrapper (needs read access to agent.env, i.e. run with sudo) ---------------
cat > "$BIN_LINK" <<WRAP
#!/usr/bin/env bash
# convenience wrapper: sudo hcluster-agent discover | once | check-config
set -a; . "$CONF_DIR/agent.env"; set +a
PYTHONPATH="$INSTALL_DIR/app" exec "$INSTALL_DIR/venv/bin/python" -m hcluster_agent -c "$CONF_DIR/config.yaml" "\$@"
WRAP
chmod 0755 "$BIN_LINK"

say "validating configuration"
"$BIN_LINK" check-config

if [[ "$SKIP_SYSTEMD" == "1" ]]; then say "HCLUSTER_SKIP_SYSTEMD=1: not installing the service. Done."; exit 0; fi

# ---- systemd -------------------------------------------------------------------------
sed -e "s|/opt/hcluster-agent|$INSTALL_DIR|g" -e "s|/etc/hcluster-agent|$CONF_DIR|g" -e "s|/var/lib/hcluster-agent|$STATE_DIR|g" \
    "$SRC/systemd/hcluster-agent.service" > "$UNIT_DIR/hcluster-agent.service"
systemctl daemon-reload
systemctl enable --now hcluster-agent.service
sleep 3
systemctl --no-pager --lines=8 status hcluster-agent.service || true

cat <<MSG

Installed. Next steps:
  1. Find the REAL ports on this host and fix the service URLs:   sudo hcluster-agent discover
  2. Edit $CONF_DIR/config.yaml  then  sudo systemctl restart hcluster-agent
  3. Watch it:  journalctl -u hcluster-agent -f      (the node should turn ONLINE in the dashboard within ~1 minute)
MSG
