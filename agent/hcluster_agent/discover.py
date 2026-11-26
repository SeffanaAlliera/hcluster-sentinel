"""`hcluster-agent discover` — find out what is REALLY running on this host.

Nothing is configured from guesses: well-known upstream defaults are only *candidates*
and are reported as verified only when a probe succeeds.
"""
from __future__ import annotations

import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import ServiceCheck
from .checks import run_check
from .prometheus import _get_json

CANDIDATE_METRICS = {
    "node_cpu_seconds_total": "CPU (node_exporter)",
    "node_memory_MemAvailable_bytes": "Memory available (node_exporter)",
    "node_memory_MemTotal_bytes": "Memory total (node_exporter)",
    "node_filesystem_avail_bytes": "Disk available (node_exporter)",
    "node_filesystem_size_bytes": "Disk size (node_exporter)",
    "node_load1": "Load average (node_exporter)",
    "node_network_receive_bytes_total": "Network RX (node_exporter)",
    "node_network_transmit_bytes_total": "Network TX (node_exporter)",
    "node_hwmon_temp_celsius": "CPU/board temperatures (node_exporter hwmon)",
    "container_cpu_usage_seconds_total": "Container CPU (cAdvisor)",
    "container_memory_usage_bytes": "Container memory (cAdvisor)",
}

# Well-known upstream defaults. They are CANDIDATES to probe, not facts about this host.
SERVICE_CANDIDATES = [
    ("jellyfin", "Jellyfin", "http", ["http://127.0.0.1:8096/health"]),
    ("immich", "Immich", "http", ["http://127.0.0.1:2283/api/server/ping", "http://127.0.0.1:2283/"]),
    ("pihole", "Pi-hole", "http", ["http://127.0.0.1/admin/", "http://127.0.0.1:8080/admin/"]),
    ("syncthing", "Syncthing", "http", ["http://127.0.0.1:8384/rest/noauth/health", "http://127.0.0.1:8384/"]),
    ("samba", "Samba", "tcp", ["127.0.0.1:445"]),
    ("portainer", "Portainer", "http", ["http://127.0.0.1:9000/api/system/status", "https://127.0.0.1:9443/api/system/status"]),
    ("gptwol", "GPTWOL", "http", ["http://127.0.0.1:5000/"]),
    ("dnsmasq", "dnsmasq (DHCP)", "process", ["dnsmasq:udp67"]),
    ("dns", "Local DNS resolver", "dns", ["127.0.0.1:53"]),
]


def listening_ports(proc: str = "/proc") -> list[tuple[int, str, str]]:
    """(port, bind address, process) for TCP LISTEN sockets. Process names need root to resolve."""
    inode_proc: dict[str, str] = {}
    try:
        for pid in filter(str.isdigit, os.listdir(proc)):
            try:
                comm = open(f"{proc}/{pid}/comm").read().strip()
                for fd in os.listdir(f"{proc}/{pid}/fd"):
                    try:
                        link = os.readlink(f"{proc}/{pid}/fd/{fd}")
                    except OSError:
                        continue
                    m = re.fullmatch(r"socket:\[(\d+)\]", link)
                    if m:
                        inode_proc[m.group(1)] = comm
            except OSError:
                continue
    except OSError:
        pass
    out: list[tuple[int, str, str]] = []
    for fname, v6 in (("net/tcp", False), ("net/tcp6", True)):
        try:
            lines = open(f"{proc}/{fname}").read().splitlines()[1:]
        except OSError:
            continue
        for ln in lines:
            f = ln.split()
            if len(f) < 10 or f[3] != "0A":
                continue
            addr_hex, port_hex = f[1].rsplit(":", 1)
            if v6:
                addr = "::" if set(addr_hex) == {"0"} else ("::1" if addr_hex.endswith("01000000") and addr_hex.startswith("0" * 24) else "v6")
            else:
                b = bytes.fromhex(addr_hex)[::-1]
                addr = ".".join(str(x) for x in b)
            out.append((int(port_hex, 16), addr, inode_proc.get(f[9], "?")))
    return sorted(set(out))


def probe_service(key: str, name: str, typ: str, target: str) -> tuple[bool, str, dict]:
    if typ == "http":
        sc = ServiceCheck(key=key, name=name, type="http", url=target, timeout_seconds=4, retries=0, degraded_after_ms=None, verify_tls=False)
        sc.expected_status = [(200, 399)]
        snippet = {"key": key, "name": name, "type": "http", "url": target}
    elif typ == "process":
        pname, _, spec = target.partition(":")
        udp = [int(spec[3:])] if spec.startswith("udp") else []
        tcp = [int(spec[3:])] if spec.startswith("tcp") else []
        sc = ServiceCheck(key=key, name=name, type="process", process_name=pname, listen_udp=udp, listen_tcp=tcp, retries=0)
        snippet = {"key": key, "name": name, "type": "process", "process_name": pname, "listen_udp": udp, "listen_tcp": tcp}
    else:
        host, _, port = target.partition(":")
        sc = ServiceCheck(key=key, name=name, type=typ, host=host, port=int(port), timeout_seconds=4, retries=0, degraded_after_ms=None)
        snippet = {"key": key, "name": name, "type": typ, "host": host, "port": int(port)}
    r = run_check(sc)
    if r.status == "down":
        return False, r.error or "down", snippet
    return True, f"{r.response_ms} ms" + (f", HTTP {r.http_status}" if r.http_status else ""), snippet


