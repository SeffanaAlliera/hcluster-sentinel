"""Node identity helpers for install.sh / update.sh (pure standard library: they run before any venv exists).

  python3 -m hcluster_agent.ident show   --config F --env F        print node id / api url / token credential id
  python3 -m hcluster_agent.ident set    --config F [--node-id ID] [--api-url URL]   edit identity IN PLACE (atomic)
  python3 -m hcluster_agent.ident verify --api-url URL --node-id ID      (token from $HCLUSTER_TOKEN)

`verify` is how the installer checks that a node id and a token BELONG TOGETHER: it sends one minimal heartbeat
(the same thing the agent sends at start-up) and interprets the answer. The token never appears on a command line.

verify exit codes: 0 pair accepted | 10 token rejected | 11 token belongs to a different node | 12 node disabled
                   20 inconclusive (network down, rate limited, server error): nothing could be proven either way
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
TOKEN_RE = re.compile(r"^hcs_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})_([A-Za-z0-9_-]{43})$")
URL_RE = re.compile(r"^(https://[A-Za-z0-9._:-]+(/.*)?|http://(127\.0\.0\.1|localhost)(:[0-9]+)?)$")


def _section(lines: list[str], name: str) -> tuple[int, int]:
    """[start, end) line range of the indented body of top-level `name:`; (-1, -1) if absent."""
    start = -1
    for i, ln in enumerate(lines):
        if re.match(rf"^{re.escape(name)}:\s*(#.*)?$", ln):
            start = i + 1
            break
    if start < 0:
        return -1, -1
    end = start
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t" or lines[end].lstrip().startswith("#")):
        end += 1
    return start, end


def _value(lines: list[str], section: str, key: str) -> str | None:
    s, e = _section(lines, section)
    if s < 0:
        return None
    for ln in lines[s:e]:
        m = re.match(rf"^\s+{re.escape(key)}:\s*([\"']?)([^\"'#\s]+)\1\s*(#.*)?$", ln)
        if m:
            return m.group(2)
    return None


def read_config_identity(path: str) -> tuple[str | None, str | None]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None, None
    nid, url = _value(lines, "node", "id"), _value(lines, "api", "url")
    return (nid if nid and UUID_RE.match(nid) else None), (url.rstrip("/") if url and URL_RE.match(url.rstrip("/")) else None)


def read_env_token(path: str) -> str | None:
    """The HCLUSTER_TOKEN value from an env file (last assignment wins, like systemd)."""
    tok = None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for ln in fh:
                m = re.match(r"^\s*HCLUSTER_TOKEN=(.*?)\s*$", ln)
                if m:
                    tok = m.group(1).strip().strip("\"'")
    except OSError:
        return None
    return tok or None


def token_credential_id(token: str | None) -> str | None:
    m = TOKEN_RE.match(token or "")
    return m.group(1) if m else None


def _atomic_write(path: str, text: str) -> None:
    st = os.stat(path)
    fd, tmp = tempfile.mkstemp(prefix=".ident-", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, st.st_mode & 0o7777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except (PermissionError, OSError):
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_config_identity(path: str, node_id: str | None = None, api_url: str | None = None) -> bool:
    """Replace node.id and/or api.url in place, leaving every other byte (services, comments) as it was."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    lines = text.split("\n")
    changed = False
    for section, key, new in (("node", "id", node_id), ("api", "url", api_url)):
        if new is None:
            continue
        s, e = _section(lines, section)
        if s < 0:
            raise ValueError(f"no `{section}:` section in {path}")
        for i in range(s, e):
            m = re.match(rf"^(\s+{key}:\s*)([\"']?)([^\"'#\s]+)([\"']?)(\s*(#.*)?)$", lines[i])
            if m:
                repl = f'{m.group(1)}"{new}"{m.group(5)}'
                if repl != lines[i]:
                    lines[i], changed = repl, True
                break
        else:
            raise ValueError(f"no `{key}:` under `{section}:` in {path}")
    if changed:
        _atomic_write(path, "\n".join(lines))
    return changed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def verify(api_url: str, node_id: str, token: str, timeout: float = 10.0) -> tuple[int, str]:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body = json.dumps({"schema": 1, "node_id": node_id,
                       "reports": [{"collected_at": now, "agent": {"version": "installer-verify"}}]}).encode()
    req = urllib.request.Request(f"{api_url.rstrip('/')}/api/agent/report", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                                          "User-Agent": "hcluster-agent-installer/1"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as resp:
            resp.read(2048)
            return 0, "accepted: this token belongs to this node"
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read(1024).decode("utf-8", "replace")).get("error", "")
        except Exception:  # noqa: BLE001
            pass
        if e.code == 401:
            return 10, "the API rejected this token (wrong, revoked or rotated)"
        if e.code == 403 and detail == "node_mismatch":
            return 11, "this token belongs to a DIFFERENT node than the node id you gave"
        if e.code == 403 and detail == "node_disabled":
            return 12, "the node is disabled in the dashboard (credentials are valid)"
        return 20, f"inconclusive: API answered HTTP {e.code} {detail}".strip()
    except Exception as e:  # noqa: BLE001
        return 20, f"inconclusive: cannot reach the API ({e.__class__.__name__}: {getattr(e, 'reason', e)})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m hcluster_agent.ident")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show")
    s.add_argument("--config", required=True)
    s.add_argument("--env", required=True)
    st = sub.add_parser("set")
    st.add_argument("--config", required=True)
    st.add_argument("--node-id")
    st.add_argument("--api-url")
    v = sub.add_parser("verify")
    v.add_argument("--api-url", required=True)
    v.add_argument("--node-id", required=True)
    v.add_argument("--timeout", type=float, default=10.0)
    a = ap.parse_args(argv)

    if a.cmd == "show":
        nid, url = read_config_identity(a.config)
        tok = read_env_token(a.env)
        print(f"NODE_ID={nid or ''}")
        print(f"API_URL={url or ''}")
        print(f"TOKEN_ID={token_credential_id(tok) or ''}")           # the credential id only, never the secret
        print(f"TOKEN_PRESENT={'1' if tok else '0'}")
        print(f"TOKEN_VALID={'1' if token_credential_id(tok) else '0'}")
        return 0
    if a.cmd == "set":
        if a.node_id and not UUID_RE.match(a.node_id):
            print("node id must be a UUID", file=sys.stderr)
            return 2
        if a.api_url and not URL_RE.match(a.api_url.rstrip("/")):
            print("api url must be https://...", file=sys.stderr)
            return 2
        try:
            print("changed" if set_config_identity(a.config, a.node_id, a.api_url.rstrip("/") if a.api_url else None) else "unchanged")
        except (OSError, ValueError) as e:
            print(f"cannot edit {a.config}: {e}", file=sys.stderr)
            return 2
        return 0
    token = os.environ.get("HCLUSTER_TOKEN", "")
    if not TOKEN_RE.match(token):
        print("HCLUSTER_TOKEN is not set to a valid hcs_ token", file=sys.stderr)
        return 2
    code, msg = verify(a.api_url, a.node_id, token, a.timeout)
    print(msg)
    return code


if __name__ == "__main__":
    sys.exit(main())
