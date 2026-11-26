# shellcheck shell=bash
# Shared helpers for install.sh / update.sh / uninstall.sh. Sourced, never executed.
# The caller must set SRC (the agent/ directory of the checkout) before sourcing.

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$1" >&2; exit "${2:-1}"; }

# ---- layout -------------------------------------------------------------------------------------------
# Every path can be overridden with HCLUSTER_*; install.sh records what it used in $INSTALL_DIR/layout.env so
# update.sh / uninstall.sh find the same CONFIG / STATE / UNIT / BIN paths without re-typing them.
INSTALL_DIR="${HCLUSTER_INSTALL_DIR:-/opt/hcluster-agent}"
_recorded() { local f="$INSTALL_DIR/layout.env"; [[ -r "$f" ]] || return 0; sed -n "s/^$1=//p" "$f" | head -n1; }
CONF_DIR="${HCLUSTER_CONF_DIR:-$(_recorded CONF_DIR)}";    CONF_DIR="${CONF_DIR:-/etc/hcluster-agent}"
STATE_DIR="${HCLUSTER_STATE_DIR:-$(_recorded STATE_DIR)}"; STATE_DIR="${STATE_DIR:-/var/lib/hcluster-agent}"
UNIT_DIR="${HCLUSTER_UNIT_DIR:-$(_recorded UNIT_DIR)}";    UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
BIN_LINK="${HCLUSTER_BIN_LINK:-$(_recorded BIN_LINK)}";    BIN_LINK="${BIN_LINK:-/usr/local/bin/hcluster-agent}"
SVC_USER="hcluster"
UNIT_FILE="$UNIT_DIR/hcluster-agent.service"
CONF="$CONF_DIR/config.yaml"
ENVF="$CONF_DIR/agent.env"

# HCLUSTER_SKIP_SYSTEMD=1  : no root, no systemctl, no user changes (used by tests and containers)
# HCLUSTER_ALLOW_NONROOT=1 : test hook: no root and no user/apt changes, but systemctl IS used (set HCLUSTER_SYSTEMCTL to a stub)
SKIP_SYSTEMD="${HCLUSTER_SKIP_SYSTEMD:-0}"
ALLOW_NONROOT="${HCLUSTER_ALLOW_NONROOT:-0}"
SYSTEMCTL="${HCLUSTER_SYSTEMCTL:-systemctl}"
USE_SYSTEMD=1;   [[ "$SKIP_SYSTEMD" == "1" ]] && USE_SYSTEMD=0
MANAGE_SYSTEM=1; [[ "$SKIP_SYSTEMD" == "1" || "$ALLOW_NONROOT" == "1" ]] && MANAGE_SYSTEM=0
PY_SYS="${HCLUSTER_PYTHON:-python3}"
VENV_FLAGS="${HCLUSTER_VENV_FLAGS:-}"          # tests: "--system-site-packages --without-pip"
sc() { "$SYSTEMCTL" "$@"; }

