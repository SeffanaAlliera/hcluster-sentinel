#!/usr/bin/env bash
# hCluster Sentinel agent installer (Debian/Ubuntu + systemd). Run from a checkout:
#
#   FRESH node:     sudo bash agent/install.sh --api-url https://<app>.vercel.app --node-id <uuid>     (token is prompted, hidden)
#                   sudo HCLUSTER_TOKEN=hcs_... bash agent/install.sh --api-url ... --node-id ... --yes   (non-interactive)
#   RE-RUN / repair on an existing node (safe; changes nothing about identity):
#                   sudo bash agent/install.sh
#   CHANGE the node id / token / API URL of an existing install (explicit, backed up, verified):
#                   sudo HCLUSTER_TOKEN=hcs_... bash agent/install.sh --reconfigure --node-id <uuid> [--api-url URL] --yes
#
# Identity rules (so a node id and a token can never silently end up mismatched):
#  * An existing config.yaml / agent.env is NEVER replaced unless you pass --reconfigure.
#  * Supplying a node id, API URL or token that DIFFERS from the installed one is an error, not an override.
#  * Before anything is written, the node id + token pair is checked against the API (one heartbeat; see docs).
#  * State (the offline queue) is never deleted. If the node id changes, the old queue is set aside, not sent
#    to the new node.
#
# Options: --api-url URL  --node-id UUID  --yes  --reconfigure  --no-discover  --skip-verify  --strict-verify
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SRC/lib/common.sh"

API_URL=""; NODE_ID=""; ASSUME_YES=0; RECONFIGURE=0; DISCOVER=1
SKIP_VERIFY="${HCLUSTER_SKIP_VERIFY:-0}"; STRICT_VERIFY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-url)        [[ $# -ge 2 ]] || die "--api-url needs a value" 2; API_URL="$2"; shift 2;;
    --node-id)        [[ $# -ge 2 ]] || die "--node-id needs a value" 2; NODE_ID="$2"; shift 2;;
    --yes|-y)         ASSUME_YES=1; shift;;
    --reconfigure)    RECONFIGURE=1; shift;;
    --no-discover)    DISCOVER=0; shift;;
    --skip-verify)    SKIP_VERIFY=1; shift;;
    --strict-verify)  STRICT_VERIFY=1; shift;;
    -h|--help)        sed -n '2,19p' "$0"; exit 0;;
    *) die "unknown option: $1 (see --help)" 2;;
  esac
done

require_root; check_layout; need_python
take_lock

# ---- 1. what is already installed? ------------------------------------------------------------------------------------
HAVE_CONF=0; HAVE_ENV=0; [[ -f "$CONF" ]] && HAVE_CONF=1; [[ -f "$ENVF" ]] && HAVE_ENV=1
EX_NODE=""; EX_API=""; EX_TOKEN=""
if [[ $HAVE_CONF -eq 1 || $HAVE_ENV -eq 1 ]]; then
  while IFS='=' read -r k v; do
    case "$k" in NODE_ID) EX_NODE="$v";; API_URL) EX_API="$v";; esac
  done < <(ident show --config "$CONF" --env "$ENVF")
  EX_TOKEN="$(read_env_token "$ENVF")"
fi
EXISTING=0; [[ $HAVE_CONF -eq 1 || $HAVE_ENV -eq 1 ]] && EXISTING=1
SUP_TOKEN="${HCLUSTER_TOKEN:-}"; API_URL="${API_URL%/}"

