"""Automatic service discovery: find common services on this machine and add health checks for them to config.yaml.

Design rules (see docs/DEPLOYMENT.md "Service discovery"):
  * signatures live in YAML (signatures.default.yaml, plus an optional <config dir>/signatures.yaml / --signatures file)
  * an application is NAMED only on strong evidence (systemd unit, process owning the port, HTTP answer signature);
    a port number alone never names an app
  * nothing is sent to the network except unauthenticated GETs to LOCAL listening ports; no credentials are read or used
  * config.yaml is edited TEXTUALLY (comments/manual entries stay byte-identical), only new entries are appended inside a
    managed block, after a backup, via an atomic replace, validated before and after, rolled back on any failure
  * existing services are never modified or removed; services that disappear are only reported
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

import yaml

from .config import KEY_RE, LOCAL_HOSTS, ConfigError, load_config

BEGIN = "# >>> hcluster-discovery (managed block: `hcluster-agent discover-services` appends new checks here; edit or delete them freely) >>>"
END = "# <<< hcluster-discovery <<<"
DEFAULT_SIGNATURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signatures.default.yaml")
LOOPBACK = {"127.0.0.1", "::1"}
MAX_UNIDENTIFIED_PROBES = 20


# ---------------------------------------------------------------------------------------------------------------- data
@dataclass
class Listener:
    port: int
    addr: str                  # bind address as seen in /proc ("0.0.0.0", "::", "127.0.0.1", "192.168.1.5", "::1")
    process: str = ""

    @property
    def loopback_only(self) -> bool:
        return self.addr in LOOPBACK or self.addr.startswith("127.")

    @property
    def host(self) -> str:
        if self.addr in ("0.0.0.0", "::", "v6") or self.addr.startswith("127."):
            return "127.0.0.1"
        return self.addr


@dataclass
class Found:
    entry: dict                # the services: item to add
    evidence: str
    port: int | None = None
    status: str = "new"        # new | monitored (already covered by an existing check)


@dataclass
class Result:
    found: list[Found] = field(default_factory=list)
    unidentified: list[dict] = field(default_factory=list)      # {port, process, addr, hint, http}
    skipped: list[str] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)           # existing checks whose app is no longer detected
    added: list[str] = field(default_factory=list)
    backup: str | None = None
    restarted: bool = False
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------------------------------------------------ signatures
def load_signatures(extra_paths: list[str] | None = None) -> list[dict]:
    sigs: dict[str, dict] = {}
    order: list[str] = []
    for path in [DEFAULT_SIGNATURES] + [p for p in (extra_paths or []) if p and os.path.exists(p)]:
        try:
            with open(path, encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError) as e:
            raise ConfigError(f"cannot load signatures {path}: {e}") from e
        for s in raw.get("signatures") or []:
            if not isinstance(s, dict) or not KEY_RE.match(str(s.get("id", ""))):
                raise ConfigError(f"{path}: every signature needs an id matching {KEY_RE.pattern}")
            for rx in list(s.get("units") or []) + ([s["description"]] if s.get("description") else []):
                try:
                    re.compile(rx)
                except re.error as e:
                    raise ConfigError(f"{path}: signature {s['id']}: bad regex {rx!r}: {e}") from e
            if s["id"] not in sigs:
                order.append(s["id"])
            sigs[s["id"]] = s
    return [sigs[i] for i in order]


# ---------------------------------------------------------------------------------------------------------------- scanning
class Scanner:
    """All machine/network access goes through here so tests can swap it (or use HCLUSTER_DISCOVERY_FIXTURE)."""

    def __init__(self, proc: str = "/proc", run: Callable | None = None):
        self.proc, self._run = proc, run or subprocess.run

    def systemd_units(self) -> list[tuple[str, str]]:
        try:
            r = self._run(["systemctl", "list-units", "--type=service", "--state=running", "--no-legend", "--plain", "--no-pager", "--full"],
                          capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return []
        out = []
        for ln in (r.stdout or "").splitlines():
            parts = ln.split(None, 4)
            if len(parts) >= 4 and parts[0].endswith(".service"):
                out.append((parts[0][:-8], parts[4] if len(parts) > 4 else ""))
        return out

    def listeners(self) -> list[Listener]:
        from .discover import listening_ports
        res = {}
        for port, addr, proc in listening_ports(self.proc):
            res.setdefault((port, addr), Listener(port, addr, "" if proc == "?" else proc))
        return sorted(res.values(), key=lambda l: (l.port, l.addr))

    def ephemeral_min(self) -> int:
        try:
            with open(f"{self.proc}/sys/net/ipv4/ip_local_port_range") as fh:
                return int(fh.read().split()[0])
        except (OSError, ValueError, IndexError):
            return 32768

    def http_get(self, scheme: str, host: str, port: int, path: str, timeout: float = 3.0) -> dict | None:
        """One unauthenticated GET to a local listener. TLS is not verified (we only identify the app; nothing secret is sent)."""
        url = f"{scheme}://{'[' + host + ']' if ':' in host else host}:{port}{path}"
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ctx))
        try:
            with opener.open(urllib.request.Request(url, headers={"User-Agent": "hcluster-agent-discovery"}), timeout=timeout) as r:
                return {"status": r.status, "headers": dict(r.headers), "body": r.read(65536).decode("utf-8", "replace")}
        except urllib.error.HTTPError as e:
            try:
                body = e.read(65536).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                body = ""
            return {"status": e.code, "headers": dict(e.headers or {}), "body": body}
        except (urllib.error.URLError, OSError, ValueError, ssl.SSLError):
            return None


class FixtureScanner(Scanner):
    """Test hook: HCLUSTER_DISCOVERY_FIXTURE=<json file> replaces systemd, /proc and the network. Never used unless set."""

    def __init__(self, path: str):
        with open(path, encoding="utf-8") as fh:
            self.fx = json.load(fh)
        self.http_log: list[str] = []

    def systemd_units(self):
        return [(u["unit"].removesuffix(".service"), u.get("description", "")) for u in self.fx.get("systemd", [])]

    def listeners(self):
        return [Listener(int(l["port"]), l.get("addr", "0.0.0.0"), l.get("process", "")) for l in self.fx.get("listeners", [])]

    def ephemeral_min(self):
        return int(self.fx.get("ephemeral_min", 32768))

    def http_get(self, scheme, host, port, path, timeout=3.0):
        self.http_log.append(f"{scheme}://{host}:{port}{path}")
        r = (self.fx.get("http") or {}).get(str(port))
        if r is None:
            return None
        if "status" not in r:
            r = r.get(path)
        if r is None:
            return None
        if r.get("scheme") and r["scheme"] != scheme:
            return None
        return {"status": r["status"], "headers": r.get("headers", {}), "body": r.get("body", "")}


# --------------------------------------------------------------------------------------------------------------- matching
def _http_matches(sig: dict, resp: dict | None) -> bool:
    h = sig.get("http") or {}
    if not resp:
        return False
    if not (h.get("body_contains") or h.get("header_contains")):
        return False
    ok = False
    if h.get("body_contains") and str(h["body_contains"]).lower() in resp.get("body", "").lower():
        ok = True
    for k, v in (h.get("header_contains") or {}).items():
        val = {a.lower(): b for a, b in resp.get("headers", {}).items()}.get(k.lower())
        if val is not None and str(v).lower() in val.lower():
            ok = True
    return ok


def _entry_for(sig: dict, key: str, listener: Listener | None) -> dict:
    chk = sig.get("check") or {}
    e: dict[str, Any] = {"key": key, "name": sig["name"] if key == sig["id"] else f"{sig['name']} (port {listener.port})" if listener else sig["name"]}
    typ = chk.get("type", "tcp")
    e["type"] = typ
    if typ == "process":
        e["process_name"] = chk.get("process_name") or (sig.get("processes") or [sig["id"]])[0]
    elif typ == "http":
        scheme = chk.get("scheme", "http")
        host = listener.host
        hostpart = f"[{host}]" if ":" in host else host
        e["url"] = f"{scheme}://{hostpart}:{listener.port}{chk.get('path', '/')}"
        e["expected_status"] = _status(chk.get("expected_status", "200-399"))
        if scheme == "https":
            e["verify_tls"] = bool(chk.get("verify_tls", False))      # local self-signed certificates are the norm
    else:
        e["host"], e["port"] = listener.host, listener.port
    return e


def _status(v):
    return int(v) if isinstance(v, str) and v.isdigit() else v


def _local_ports(services: list[dict], local_names: set[str]) -> set[int]:
    from urllib.parse import urlparse
    ports: set[int] = set()
    for s in services:
        t = s.get("type")
        if t == "http" and s.get("url"):
            u = urlparse(str(s["url"]))
            if (u.hostname or "") in local_names and u.scheme in ("http", "https"):
                ports.add(u.port or (443 if u.scheme == "https" else 80))
        elif t in ("tcp", "dns") and str(s.get("host", "127.0.0.1")) in local_names and s.get("port"):
            ports.add(int(s["port"]))
        elif t == "process":
            ports.update(int(p) for p in s.get("listen_tcp") or [])
    return ports


def detect(scanner: Scanner, sigs: list[dict], existing: list[dict], include_unidentified: bool = False) -> Result:
    res = Result()
    units = scanner.systemd_units()
    listeners = scanner.listeners()
    eph = scanner.ephemeral_min()
    local_names = set(LOCAL_HOSTS) | {"0.0.0.0", socket.gethostname()} | {l.host for l in listeners}
    ex_keys = {str(s.get("key")) for s in existing}
    ex_ports = _local_ports(existing, local_names)
    ex_procs = {str(s.get("process_name")) for s in existing if s.get("type") == "process"}
    by_port: dict[int, list[Listener]] = {}
    for l in listeners:
        by_port.setdefault(l.port, []).append(l)

    def pick(port: int) -> Listener:                      # one endpoint per port: prefer a non-loopback bind (dedupes 0.0.0.0 + ::)
        return sorted(by_port[port], key=lambda l: (l.loopback_only, l.addr))[0]

    claimed: set[int] = set()
    detected_keys: set[str] = set()

    def add(sig, key, entry, evidence, port=None):
        detected_keys.add(key)
        dup_port = port is not None and port in ex_ports
        dup_proc = entry["type"] == "process" and entry.get("process_name") in ex_procs
        if key in ex_keys or dup_port or dup_proc:
            why = "same key" if key in ex_keys else f"port {port} already checked" if dup_port else "process already checked"
            res.found.append(Found(entry, evidence, port, "monitored"))
            res.notes.append(f"{sig['name']}: already monitored ({why}); left as configured")
            if port is not None:
                claimed.add(port)
            return
        if any(f.entry["key"] == key or (port is not None and f.port == port) for f in res.found):
            return
        res.found.append(Found(entry, evidence, port, "new"))
        if port is not None:
            claimed.add(port)

    for sig in sigs:
        unit_hit = next(((u, d) for u, d in units
                         if any(re.search(rx, u) for rx in sig.get("units") or []) or (sig.get("description") and re.search(sig["description"], d))), None)
        chk = sig.get("check") or {}
        if chk.get("type") == "process":
            procs = set(sig.get("processes") or [chk.get("process_name")])
            owned = any(l.process in procs for l in listeners)
            if unit_hit or owned or _proc_running(scanner, procs):
                add(sig, sig["id"], _entry_for(sig, sig["id"], None), f"systemd unit {unit_hit[0]}" if unit_hit else "running process")
            continue
        procs = set(sig.get("processes") or [])
        known = list(sig.get("ports") or [])
        cand: dict[int, str] = {}                                         # port -> evidence
        for l in listeners:
            if l.process and l.process in procs:
                cand.setdefault(l.port, f"process {l.process} owns the port")
        if unit_hit:
            for p in known:
                if p in by_port:
                    cand.setdefault(p, f"systemd unit {unit_hit[0]} + known port {p}")
        probe = (sig.get("http") or {}).get("probe", True) and chk.get("type") == "http"
        h = sig.get("http") or {}
        if probe:
            for p in known:                                              # a known port may reveal itself over HTTP alone
                if p in by_port and p not in cand and not _claimed_elsewhere(p, claimed):
                    l = pick(p)
                    r = scanner.http_get(h.get("scheme", "http"), l.host, p, h.get("path", "/"))
                    if _http_matches(sig, r):
                        cand[p] = f"HTTP answer on port {p} matches {sig['name']}"
        if not cand:
            if unit_hit:
                res.skipped.append(f"{sig['name']}: unit {unit_hit[0]} is running but no listening port was found; not added")
            continue
        # validate / order candidates
        good: list[tuple[int, str]] = []
        for p, ev in cand.items():
            l = pick(p)
            if l.loopback_only and not sig.get("loopback_ok"):
                res.skipped.append(f"{sig['name']} on 127.0.0.1:{p}: loopback-only and not a user-facing service; not added")
                continue
            if probe and "HTTP answer" not in ev:
                r = scanner.http_get(h.get("scheme", "http"), l.host, p, h.get("path", "/"))
                if not _http_matches(sig, r) and p not in known:
                    res.skipped.append(f"{sig['name']}: port {p} is owned by its process but did not answer like {sig['name']}; not added")
                    continue
            good.append((p, ev))
        if not good:
            continue
        good.sort(key=lambda t: (known.index(t[0]) if t[0] in known else 10_000, t[0]))
        if not sig.get("multi"):
            good = good[:1]
        for p, ev in good:
            key = sig["id"] if (not sig.get("multi") or (known and p == known[0])) else f"{sig['id']}-{p}"
            add(sig, key, _entry_for(sig, key, pick(p)), ev, p)

    # --- everything else that listens ------------------------------------------------------------------------------
    probes = 0
    weak: dict[int, list[str]] = {}
    for sig in sigs:
        for p in sig.get("ports") or []:
            weak.setdefault(p, []).append(sig["name"])
    for port in sorted(by_port):
        if port in claimed or port in ex_ports:
            continue
        l = pick(port)
        if l.loopback_only:
            res.skipped.append(f"127.0.0.1:{port}{' (' + l.process + ')' if l.process else ''}: loopback-only internal endpoint; ignored")
            continue
        if port >= eph:
            res.skipped.append(f"port {port}: ephemeral range (>= {eph}); ignored")
            continue
        info = {"port": port, "process": l.process, "addr": l.addr, "hint": ", ".join(weak.get(port, [])), "http": None}
        if probes < MAX_UNIDENTIFIED_PROBES:
            probes += 1
            for scheme in ("http", "https"):
                r = scanner.http_get(scheme, l.host, port, "/", 2.0)
                if r:
                    info["http"] = scheme
                    break
        res.unidentified.append(info)
        if include_unidentified:
            key = f"tcp-{port}"
            e: dict[str, Any] = {"key": key, "name": f"Unidentified TCP :{port}" + (f" ({l.process})" if l.process else "")}
            if info["http"]:
                e.update(type="http", url=f"{info['http']}://{l.host}:{port}/", expected_status="100-499")
                if info["http"] == "https":
                    e["verify_tls"] = False
            else:
                e.update(type="tcp", host=l.host, port=port)
            detected_keys.add(key)
            if key not in ex_keys:
                res.found.append(Found(e, "listening port, application unidentified", port, "new"))

    # --- existing checks whose application is gone (report only) ----------------------------------------------------
    sig_ids = [s["id"] for s in sigs]
    for s in existing:
        k = str(s.get("key"))
        base = next((i for i in sig_ids if k == i or k.startswith(i + "-")), None)
        if base and k not in detected_keys and not any(f.status == "monitored" and f.entry["key"].startswith(base) for f in res.found):
            res.missing.append({"key": k, "name": s.get("name", k)})
    return res


def _claimed_elsewhere(port: int, claimed: set[int]) -> bool:
    return port in claimed


def _proc_running(scanner: Scanner, names: set[str]) -> bool:
    if isinstance(scanner, FixtureScanner):
        return any(n in set(scanner.fx.get("processes", [])) for n in names)
    try:
        for pid in filter(str.isdigit, os.listdir(scanner.proc)):
            try:
                if open(f"{scanner.proc}/{pid}/comm").read().strip() in names:
                    return True
            except OSError:
                continue
    except OSError:
        pass
    return False


# ------------------------------------------------------------------------------------------------------------ config edit
def _dump_item(entry: dict, indent: str) -> list[str]:
    lines = yaml.safe_dump(entry, sort_keys=False, default_flow_style=False, allow_unicode=True, width=1000).splitlines()
    return [indent + "- " + lines[0]] + [indent + "  " + ln for ln in lines[1:]]


def merge_into_text(text: str, entries: list[dict]) -> str:
    """Append `entries` to the services list inside a managed block. Everything else stays byte-identical."""
    if not entries:
        return text
    lines = text.split("\n")
    sv = next((i for i, ln in enumerate(lines) if re.match(r"^services\s*:", ln)), None)
    if sv is None:
        if lines and lines[-1] == "":
            lines.pop()
        lines += ["", "services:"]
        sv = len(lines) - 1
    else:
        m = re.match(r"^services\s*:\s*(\[\s*\])?\s*(#.*)?$", lines[sv])
        if not m:
            raise ConfigError("cannot edit `services:` automatically (unusual layout); add the checks by hand")
        if m.group(1):                                                    # services: []   ->   services:
            lines[sv] = "services:" + (f" {m.group(2)}" if m.group(2) else "")
    end = len(lines)
    for i in range(sv + 1, len(lines)):
        if re.match(r"^[A-Za-z_\"']", lines[i]):
            end = i
            break
    indent = "  "
    for i in range(sv + 1, end):
        m = re.match(r"^(\s*)- ", lines[i])
        if m:
            indent = m.group(1)
            break
    new_items: list[str] = []
    for e in entries:
        new_items += _dump_item(e, indent)
    b = next((i for i in range(sv + 1, end) if lines[i].strip() == END), None)
    if b is not None:
        lines[b:b] = new_items
    else:
        at = end
        while at > sv + 1 and (lines[at - 1].strip() == "" or re.match(r"^#", lines[at - 1])):
            at -= 1
        block = [indent + BEGIN] + new_items + [indent + END]
        lines[at:at] = block
    out = "\n".join(lines)
    return out if out.endswith("\n") else out + "\n"


def _validate_text(text: str, orig_raw: dict) -> dict:
    """YAML + full agent-config validation + 'nothing pre-existing changed'."""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"result is not valid YAML: {e}") from e
    fd, tmp = tempfile.mkstemp(prefix=".hcs-validate-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        load_config(tmp, {"HCLUSTER_TOKEN": "validate-only"})
    finally:
        os.unlink(tmp)
    old_services = orig_raw.get("services") or []
    new_services = raw.get("services") or []
    if new_services[:len(old_services)] != old_services:
        raise ConfigError("internal check failed: existing service entries would have changed")
    a, b = dict(orig_raw), dict(raw)
    a.pop("services", None), b.pop("services", None)
    if a != b:
        raise ConfigError("internal check failed: non-service settings would have changed")
    return raw


def _backup(path: str, keep: int = 5) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak, n = f"{path}.bak-{stamp}", 0
    while os.path.exists(bak):
        n += 1
        bak = f"{path}.bak-{stamp}-{n}"
    shutil.copy2(path, bak)
    d, base = os.path.dirname(path) or ".", os.path.basename(path)
    old = sorted(f for f in os.listdir(d) if f.startswith(base + ".bak-"))
    for f in old[:-keep]:
        try:
            os.unlink(os.path.join(d, f))
        except OSError:
            pass
    return bak


def _atomic_write(path: str, text: str) -> None:
    st = os.stat(path)
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".config.yaml.disc.", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, st.st_mode & 0o7777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except (PermissionError, OSError):
            pass
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def apply_changes(path: str, entries: list[dict], orig_text: str, orig_raw: dict, restart: Callable[[], bool] | None = None) -> tuple[str, bool]:
    """Returns (backup path, restarted). Raises ConfigError after restoring the original on any failure."""
    new_text = merge_into_text(orig_text, entries)
    _validate_text(new_text, orig_raw)                        # nothing is touched if this fails
    bak = _backup(path)
    try:
        _atomic_write(path, new_text)
        load_config(path, {"HCLUSTER_TOKEN": "validate-only"})
        restarted = False
        if restart is not None:
            restarted = True
            if not restart():
                raise ConfigError("the agent did not come back up with the new configuration")
        return bak, restarted
    except Exception as e:  # noqa: BLE001
        shutil.copy2(bak, path + ".restore")
        os.replace(path + ".restore", path)
        if restart is not None:
            restart()
        raise ConfigError(f"rolled back to the previous config.yaml ({e})") from e


# ------------------------------------------------------------------------------------------------------------------ CLI
def make_restarter(unit: str = "hcluster-agent.service") -> Callable[[], bool] | None:
    if os.environ.get("HCLUSTER_SKIP_SYSTEMD") == "1":
        return None
    sc = os.environ.get("HCLUSTER_SYSTEMCTL") or shutil.which("systemctl")
    if not sc:
        return None
    if subprocess.run([sc, "is-active", "--quiet", unit]).returncode != 0:
        return None                                           # not running: the next start simply reads the new file

    def restart() -> bool:
        if subprocess.run([sc, "restart", unit]).returncode != 0:
            return False
        for _ in range(int(os.environ.get("HCLUSTER_WAIT_SECONDS", "10"))):
            if subprocess.run([sc, "is-active", "--quiet", unit]).returncode == 0:
                return True
            time.sleep(1)
        return subprocess.run([sc, "is-active", "--quiet", unit]).returncode == 0
    return restart


def run(config_path: str, dry_run: bool = False, no_restart: bool = False, include_unidentified: bool = False,
        signatures: list[str] | None = None, as_json: bool = False, scanner: Scanner | None = None, out=None) -> int:
    out = out or sys.stdout
    try:
        with open(config_path, encoding="utf-8") as fh:
            orig_text = fh.read()
    except OSError as e:
        print(f"discover-services: cannot read {config_path}: {e}", file=sys.stderr)
        return 2
    try:
        orig_raw = yaml.safe_load(orig_text) or {}
        load_config(config_path, {"HCLUSTER_TOKEN": "validate-only"})          # refuse to touch a config that is already broken
        sigs = load_signatures([os.path.join(os.path.dirname(os.path.abspath(config_path)), "signatures.yaml")] + list(signatures or []))
    except (ConfigError, yaml.YAMLError) as e:
        print(f"discover-services: {e}\nnothing was changed", file=sys.stderr)
        return 2
    fx = os.environ.get("HCLUSTER_DISCOVERY_FIXTURE")
    scanner = scanner or (FixtureScanner(fx) if fx else Scanner())
    existing = list(orig_raw.get("services") or [])
    res = detect(scanner, sigs, existing, include_unidentified)
    new = [f for f in res.found if f.status == "new"]
    entries = [f.entry for f in new]
    rc = 0
    if entries and not dry_run:
        try:
            res.backup, res.restarted = apply_changes(config_path, entries, orig_text, orig_raw, None if no_restart else make_restarter())
            res.added = [e["key"] for e in entries]
        except PermissionError as e:
            print(f"discover-services: cannot write {config_path}: {e}\n(run with sudo)", file=sys.stderr)
            return 1
        except ConfigError as e:
            print(f"discover-services: {e}", file=sys.stderr)
            return 1
    _report(res, new, dry_run, as_json, out)
    return rc


def _report(res: Result, new: list[Found], dry_run: bool, as_json: bool, out) -> None:
    if as_json:
        json.dump({"dry_run": dry_run, "added": res.added, "would_add": [f.entry["key"] for f in new] if dry_run else [],
                   "already_monitored": [f.entry["key"] for f in res.found if f.status == "monitored"],
                   "not_detected": res.missing, "unidentified": res.unidentified, "skipped": res.skipped,
                   "backup": res.backup, "restarted": res.restarted}, out, indent=2)
        out.write("\n")
        return
    p = lambda s="": print(s, file=out)  # noqa: E731
    mon = [f for f in res.found if f.status == "monitored"]
    p(f"== service discovery{' (dry run: nothing written)' if dry_run else ''} ==")
    for f in new:
        p(f"  {'would add' if dry_run else 'added   '} {f.entry['key']:<16} {f.entry['type']:<7} {f.entry.get('url') or (str(f.entry.get('host', '')) + ':' + str(f.entry.get('port', ''))) if f.entry['type'] != 'process' else 'process ' + f.entry['process_name']}   [{f.evidence}]")
    for f in mon:
        p(f"  kept     {f.entry['key']:<16} already monitored; your existing check is untouched")
    if not new and not mon:
        p("  no known services detected")
    if res.missing:
        p("\nNo longer detected (NOT removed; delete the entry from config.yaml if it is gone for good):")
        for m in res.missing:
            p(f"  - {m['key']} ({m['name']})")
    if res.unidentified:
        p("\nUnidentified TCP endpoints (not added; no application name is guessed). Add one by hand, or rerun with --include-unidentified:")
        for u in res.unidentified:
            p(f"  - port {u['port']}" + (f" process {u['process']}" if u["process"] else "") + (f", answers {u['http'].upper()}" if u["http"] else "")
              + (f"; port is conventionally used by: {u['hint']}" if u["hint"] else ""))
    for s in res.skipped:
        p(f"  skipped: {s}")
    if res.added:
        p(f"\nconfig updated; previous version saved as {res.backup}")
        p("agent restarted with the new checks" if res.restarted else "restart the agent to start the new checks: sudo systemctl restart hcluster-agent")
    elif not dry_run:
        p("\nconfig unchanged")
