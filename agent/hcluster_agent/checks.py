"""Service availability checks (HTTP / TCP). No credentials, no side effects."""
from __future__ import annotations

import os
import random
import socket
import ssl
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import ServiceCheck

MAX_BODY = 65536


@dataclass
class CheckResult:
    status: str                 # operational | degraded | down
    response_ms: int | None = None
    http_status: int | None = None
    error: str | None = None


def _short(e: BaseException) -> str:
    msg = str(getattr(e, "reason", None) or e) or e.__class__.__name__
    return f"{e.__class__.__name__}: {msg}"[:300]


def _status_ok(code: int, ranges: list[tuple[int, int]]) -> bool:
    return any(lo <= code <= hi for lo, hi in ranges)


def _http_once(sc: ServiceCheck) -> CheckResult:
    ctx = ssl.create_default_context()
    if not sc.verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    # follow redirects (e.g. Pi-hole /admin); no auth headers are ever sent here
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(sc.url, headers={"User-Agent": "hcluster-agent/1", "Accept": "*/*"})
    start = time.monotonic()
    code: int
    body = b""
    try:
        with opener.open(req, timeout=sc.timeout_seconds) as resp:
            code = resp.status
            if sc.expect_body_contains:
                body = resp.read(MAX_BODY)
            else:
                resp.read(1024)
    except urllib.error.HTTPError as e:          # non-2xx still means the server answered
        code = e.code
        if sc.expect_body_contains:
            try:
                body = e.read(MAX_BODY)
            except Exception:  # noqa: BLE001
                body = b""
    except Exception as e:  # noqa: BLE001  (timeouts, refused, DNS, TLS, ...)
        return CheckResult("down", None, None, _short(e))
    ms = int((time.monotonic() - start) * 1000)
    if not _status_ok(code, sc.expected_status):
        return CheckResult("down", ms, code, f"unexpected HTTP status {code}")
    if sc.expect_body_contains and sc.expect_body_contains.encode() not in body:
        return CheckResult("down", ms, code, "expected text not found in response body")
    if sc.degraded_after_ms is not None and ms > sc.degraded_after_ms:
        return CheckResult("degraded", ms, code, f"slow response ({ms} ms)")
    return CheckResult("operational", ms, code, None)


def _tcp_once(sc: ServiceCheck) -> CheckResult:
    start = time.monotonic()
    try:
        with socket.create_connection((sc.host, sc.port), timeout=sc.timeout_seconds):
            pass
    except Exception as e:  # noqa: BLE001
        return CheckResult("down", None, None, _short(e))
    ms = int((time.monotonic() - start) * 1000)
    if sc.degraded_after_ms is not None and ms > sc.degraded_after_ms:
        return CheckResult("degraded", ms, None, f"slow connect ({ms} ms)")
    return CheckResult("operational", ms, None, None)


def build_dns_query(name: str, qid: int) -> bytes:
    labels = b"".join(bytes([len(p)]) + p.encode("idna") for p in name.strip(".").split(".") if p)
    return struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + labels + b"\x00" + struct.pack(">HH", 1, 1)   # A / IN


def _dns_once(sc: ServiceCheck) -> CheckResult:
    """Sends one UDP DNS query. ANY well-formed answer (NOERROR or NXDOMAIN) means the resolver is alive."""
    qid = random.randrange(65536)
    start = time.monotonic()
    try:
        fam, typ, proto, _, addr = socket.getaddrinfo(sc.host, sc.port, type=socket.SOCK_DGRAM)[0]
        with socket.socket(fam, typ, proto) as s:
            s.settimeout(sc.timeout_seconds)
            s.sendto(build_dns_query(sc.query_name, qid), addr)
            data, _ = s.recvfrom(4096)
    except Exception as e:  # noqa: BLE001
        return CheckResult("down", None, None, _short(e))
    ms = int((time.monotonic() - start) * 1000)
    if len(data) < 12 or struct.unpack(">H", data[:2])[0] != qid or not data[2] & 0x80:
        return CheckResult("down", ms, None, "invalid DNS response")
    rcode = data[3] & 0x0F
    if rcode not in (0, 3):
        return CheckResult("down", ms, None, f"DNS error rcode {rcode} ({'SERVFAIL' if rcode == 2 else 'REFUSED' if rcode == 5 else 'error'})")
    if sc.degraded_after_ms is not None and ms > sc.degraded_after_ms:
        return CheckResult("degraded", ms, None, f"slow DNS answer ({ms} ms)")
    return CheckResult("operational", ms, None, None)


def _bound_ports(proc: str, files: tuple[str, ...], listen_only: bool) -> set[int]:
    ports: set[int] = set()
    for f in files:
        try:
            lines = open(f"{proc}/net/{f}", encoding="utf-8").read().splitlines()[1:]
        except OSError:
            continue
        for ln in lines:
            cols = ln.split()
            if len(cols) > 3 and (not listen_only or cols[3] == "0A"):
                ports.add(int(cols[1].rsplit(":", 1)[1], 16))
    return ports


def _process_once(sc: ServiceCheck, proc: str = "/proc") -> CheckResult:
    """Is the process running, and are the expected ports bound? Read-only /proc inspection; sends no packets."""
    want = (sc.process_name or "")[:15]                        # /proc/<pid>/comm is truncated to 15 chars
    found = False
    try:
        for pid in os.listdir(proc):
            if pid.isdigit():
                try:
                    if open(f"{proc}/{pid}/comm", encoding="utf-8").read().strip() == want:
                        found = True
                        break
                except OSError:
                    continue
    except OSError as e:
        return CheckResult("down", None, None, _short(e))
    if not found:
        return CheckResult("down", None, None, f"process '{sc.process_name}' is not running")
    missing = [f"udp/{p}" for p in sc.listen_udp if p not in _bound_ports(proc, ("udp", "udp6"), False)]
    missing += [f"tcp/{p}" for p in sc.listen_tcp if p not in _bound_ports(proc, ("tcp", "tcp6"), True)]
    if missing:
        return CheckResult("down", None, None, f"'{sc.process_name}' running but not listening on {', '.join(missing)}")
    return CheckResult("operational", None, None, None)


def run_check(sc: ServiceCheck, sleep=time.sleep) -> CheckResult:
    """Runs the check, retrying `retries` times on failure to absorb transient blips."""
    fn = {"http": _http_once, "tcp": _tcp_once, "dns": _dns_once, "process": _process_once}[sc.type]
    res = fn(sc)
    attempts = 0
    while res.status == "down" and attempts < sc.retries:
        attempts += 1
        sleep(min(2.0, sc.timeout_seconds))
        res = fn(sc)
    return res