[[ -z "$NODE_ID" || "$NODE_ID" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] || die "Node ID must be a UUID" 2
[[ -z "$API_URL" || "$API_URL" =~ ^https://[A-Za-z0-9._:-]+(/.*)?$ || "$API_URL" =~ ^http://(127\.0\.0\.1|localhost)(:[0-9]+)?$ ]] || die "API URL must be https://..." 2
[[ -z "$SUP_TOKEN" || "$SUP_TOKEN" =~ ^hcs_[0-9a-f-]{36}_[A-Za-z0-9_-]{43}$ ]] || die "token does not look like an hCluster token (hcs_<id>_<secret>)" 2

if [[ $EXISTING -eq 1 ]]; then
  say "existing installation detected (config.yaml: $([[ $HAVE_CONF -eq 1 ]] && echo present || echo MISSING), agent.env: $([[ $HAVE_ENV -eq 1 ]] && echo present || echo MISSING))"
  [[ -z "$EX_NODE" ]] || say "installed node id: $EX_NODE"
  conflicts=()
  [[ -z "$NODE_ID" || -z "$EX_NODE" || "${NODE_ID,,}" == "${EX_NODE,,}" ]] || conflicts+=("node id: installed $EX_NODE, you gave $NODE_ID")
  [[ -z "$API_URL" || -z "$EX_API" || "$API_URL" == "$EX_API" ]] || conflicts+=("API URL: installed $EX_API, you gave $API_URL")
  [[ -z "$SUP_TOKEN" || -z "$EX_TOKEN" || "$SUP_TOKEN" == "$EX_TOKEN" ]] || conflicts+=("token: the token you supplied differs from the installed one")
  if [[ ${#conflicts[@]} -gt 0 && $RECONFIGURE -eq 0 ]]; then
    printf '\033[1;31merror:\033[0m refusing to replace the identity of an existing node:\n' >&2
    printf '  - %s\n' "${conflicts[@]}" >&2
    cat >&2 <<MSG

Nothing was changed. If you really want to switch this machine to the values above, say so explicitly:
  sudo HCLUSTER_TOKEN=<token of that node> bash agent/install.sh --reconfigure --node-id <uuid> --yes
(the old config.yaml / agent.env are backed up first). To just repair/refresh the existing install, run
  sudo bash agent/install.sh        (no identity options)
MSG
    exit 3
  fi
fi

# ---- 2. the identity we are going to end up with ----------------------------------------------------------------------------
F_NODE="${NODE_ID:-$EX_NODE}"; F_API="${API_URL:-$EX_API}"; F_TOKEN="${SUP_TOKEN:-$EX_TOKEN}"
if [[ $RECONFIGURE -eq 1 && -n "$EX_NODE" && -n "$NODE_ID" && "${NODE_ID,,}" != "${EX_NODE,,}" && -z "$SUP_TOKEN" ]]; then
  die "changing the node id needs the NEW node's token (HCLUSTER_TOKEN=...): the installed token belongs to $EX_NODE" 2
fi
if [[ -z "$F_API" ]];  then [[ $ASSUME_YES -eq 1 ]] && die "--api-url required" 2; read -rp "Sentinel API URL (https://<your-app>.vercel.app): " F_API; F_API="${F_API%/}"; fi
if [[ -z "$F_NODE" ]]; then [[ $ASSUME_YES -eq 1 ]] && die "--node-id required" 2; read -rp "Node ID (UUID from the dashboard): " F_NODE; fi
if [[ -z "$F_TOKEN" ]]; then [[ $ASSUME_YES -eq 1 ]] && die "HCLUSTER_TOKEN required" 2; read -rsp "Node API token (hidden): " F_TOKEN; echo; fi
[[ "$F_API" =~ ^https://[A-Za-z0-9._:-]+(/.*)?$ || "$F_API" =~ ^http://(127\.0\.0\.1|localhost)(:[0-9]+)?$ ]] || die "API URL must be https://..." 2
[[ "$F_NODE" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] || die "Node ID must be a UUID" 2
[[ "$F_TOKEN" =~ ^hcs_[0-9a-f-]{36}_[A-Za-z0-9_-]{43}$ ]] || die "token does not look like an hCluster token (hcs_<id>_<secret>)" 2

CHANGED_NODE=0; CHANGED_API=0; CHANGED_TOKEN=0
[[ -n "$EX_NODE"  && "${F_NODE,,}" != "${EX_NODE,,}" ]] && CHANGED_NODE=1
[[ -n "$EX_API"   && "$F_API" != "$EX_API" ]] && CHANGED_API=1
[[ -n "$EX_TOKEN" && "$F_TOKEN" != "$EX_TOKEN" ]] && CHANGED_TOKEN=1
WRITE_CONF=0; WRITE_ENV=0
[[ $HAVE_CONF -eq 0 || $CHANGED_NODE -eq 1 || $CHANGED_API -eq 1 ]] && WRITE_CONF=1
[[ $HAVE_ENV  -eq 0 || $CHANGED_TOKEN -eq 1 ]] && WRITE_ENV=1

# ---- 3. prove the node id and the token belong together BEFORE writing anything ---------------------------------------
if [[ $WRITE_CONF -eq 1 || $WRITE_ENV -eq 1 ]] && [[ "$SKIP_VERIFY" != "1" ]]; then
  say "verifying that this token belongs to node $F_NODE (sends one heartbeat)"
  set +e; vout="$(HCLUSTER_TOKEN="$F_TOKEN" ident verify --api-url "$F_API" --node-id "$F_NODE")"; vrc=$?; set -e
  case $vrc in
    0)     say "$vout";;
    10|11) die "$vout. Nothing was changed." 4;;
    12)    warn "$vout";;
    *)     if [[ $STRICT_VERIFY -eq 1 ]]; then die "$vout (--strict-verify)" 5; fi
           warn "$vout. Continuing: the agent keeps retrying, but double-check the node id and token.";;
  esac
fi

# ---- 4. users, directories (state is NEVER removed or emptied) ------------------------------------------------------------
if [[ $MANAGE_SYSTEM -eq 1 ]]; then
  id "$SVC_USER" &>/dev/null || { say "creating system user $SVC_USER"; useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER"; }
  if ! "$PY_SYS" -c 'import venv, ensurepip' 2>/dev/null; then say "installing python3-venv"; apt-get update -qq && apt-get install -y -qq python3-venv; fi
fi
install -d -m 0755 "$INSTALL_DIR" "$CONF_DIR"
install -d -m 0750 "$STATE_DIR"
[[ $MANAGE_SYSTEM -eq 0 ]] || chown "$SVC_USER:$SVC_USER" "$STATE_DIR"

# ---- 5. candidate identity files (written next to the real ones, validated, then moved into place) ------------------------
CAND_CONF="$CONF"; CAND_ENV="$ENVF"
trap 'rm -f "$CONF_DIR/.config.yaml.new" "$CONF_DIR"/.agent.env.* 2>/dev/null || true; cleanup_stage' EXIT
if [[ $WRITE_CONF -eq 1 ]]; then
  CAND_CONF="$CONF_DIR/.config.yaml.new"
  if [[ $HAVE_CONF -eq 1 ]]; then
    cp -p "$CONF" "$CAND_CONF"
    ident set --config "$CAND_CONF" --node-id "$F_NODE" --api-url "$F_API" >/dev/null
  else
    sed -e "s|00000000-0000-0000-0000-000000000000|$F_NODE|" -e "s|https://YOUR-PROJECT.vercel.app|$F_API|" \
        -e "s|^state_dir: .*|state_dir: $STATE_DIR|" "$SRC/config.example.yaml" > "$CAND_CONF"
    chmod 0644 "$CAND_CONF"
  fi
fi
if [[ $WRITE_ENV -eq 1 ]]; then
  CAND_ENV="$CONF_DIR/.agent.env.new"
  write_env_token "$CAND_ENV" "$F_TOKEN"
fi

# ---- 6. new code, validated against the candidate identity, then swapped in ---------------------------------------------------
say "installing agent code to $INSTALL_DIR"
deploy_code "$CAND_CONF" "$CAND_ENV" || die "installation aborted before changing anything that was working (see above)" 1

# ---- 7. commit identity (existing files are backed up; the old offline queue is set aside if the node changed) -------------------
if [[ $WRITE_CONF -eq 1 && $HAVE_CONF -eq 1 ]]; then b="$(backup_file "$CONF")"; say "backed up config.yaml -> $b"; fi
if [[ $WRITE_ENV  -eq 1 && $HAVE_ENV  -eq 1 ]]; then b="$(backup_file "$ENVF")"; say "backed up agent.env -> $b"; fi
[[ $WRITE_CONF -eq 0 ]] || mv -f "$CAND_CONF" "$CONF"
[[ $WRITE_ENV  -eq 0 ]] || mv -f "$CAND_ENV" "$ENVF"
if [[ $CHANGED_NODE -eq 1 && -f "$STATE_DIR/queue.json" ]]; then
  aside="$STATE_DIR/queue.node-${EX_NODE:0:8}.json.bak"
  mv -f "$STATE_DIR/queue.json" "$aside"
  say "the offline queue belongs to the OLD node id; kept (not sent, not deleted): $aside"
fi
if [[ $EXISTING -eq 1 ]]; then
  if [[ $WRITE_CONF -eq 0 && $WRITE_ENV -eq 0 ]]; then say "kept existing config.yaml and agent.env (identity untouched)"; else say "identity updated (backups next to the files: *.bak-*)"; fi
fi
write_layout; write_wrapper
FRESH=$(( EXISTING == 0 ))

# ---- 8. service discovery on a FRESH install only (re-running install never edits an existing service list) --------------------
if [[ $FRESH -eq 1 && $DISCOVER -eq 1 ]]; then
  say "discovering services on this machine"
  "$BIN_LINK" discover-services --no-restart || warn "automatic discovery failed (the agent still works; add services by hand or run: sudo hcluster-agent discover-services)"
fi

say "validating configuration"
"$BIN_LINK" check-config || { rollback_code; die "configuration invalid after install; previous code restored" 1; }

if [[ $USE_SYSTEMD -eq 0 ]]; then commit_code; say "HCLUSTER_SKIP_SYSTEMD=1: not installing the service. Done."; exit 0; fi

# ---- 9. systemd ------------------------------------------------------------------------------------------------------------------
UNIT_BAK=""
if [[ -f "$UNIT_FILE" ]]; then UNIT_BAK="$INSTALL_DIR/.unit.bak"; cp -p "$UNIT_FILE" "$UNIT_BAK"; fi
mkdir -p "$UNIT_DIR"; render_unit > "$UNIT_FILE"
if [[ $EXISTING -eq 1 ]] && { sc is-active --quiet hcluster-agent.service || sc is-enabled --quiet hcluster-agent.service; }; then
  restart_or_rollback "$UNIT_BAK" || die "the service did not start with the new code/unit; the previous version was restored (see: journalctl -u hcluster-agent)" 1
else
  sc daemon-reload; sc enable --now hcluster-agent.service
  wait_active || { sc --no-pager --lines=15 status hcluster-agent.service || true; die "the service did not stay up; see: journalctl -u hcluster-agent -n 50" 1; }
fi
commit_code; rm -f "$UNIT_BAK"
sc --no-pager --lines=6 status hcluster-agent.service || true

cat <<MSG

Installed. Next steps:
  1. Review what was discovered / add more:   sudo hcluster-agent discover-services --dry-run     (or: sudo hcluster-agent discover)
  2. Edit $CONF_DIR/config.yaml  then  sudo systemctl restart hcluster-agent
  3. Watch it:  journalctl -u hcluster-agent -f      (the node should turn ONLINE in the dashboard within ~1 minute)
  Change identity later (explicit): sudo HCLUSTER_TOKEN=... bash agent/install.sh --reconfigure --node-id <uuid> --yes
MSG
