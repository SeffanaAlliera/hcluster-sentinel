"""Main scheduling loop."""
from __future__ import annotations

import logging
import os
import signal
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import __version__
from .checks import run_check
from .config import Config
from .localmetrics import DEFAULT_DISK_EXCLUDE, LocalCollector, host_info
from .prometheus import collect as prom_collect
from .reporter import Reporter

log = logging.getLogger("hcluster.agent")


def sd_notify(msg: str) -> None:
    """Minimal systemd notify (READY/WATCHDOG) with no dependencies."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except OSError:
        pass


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _entry(name: str, label: str, value: float, source: str) -> dict:
    e = {"name": name, "value": value, "source": source}
    if label:
        e["label"] = label
    return e


class Agent:
    def __init__(self, cfg: Config, reporter: Reporter | None = None, now=time.monotonic):
        self.cfg = cfg
        self.reporter = reporter or Reporter(cfg)
        self.now = now
        self.local = LocalCollector(cfg.disk_mountpoints or None, cfg.net_exclude_prefixes, disk_exclude=cfg.disk_exclude_prefixes,
                                   net_per_interface=cfg.net_per_interface)
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="check")
        t = self.now()
        self.next_hb = t
        self.next_metrics = t
        self.next_service = {s.key: t for s in cfg.services if s.enabled}
        self.stop = False

    # ---- collection ----------------------------------------------------
    def collect_services(self, due) -> list[dict]:
        futs = [(s, self.pool.submit(run_check, s)) for s in due]
        out = []
        for s, f in futs:
            checked_at = iso_now()
            try:
                r = f.result(timeout=s.timeout_seconds * (s.retries + 1) + 10)
                status, ms, code, err = r.status, r.response_ms, r.http_status, r.error
            except Exception as e:  # noqa: BLE001
                status, ms, code, err = "down", None, None, f"check crashed: {e.__class__.__name__}"
            out.append({"key": s.key, "name": s.name, "type": s.type,
                        "target": s.target() if s.report_target else None,
                        "status": status, "response_ms": ms, "http_status": code,
                        "error": err, "checked_at": checked_at})
        return out

    def collect_metrics(self) -> dict:
        cfg = self.cfg
        local = self.local.collect() if (cfg.local_fallback or not cfg.prometheus.enabled) else {}
        values: list[dict] = []
        if cfg.prometheus.enabled:
            pr = prom_collect(cfg.prometheus)
            excl = DEFAULT_DISK_EXCLUDE if cfg.disk_exclude_prefixes is None else cfg.disk_exclude_prefixes
            pinned = set(cfg.disk_mountpoints)
            skipped = lambda m: bool(pinned and m not in pinned) or (not pinned and any(  # noqa: E731
                m == p or m.startswith(p.rstrip("/") + "/") for p in excl))
            for (name, label), v in pr.values.items():
                if name.startswith("disk_") and label and skipped(label):
                    continue                                   # e.g. /boot/efi: not a data drive
                if name.startswith("net_") and label and not cfg.net_per_interface:
                    continue                                   # one overall network metric only
                values.append(_entry(name, label, v, "prometheus"))
            unavailable = list(pr.unavailable)
            if cfg.local_fallback:                             # drives Prometheus can't see (e.g. node_exporter in Docker)
                have = {(e["name"], e.get("label", "")) for e in values}
                for (name, label), v in local.items():
                    if name.startswith("disk_") and label and (name, label) not in have:
                        values.append(_entry(name, label, v, "local"))
            if cfg.local_fallback:
                # Fill from the kernel every metric Prometheus did not deliver: queries that returned nothing AND
                # metrics the config has no query for at all (e.g. a config written before cpu_temp existed).
                have_names = {e["name"] for e in values}
                for name in sorted({k[0] for k in local} - have_names):
                    values.extend(_entry(k[0], k[1], v, "local") for k, v in local.items() if k[0] == name)
                    if name in unavailable:
                        unavailable.remove(name)
            if "cpu_temp_celsius" not in {e["name"] for e in values} and "cpu_temp_celsius" not in unavailable:
                unavailable.append("cpu_temp_celsius")        # nothing could read it: say so instead of staying silent
            for name, err in pr.errors.items():
                log.debug("query %s failed: %s", name, err)
            return {"status": pr.metrics_status,
                    "prometheus": {"status": pr.prom_status, "error": pr.prom_error,
                                   "targets_up": pr.targets_up, "targets_total": pr.targets_total,
                                   "down_targets": pr.down_targets},
                    "values": values, "unavailable": unavailable}
        if cfg.local_fallback:
            for (name, label), v in local.items():
                values.append(_entry(name, label, v, "local"))
            core = {"mem_percent", "disk_percent"}
            status = "ok" if core & {k[0] for k in local} else "failed"
            miss = ["cpu_temp_celsius"] if ("cpu_temp_celsius", "") not in local else []
            return {"status": status, "prometheus": {"status": "disabled"}, "values": values, "unavailable": miss}
        return {"status": "disabled", "prometheus": {"status": "disabled"}, "values": [], "unavailable": []}

    # ---- one scheduling step ------------------------------------------
    def step(self, force: bool = False) -> dict | None:
        """Builds a report if anything is due; enqueues and returns it."""
        t = self.now()
        due = [s for s in self.cfg.services if s.enabled and (force or self.next_service[s.key] <= t)]
        metrics_due = force or t >= self.next_metrics
        hb_due = force or t >= self.next_hb
        if not (due or metrics_due or hb_due):
            return None
        report: dict = {"collected_at": iso_now(), "agent": {"version": __version__}, "host": host_info()}
        if due:
            report["services"] = self.collect_services(due)
            t2 = self.now()
            for s in due:
                self.next_service[s.key] = t2 + s.interval_seconds
        if metrics_due:
            report["metrics_report"] = self.collect_metrics()
            self.next_metrics = self.now() + self.cfg.metrics_seconds
        self.next_hb = self.now() + self.cfg.heartbeat_seconds     # every report is also a heartbeat
        self.reporter.enqueue(report)
        return report

    def run(self) -> None:
        def _stop(*_):
            self.stop = True
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        from .localmetrics import list_sensors, read_cpu_temp
        t0 = read_cpu_temp()
        if t0 is None:
            log.warning("no CPU temperature sensor found; kernel exposes: %s", "; ".join(list_sensors()) or "nothing (try: sudo modprobe coretemp)")
        else:
            log.info("CPU temperature sensor OK (%.1f C)", t0)
        log.info("hcluster-agent %s starting for node %s -> %s", __version__, self.cfg.node_id, self.cfg.api_url)
        sd_notify("READY=1")
        while not self.stop:
            try:
                self.step()
                self.reporter.flush()
            except Exception:  # noqa: BLE001  never die on a bad cycle; systemd would restart but we can do better
                log.exception("cycle failed")
            sd_notify("WATCHDOG=1")
            for _ in range(10):                       # 1s granularity, interruptible
                if self.stop:
                    break
                time.sleep(0.1)
        log.info("stopping")
        sd_notify("STOPPING=1")
        self.pool.shutdown(wait=False, cancel_futures=True)
