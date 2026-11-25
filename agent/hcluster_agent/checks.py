"""Service availability checks (HTTP / TCP). No credentials, no side effects."""
from __future__ import annotations

import socket
import ssl
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


def run_check(sc: ServiceCheck, sleep=time.sleep) -> CheckResult:
    """Runs the check, retrying `retries` times on failure to absorb transient blips."""
    fn = _http_once if sc.type == "http" else _tcp_once
    res = fn(sc)
    attempts = 0
    while res.status == "down" and attempts < sc.retries:
        attempts += 1
        sleep(min(2.0, sc.timeout_seconds))
        res = fn(sc)
    return res