def discover(prom_urls: list[str], proc: str = "/proc") -> int:
    print("== hCluster agent discovery ==\n")
    print("-- Prometheus --")
    found = None
    for base in prom_urls:
        try:
            body = _get_json(f"{base.rstrip('/')}/api/v1/targets?state=active", 4)
            if body.get("status") == "success":
                found = base.rstrip("/")
                break
        except Exception as e:  # noqa: BLE001
            print(f"  ✘ {base}: {e.__class__.__name__}: {getattr(e, 'reason', e)}")
    if not found:
        print("  No Prometheus API reachable from this host. Set prometheus.enabled: false, or pass --prometheus-url.")
    else:
        print(f"  ✔ Prometheus API answers at {found}   (use this as prometheus.base_url)")
        targets = _get_json(f"{found}/api/v1/targets?state=active", 4)["data"]["activeTargets"]
        print(f"  Targets ({len(targets)}):")
        for t in targets:
            lab = t.get("labels", {})
            print(f"    {'✔' if t.get('health') == 'up' else '✘'} job={lab.get('job')} instance={lab.get('instance')} health={t.get('health')}")
        try:
            names = set(_get_json(f"{found}/api/v1/label/__name__/values", 6).get("data", []))
        except Exception as e:  # noqa: BLE001
            names = set()
            print(f"  (could not list metric names: {e})")
        print("  Metrics the default queries depend on:")
        for m, desc in CANDIDATE_METRICS.items():
            print(f"    {'✔ present ' if m in names else '✘ missing '} {m:40s} {desc}")
        if "node_filesystem_size_bytes" in names:
            try:
                q = urllib.parse.urlencode({"query": "count by (mountpoint, fstype) (node_filesystem_size_bytes)"})
                res = _get_json(f"{found}/api/v1/query?{q}", 5)["data"]["result"]
                print("  Filesystems seen by node_exporter (set local_metrics/disk query mountpoint to the right one):")
                for r in res:
                    print(f"    mountpoint={r['metric'].get('mountpoint')} fstype={r['metric'].get('fstype')}")
            except Exception:  # noqa: BLE001
                pass
        if not (names & {"node_cpu_seconds_total"}):
            print("  → node_exporter metrics not found: either install node_exporter or rely on local /proc fallback metrics.")

    print("\n-- Drives that will be monitored (local /proc/mounts auto-detection) --")
    from .localmetrics import list_drives, list_sensors, read_cpu_temp, read_net_bytes
    for mnt in list_drives(proc):
        try:
            st = os.statvfs(mnt)
            print(f"    {mnt:30s} {st.f_blocks * st.f_frsize / 1e9:8.1f} GB total, {st.f_bavail * st.f_frsize / 1e9:8.1f} GB free")
        except OSError as e:
            print(f"    {mnt:30s} (cannot stat: {e})")
    t = read_cpu_temp()
    print(f"\n-- CPU temperature sensor: {'%.1f °C' % t if t is not None else 'none found in /sys (coretemp/k10temp/thermal_zone)'}")
    for s in list_sensors() or ["(the kernel exposes no temperature sensors; try `sudo modprobe coretemp`)"]:
        print(f"    {s}")
    from .gpu import diagnose
    print("\n-- GPU (utilisation / clock / VRAM) --")
    for ln in diagnose():
        print(f"    {ln}")
    print("\n-- Network interfaces (all non-virtual-prefix NICs are summed into ONE overall throughput metric) --")
    try:
        for ifc in sorted(read_net_bytes(proc, [])):
            virtual = os.path.exists(f"/sys/devices/virtual/net/{ifc}")
            try:
                state = open(f"/sys/class/net/{ifc}/operstate").read().strip()
            except OSError:
                state = "?"
            print(f"    {ifc:16s} {'virtual ' if virtual else 'PHYSICAL'} state={state}")
    except OSError:
        pass
    print("  (edit local_metrics.net_exclude_prefixes and the device!~ regex in the Prometheus queries to change what counts)")

    print("\n-- Listening TCP ports (run with sudo to see process names) --")
    for port, addr, comm in listening_ports(proc):
        print(f"    {addr:>15}:{port:<6} {comm}")

    print("\n-- Service candidates (upstream defaults; ✔ only if they answer here) --")
    snippets = []
    for key, name, typ, targets in SERVICE_CANDIDATES:
        hit = False
        for tgt in targets:
            ok, detail, snip = probe_service(key, name, typ, tgt)
            print(f"    {'✔' if ok else '✘'} {name:10s} {tgt:45s} {detail}")
            if ok and not hit:
                snippets.append(snip)
                hit = True
        if not hit:
            print(f"      → {name}: no default endpoint answered. Find its port in the list above and edit the config.")
    if snippets:
        print("\n-- Paste-ready `services:` entries for the endpoints that answered --")
        print("services:")
        for s in snippets:
            print(f"  - key: {s['key']}\n    name: {s['name']}\n    type: {s['type']}")
            if s["type"] == "http":
                print(f"    url: {s['url']}" + ("\n    verify_tls: false" if s["url"].startswith("https") else ""))
            elif s["type"] == "process":
                print(f"    process_name: {s['process_name']}" + (f"\n    listen_udp: {s['listen_udp']}" if s["listen_udp"] else "") + (f"\n    listen_tcp: {s['listen_tcp']}" if s["listen_tcp"] else ""))
            else:
                print(f"    host: {s['host']}\n    port: {s['port']}")
    return 0