check_path() {   # name value: absolute, no characters that would break sed/rm, never a system directory
  local v="$2"
  [[ "$v" == /* && "$v" != *[[:space:]]* && "$v" != *"|"* && "$v" != *"&"* && "$v" != *"\\"* ]] \
    || die "$1 must be an absolute path without spaces or | & \\ (got: '$v')" 2
  case "${v%/}" in
    ""|/|/etc|/opt|/var|/var/lib|/usr|/usr/local|/usr/local/bin|/home|/root|/bin|/sbin|/lib|/boot|/tmp|/srv|/mnt|/run|/dev|/proc|/sys)
      die "$1=$v is a system directory; refusing" 2;;
  esac
}
check_layout() { check_path INSTALL_DIR "$INSTALL_DIR"; check_path CONF_DIR "$CONF_DIR"; check_path STATE_DIR "$STATE_DIR"
                 check_path UNIT_DIR "$UNIT_DIR"; check_path BIN_LINK "$BIN_LINK"; }

require_root() { [[ "$SKIP_SYSTEMD" == "1" || "$ALLOW_NONROOT" == "1" || $EUID -eq 0 ]] || die "run as root (sudo $0 ...)" 2; }
need_python() {
  command -v "$PY_SYS" >/dev/null || die "python3 is required (sudo apt install python3)" 2
  "$PY_SYS" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' || die "Python >= 3.9 required" 2
}
take_lock() {    # one install/update/uninstall at a time
  mkdir -p "$(dirname "$INSTALL_DIR")"
  command -v flock >/dev/null || { warn "flock not found: concurrent runs are not guarded"; return 0; }
  exec 9>"${INSTALL_DIR%/}.lock" || die "cannot create ${INSTALL_DIR%/}.lock"
  flock -n 9 || die "another hcluster-agent install/update/uninstall is already running" 6
}

# ---- identity files --------------------------------------------------------------------------------------
read_env_token() { sed -n 's/^[[:space:]]*HCLUSTER_TOKEN=//p' "$1" 2>/dev/null | tail -n1 | tr -d "\"'" || true; }
ident() { PYTHONPATH="$SRC" "$PY_SYS" -m hcluster_agent.ident "$@"; }
write_env_token() {   # file token  (atomic, 0640 root:hcluster or 0600 when not managing users)
  local f="$1" tmp; tmp="$(mktemp "$(dirname "$f")/.agent.env.XXXXXX")"
  ( umask 077; printf 'HCLUSTER_TOKEN=%s\n' "$2" > "$tmp" )
  if [[ $MANAGE_SYSTEM -eq 1 ]]; then chown "root:$SVC_USER" "$tmp"; chmod 0640 "$tmp"; else chmod 0600 "$tmp"; fi
  mv -f "$tmp" "$f"
}
backup_file() {       # keeps the 5 newest backups of a file; prints the backup path
  local f="$1" b; [[ -f "$f" ]] || return 0
  b="$f.bak-$(date +%Y%m%d-%H%M%S)"; cp -p "$f" "$b"
  ls -1t "$f".bak-* 2>/dev/null | tail -n +6 | while read -r old; do rm -f "$old"; done
  echo "$b"
}

# ---- code + virtualenv: stage, validate, swap, roll back ---------------------------------------------------
cleanup_stage() { rm -rf "$INSTALL_DIR/.app.new" "$INSTALL_DIR/.venv.new"; }

# deploy_code <config> <env>: build the new code (and a NEW virtualenv only if requirements changed), prove it
# imports and accepts <config>/<env>, and only then swap it in. The previous app/ and venv/ are kept as
# app.old / venv.old until commit_code (or restored by rollback_code). Nothing live is touched on failure.
deploy_code() {
  local cfg="${1:-}" envf="${2:-}" stage="$INSTALL_DIR/.app.new" py new_venv=0
  cleanup_stage; mkdir -p "$stage"
  cp -r "$SRC/hcluster_agent" "$stage/"; cp "$SRC/requirements.txt" "$stage/requirements.txt"
  find "$stage" -name __pycache__ -type d -prune -exec rm -rf {} +
  py="$INSTALL_DIR/venv/bin/python"
  if [[ ! -x "$py" ]] || ! cmp -s "$SRC/requirements.txt" "$INSTALL_DIR/app/requirements.txt" 2>/dev/null; then new_venv=1; fi
  if [[ $new_venv -eq 1 ]]; then
    say "building a fresh virtualenv (first install or changed dependencies)"
    # shellcheck disable=SC2086
    if ! "$PY_SYS" -m venv $VENV_FLAGS "$INSTALL_DIR/.venv.new"; then cleanup_stage; return 1; fi
    py="$INSTALL_DIR/.venv.new/bin/python"
    if ! "$py" -c 'import yaml' 2>/dev/null; then
      if ! "$INSTALL_DIR/.venv.new/bin/pip" install --quiet -r "$stage/requirements.txt"; then cleanup_stage; warn "dependency install failed; nothing was changed"; return 1; fi
    fi
  fi
  if ! PYTHONPATH="$stage" "$py" -c 'import hcluster_agent, yaml' 2>/dev/null; then cleanup_stage; warn "new code does not import; nothing was changed"; return 1; fi
  if [[ -n "$cfg" && -f "$cfg" && -n "$envf" && -f "$envf" ]]; then
    if ! ( export HCLUSTER_TOKEN; HCLUSTER_TOKEN="$(read_env_token "$envf")"; PYTHONPATH="$stage" "$py" -m hcluster_agent -c "$cfg" check-config ); then
      cleanup_stage; warn "the configuration is not valid for the new code; nothing was changed"; return 1
    fi
  fi
  rm -rf "$INSTALL_DIR/app.old" "$INSTALL_DIR/venv.old"
  [[ ! -d "$INSTALL_DIR/app" ]] || mv "$INSTALL_DIR/app" "$INSTALL_DIR/app.old"
  mv "$stage" "$INSTALL_DIR/app"
  if [[ $new_venv -eq 1 ]]; then
    [[ ! -d "$INSTALL_DIR/venv" ]] || mv "$INSTALL_DIR/venv" "$INSTALL_DIR/venv.old"
    mv "$INSTALL_DIR/.venv.new" "$INSTALL_DIR/venv"
  fi
  return 0
}
rollback_code() {
  if [[ -d "$INSTALL_DIR/app.old" ]]; then rm -rf "$INSTALL_DIR/app"; mv "$INSTALL_DIR/app.old" "$INSTALL_DIR/app"; fi
  if [[ -d "$INSTALL_DIR/venv.old" ]]; then rm -rf "$INSTALL_DIR/venv"; mv "$INSTALL_DIR/venv.old" "$INSTALL_DIR/venv"; fi
}
commit_code() {   # keep the previous code as app.prev for manual recovery; the old venv is dropped
  rm -rf "$INSTALL_DIR/venv.old" "$INSTALL_DIR/app.prev"
  [[ ! -d "$INSTALL_DIR/app.old" ]] || mv "$INSTALL_DIR/app.old" "$INSTALL_DIR/app.prev"
}

# ---- wrapper, layout record, unit ------------------------------------------------------------------------------
write_layout() {
  ( umask 022; printf 'INSTALL_DIR=%s\nCONF_DIR=%s\nSTATE_DIR=%s\nUNIT_DIR=%s\nBIN_LINK=%s\n' \
      "$INSTALL_DIR" "$CONF_DIR" "$STATE_DIR" "$UNIT_DIR" "$BIN_LINK" > "$INSTALL_DIR/layout.env" )
}
write_wrapper() {   # `sudo hcluster-agent discover | once | check-config | discover-services`
  mkdir -p "$(dirname "$BIN_LINK")"
  cat > "$BIN_LINK" <<WRAP
#!/usr/bin/env bash
# convenience wrapper written by install.sh/update.sh: runs the agent CLI with this node's config and token
if [[ -r "$ENVF" ]]; then
  HCLUSTER_TOKEN="\$(sed -n 's/^[[:space:]]*HCLUSTER_TOKEN=//p' "$ENVF" | tail -n1 | tr -d "\\"'")"; export HCLUSTER_TOKEN
fi
PYTHONPATH="$INSTALL_DIR/app" exec "$INSTALL_DIR/venv/bin/python" -m hcluster_agent -c "$CONF" "\$@"
WRAP
  chmod 0755 "$BIN_LINK"
}
render_unit() {
  sed -e "s|/opt/hcluster-agent|$INSTALL_DIR|g" -e "s|/etc/hcluster-agent|$CONF_DIR|g" -e "s|/var/lib/hcluster-agent|$STATE_DIR|g" \
      "$SRC/systemd/hcluster-agent.service"
}

# ---- service control --------------------------------------------------------------------------------------------------
wait_active() {   # active, and still active 2 s later (a crash-looping unit passes `restart` once)
  local i
  for i in $(seq 1 "${HCLUSTER_WAIT_SECONDS:-20}"); do
    if sc is-active --quiet hcluster-agent.service; then sleep "${HCLUSTER_SETTLE_SECONDS:-2}"; sc is-active --quiet hcluster-agent.service && return 0; fi
    sleep 1
  done
  return 1
}
# restart_or_rollback <unit-backup|""> : restart with the new code/unit; if it does not come up, put the old
# code and unit back and start THAT, so a failed update never leaves the node without a working agent.
restart_or_rollback() {
  local unit_bak="${1:-}"
  sc daemon-reload
  if sc restart hcluster-agent.service && wait_active; then return 0; fi
  warn "the new version did not start: rolling back to the previous version"
  sc stop hcluster-agent.service >/dev/null 2>&1 || true
  rollback_code
  if [[ -n "$unit_bak" && -f "$unit_bak" ]]; then cp -p "$unit_bak" "$UNIT_FILE"; fi
  sc daemon-reload
  sc restart hcluster-agent.service >/dev/null 2>&1 || true
  return 1
}
