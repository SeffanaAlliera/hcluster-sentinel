"""Minimal /proc-based fallback metrics (CPU, memory, disk, load, network, uptime).

Used only when Prometheus is disabled/unreachable/missing a metric, so node cards still work.
Metrics come from the kernel, never estimated: if a source can't be read the metric is absent.
"""
from __future__ import annotations

import os
import platform
import time
from typing import Callable


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def read_cpu_times(proc: str = "/proc") -> tuple[int, int]:
    """(idle_all, total) jiffies from the aggregate cpu line."""
    parts = _read(f"{proc}/stat").splitlines()[0].split()
    vals = [int(x) for x in parts[1:9]]
    idle = vals[3] + vals[4]                  # idle + iowait
    return idle, sum(vals)


def read_meminfo(proc: str = "/proc") -> dict[str, int]:
    out: dict[str, int] = {}
    for line in _read(f"{proc}/meminfo").splitlines():
        k, _, rest = line.partition(":")
        num = rest.strip().split()
        if num and num[0].isdigit():
            out[k] = int(num[0]) * 1024
    return out


def read_net_bytes(proc: str, exclude: list[str]) -> tuple[int, int]:
    rx = tx = 0
    for line in _read(f"{proc}/net/dev").splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        if not rest or any(name.startswith(p) for p in exclude):
            continue
        f = rest.split()
        rx += int(f[0])
        tx += int(f[8])
    return rx, tx


class LocalCollector:
    def __init__(self, mountpoint: str = "/", exclude: list[str] | None = None, proc: str = "/proc",
                 clock: Callable[[], float] = time.monotonic):
        self.mountpoint = mountpoint
        self.exclude = exclude or []
        self.proc = proc
        self.clock = clock
        self._cpu: tuple[int, int] | None = None
        self._net: tuple[int, int, float] | None = None

    def collect(self) -> dict[str, float]:
        out: dict[str, float] = {}
        try:
            idle, total = read_cpu_times(self.proc)
            if self._cpu is not None and total > self._cpu[1]:
                d_total, d_idle = total - self._cpu[1], idle - self._cpu[0]
                out["cpu_percent"] = round(max(0.0, min(100.0, 100.0 * (1 - d_idle / d_total))), 2)
            self._cpu = (idle, total)
        except (OSError, ValueError, IndexError):
            pass
        try:
            mi = read_meminfo(self.proc)
            total, avail = mi["MemTotal"], mi["MemAvailable"]
            out["mem_total_bytes"] = float(total)
            out["mem_available_bytes"] = float(avail)
            out["mem_percent"] = round(100.0 * (total - avail) / total, 2)
        except (OSError, KeyError, ZeroDivisionError, ValueError):
            pass
        try:
            st = os.statvfs(self.mountpoint)
            total = st.f_blocks * st.f_frsize
            avail = st.f_bavail * st.f_frsize
            used = (st.f_blocks - st.f_bfree) * st.f_frsize
            if total > 0:
                out["disk_total_bytes"] = float(total)
                out["disk_available_bytes"] = float(avail)
                out["disk_percent"] = round(100.0 * used / (used + avail), 2) if used + avail else 0.0
        except OSError:
            pass
        try:
            l1, l5, _ = _read(f"{self.proc}/loadavg").split()[:3]
            out["load1"], out["load5"] = float(l1), float(l5)
        except (OSError, ValueError):
            pass
        try:
            rx, tx = read_net_bytes(self.proc, self.exclude)
            now = self.clock()
            if self._net is not None and now > self._net[2] and rx >= self._net[0] and tx >= self._net[1]:
                dt = now - self._net[2]
                out["net_rx_bytes_per_sec"] = round((rx - self._net[0]) / dt, 2)
                out["net_tx_bytes_per_sec"] = round((tx - self._net[1]) / dt, 2)
            self._net = (rx, tx, now)
        except (OSError, ValueError, IndexError):
            pass
        return out


def host_info(proc: str = "/proc") -> dict:
    info: dict = {"hostname": platform.node()[:253], "arch": platform.machine(), "kernel": platform.release()}
    try:
        for line in _read("/etc/os-release").splitlines():
            if line.startswith("PRETTY_NAME="):
                info["os"] = line.split("=", 1)[1].strip().strip('"')
                break
    except OSError:
        info["os"] = platform.system()
    try:
        info["uptime_seconds"] = int(float(_read(f"{proc}/uptime").split()[0]))
    except (OSError, ValueError, IndexError):
        pass
    return info
