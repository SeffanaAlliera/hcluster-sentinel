"""/proc and /sys based metrics: CPU, CPU temperature, memory, every real drive, load, per-NIC + total network.

Used when Prometheus is disabled/unreachable/missing a metric. Values come straight from the kernel;
if a source can't be read the metric is simply absent (never estimated).
Keys are (metric_name, label): label '' = node-wide, otherwise a mountpoint or interface name.
"""
from __future__ import annotations

import glob
import os
import platform
import time
from typing import Callable

Key = tuple[str, str]

REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "ntfs", "ntfs3", "fuseblk", "vfat", "exfat", "f2fs", "jfs", "reiserfs"}
DEFAULT_DISK_EXCLUDE = ["/boot", "/var/lib/docker", "/var/lib/containers", "/var/snap", "/snap", "/run", "/sys", "/proc", "/dev"]
CPU_HWMON_CHIPS = {"coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "soc_thermal", "cpu", "x86_pkg_temp"}
CPU_THERMAL_TYPES = {"x86_pkg_temp", "cpu-thermal", "cpu_thermal", "soc_thermal"}
FALLBACK_THERMAL_TYPES = {"acpitz"}      # board/ACPI zone: used only when no CPU-specific sensor exists


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def read_cpu_times(proc: str = "/proc") -> tuple[int, int]:
    parts = _read(f"{proc}/stat").splitlines()[0].split()
    vals = [int(x) for x in parts[1:9]]
    return vals[3] + vals[4], sum(vals)          # (idle+iowait, total)


def read_meminfo(proc: str = "/proc") -> dict[str, int]:
    out: dict[str, int] = {}
    for line in _read(f"{proc}/meminfo").splitlines():
        k, _, rest = line.partition(":")
        num = rest.strip().split()
        if num and num[0].isdigit():
            out[k] = int(num[0]) * 1024
    return out


def read_net_bytes(proc: str, exclude: list[str]) -> dict[str, tuple[int, int]]:
    """{interface: (rx_bytes, tx_bytes)} for interfaces not matching an excluded prefix."""
    out: dict[str, tuple[int, int]] = {}
    for line in _read(f"{proc}/net/dev").splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        if not rest or any(name.startswith(p) for p in exclude):
            continue
        f = rest.split()
        out[name] = (int(f[0]), int(f[8]))
    return out


def _unescape_mount(s: str) -> str:
    return s.replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")


def list_drives(proc: str = "/proc", only: list[str] | None = None, exclude_prefixes: list[str] | None = None) -> list[str]:
    """Mountpoints of real, distinct drives. Bind mounts / btrfs subvolumes of one device count once."""
    excl = DEFAULT_DISK_EXCLUDE if exclude_prefixes is None else exclude_prefixes
    if only:
        return list(only)
    seen: dict[str, str] = {}
    try:
        lines = _read(f"{proc}/mounts").splitlines()
    except OSError:
        return []
    for ln in lines:
        f = ln.split()
        if len(f) < 3:
            continue
        dev, mnt, fs = f[0], _unescape_mount(f[1]), f[2]
        if fs not in REAL_FS or not (dev.startswith("/") or fs in ("zfs", "fuseblk")):
            continue
        if any(mnt == p or mnt.startswith(p.rstrip("/") + "/") for p in excl):
            continue
        if dev not in seen or len(mnt) < len(seen[dev]):
            seen[dev] = mnt
    return sorted(seen.values())


def read_cpu_temp(sys_root: str = "/sys") -> float | None:
    """CPU package temperature in °C from hwmon (coretemp/k10temp/...) or thermal zones; None if unavailable."""
    def sane(v: float) -> float | None:
        return round(v, 1) if 0 < v < 150 else None
    for hw in sorted(glob.glob(f"{sys_root}/class/hwmon/hwmon*")):
        try:
            chip = _read(f"{hw}/name").strip()
        except OSError:
            continue
        if chip not in CPU_HWMON_CHIPS:
            continue
        temps: list[tuple[str, float]] = []
        for f in sorted(glob.glob(f"{hw}/temp*_input")):
            try:
                raw = int(_read(f).strip()) / 1000.0
                try:
                    lab = _read(f.replace("_input", "_label")).strip()
                except OSError:
                    lab = ""
                temps.append((lab, raw))
            except (OSError, ValueError):
                continue
        if not temps:
            continue
        for lab, v in temps:                      # prefer the package/die sensor
            if lab.startswith(("Package", "Tctl", "Tdie")):
                return sane(v)
        return sane(max(v for _, v in temps))
    for z in sorted(glob.glob(f"{sys_root}/class/thermal/thermal_zone*")):
        try:
            if _read(f"{z}/type").strip() in CPU_THERMAL_TYPES:
                return sane(int(_read(f"{z}/temp").strip()) / 1000.0)
        except (OSError, ValueError):
            continue
    for z in sorted(glob.glob(f"{sys_root}/class/thermal/thermal_zone*")):      # last resort: ACPI zone
        try:
            if _read(f"{z}/type").strip() in FALLBACK_THERMAL_TYPES:
                v = sane(int(_read(f"{z}/temp").strip()) / 1000.0)
                if v is not None:
                    return v
        except (OSError, ValueError):
            continue
    return None


def list_sensors(sys_root: str = "/sys") -> list[str]:
    """Every temperature sensor the kernel exposes, for `discover` troubleshooting."""
    out: list[str] = []
    for hw in sorted(glob.glob(f"{sys_root}/class/hwmon/hwmon*")):
        try:
            chip = _read(f"{hw}/name").strip()
        except OSError:
            chip = "?"
        for f in sorted(glob.glob(f"{hw}/temp*_input")):
            try:
                lab = ""
                try:
                    lab = _read(f.replace("_input", "_label")).strip()
                except OSError:
                    pass
                out.append(f"hwmon {chip}/{lab or f.rsplit('/', 1)[1]} = {int(_read(f).strip()) / 1000.0:.1f} °C")
            except (OSError, ValueError):
                continue
    for z in sorted(glob.glob(f"{sys_root}/class/thermal/thermal_zone*")):
        try:
            out.append(f"thermal_zone type={_read(f'{z}/type').strip()} = {int(_read(f'{z}/temp').strip()) / 1000.0:.1f} °C")
        except (OSError, ValueError):
            continue
    return out


class LocalCollector:
    def __init__(self, mountpoints: list[str] | None = None, exclude: list[str] | None = None, proc: str = "/proc",
                 clock: Callable[[], float] = time.monotonic, disk_exclude: list[str] | None = None, sys_root: str = "/sys",
                 net_per_interface: bool = False):
        self.mountpoints = mountpoints            # None/[] = auto-detect every real drive
        self.exclude = exclude or []
        self.disk_exclude = disk_exclude
        self.proc = proc
        self.sys_root = sys_root
        self.net_per_interface = net_per_interface
        self.clock = clock
        self._cpu: tuple[int, int] | None = None
        self._net: tuple[dict[str, tuple[int, int]], float] | None = None

    def collect(self) -> dict[Key, float]:
        out: dict[Key, float] = {}
        try:
            idle, total = read_cpu_times(self.proc)
            if self._cpu is not None and total > self._cpu[1]:
                d_total, d_idle = total - self._cpu[1], idle - self._cpu[0]
                out[("cpu_percent", "")] = round(max(0.0, min(100.0, 100.0 * (1 - d_idle / d_total))), 2)
            self._cpu = (idle, total)
        except (OSError, ValueError, IndexError):
            pass
        temp = read_cpu_temp(self.sys_root)
        if temp is not None:
            out[("cpu_temp_celsius", "")] = temp
        try:
            mi = read_meminfo(self.proc)
            total, avail = mi["MemTotal"], mi["MemAvailable"]
            out[("mem_total_bytes", "")] = float(total)
            out[("mem_available_bytes", "")] = float(avail)
            out[("mem_percent", "")] = round(100.0 * (total - avail) / total, 2)
        except (OSError, KeyError, ZeroDivisionError, ValueError):
            pass
        for mnt in list_drives(self.proc, self.mountpoints, self.disk_exclude):
            try:
                st = os.statvfs(mnt)
            except OSError:
                continue
            total = st.f_blocks * st.f_frsize
            avail = st.f_bavail * st.f_frsize
            used = (st.f_blocks - st.f_bfree) * st.f_frsize
            if total > 0 and used + avail > 0:
                out[("disk_total_bytes", mnt)] = float(total)
                out[("disk_available_bytes", mnt)] = float(avail)
                out[("disk_percent", mnt)] = round(100.0 * used / (used + avail), 2)
        try:
            l1, l5, _ = _read(f"{self.proc}/loadavg").split()[:3]
            out[("load1", "")], out[("load5", "")] = float(l1), float(l5)
        except (OSError, ValueError):
            pass
        try:
            cur = read_net_bytes(self.proc, self.exclude)
            now = self.clock()
            if self._net is not None and now > self._net[1]:
                prev, t0 = self._net
                dt = now - t0
                tot_rx = tot_tx = 0.0
                for ifc, (rx, tx) in cur.items():
                    if ifc in prev and rx >= prev[ifc][0] and tx >= prev[ifc][1]:
                        r, t = (rx - prev[ifc][0]) / dt, (tx - prev[ifc][1]) / dt
                        if self.net_per_interface:
                            out[("net_rx_bytes_per_sec", ifc)] = round(r, 2)
                            out[("net_tx_bytes_per_sec", ifc)] = round(t, 2)
                        tot_rx += r
                        tot_tx += t
                if any(ifc in prev for ifc in cur):
                    out[("net_rx_bytes_per_sec", "")] = round(tot_rx, 2)       # all selected NICs combined
                    out[("net_tx_bytes_per_sec", "")] = round(tot_tx, 2)
            self._net = (cur, now)
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
