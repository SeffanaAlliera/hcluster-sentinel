"""Outbound HTTPS reporter with a bounded, persistent backlog and exponential backoff.

The agent only ever dials OUT to the Sentinel API. Redirects are refused so the
Authorization header can never be forwarded to another host.
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass

from .config import Config

log = logging.getLogger("hcluster.reporter")
BATCH = 20


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


@dataclass
class SendOutcome:
    ok: bool
    retry: bool = True          # False => permanent (drop the batch)
    status: int | None = None
    retry_after: float | None = None
    detail: str = ""


class Reporter:
    def __init__(self, cfg: Config, clock=time.monotonic):
        self.cfg = cfg
        self.clock = clock
        self.queue: deque[dict] = deque(maxlen=cfg.max_queue)
        self.failures = 0
        self.next_attempt = 0.0
        self.last_success: float | None = None
        self.dropped = 0
        self._path = os.path.join(cfg.state_dir, "queue.json")
        self._load()

    # ---- persistence (best effort) -------------------------------------
    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for item in json.load(fh)[-self.cfg.max_queue:]:
                    self.queue.append(item)
            if self.queue:
                log.info("restored %d queued report(s) from disk", len(self.queue))
        except (OSError, ValueError):
            pass

    def _persist(self) -> None:
        try:
            if not self.queue:
                if os.path.exists(self._path):
                    os.remove(self._path)
                return
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(list(self.queue), fh)
            os.replace(tmp, self._path)
        except OSError as e:
            log.debug("cannot persist queue: %s", e)

    # ---- HTTP ----------------------------------------------------------
    def _post(self, reports: list[dict]) -> SendOutcome:
        body = json.dumps({"schema": 1, "node_id": self.cfg.node_id, "reports": reports}, separators=(",", ":")).encode()
        req = urllib.request.Request(
            f"{self.cfg.api_url}/api/agent/report", data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.cfg.token}",
                     "User-Agent": "hcluster-agent/1"})
        try:
            with urllib.request.build_opener(_NoRedirect).open(req, timeout=self.cfg.request_timeout_seconds) as resp:
                resp.read(4096)
                return SendOutcome(True, status=resp.status)
        except urllib.error.HTTPError as e:
            ra = None
            try:
                ra = float(e.headers.get("Retry-After", ""))
            except (TypeError, ValueError):
                pass
            detail = ""
            try:
                detail = e.read(300).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            permanent = e.code in (400, 413, 422)
            return SendOutcome(False, retry=not permanent, status=e.code, retry_after=ra, detail=detail)
        except Exception as e:  # noqa: BLE001
            return SendOutcome(False, retry=True, detail=f"{e.__class__.__name__}: {getattr(e, 'reason', e)}")

    # ---- queue API -----------------------------------------------------
    def enqueue(self, report: dict) -> None:
        if len(self.queue) == self.queue.maxlen:
            self.dropped += 1                       # oldest is evicted by deque(maxlen)
        self.queue.append(report)
        self._persist()

    def backoff_seconds(self) -> float:
        base = min(300.0, 5.0 * (2 ** min(self.failures, 6)))
        return base * random.uniform(0.8, 1.2)

    def flush(self) -> int:
        """Sends as much of the queue as possible. Returns number of reports delivered."""
        sent = 0
        while self.queue and self.clock() >= self.next_attempt:
            batch = [self.queue[i] for i in range(min(BATCH, len(self.queue)))]
            out = self._post(batch)
            if out.ok:
                for _ in batch:
                    self.queue.popleft()
                sent += len(batch)
                if self.failures:
                    log.info("connectivity restored; delivered backlog")
                self.failures = 0
                self.last_success = self.clock()
                self.next_attempt = 0.0
                continue
            if not out.retry:
                log.error("server rejected batch permanently (HTTP %s): %s. dropping %d report(s)", out.status, out.detail, len(batch))
                for _ in batch:
                    self.queue.popleft()
                continue
            self.failures += 1
            wait = out.retry_after if out.retry_after else self.backoff_seconds()
            if out.status in (401, 403):
                wait = max(wait, 60.0)
                log.error("authentication refused (HTTP %s). check token / node enabled state. retrying in %.0fs", out.status, wait)
            else:
                log.warning("send failed (%s %s); %d queued; retry in %.0fs", out.status or "-", out.detail[:120], len(self.queue), wait)
            self.next_attempt = self.clock() + wait
            break
        self._persist()
        return sent
