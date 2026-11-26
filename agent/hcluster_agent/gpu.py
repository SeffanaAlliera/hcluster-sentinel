"""GPU metrics (utilisation, VRAM, clock), auto-detected per vendor. Nothing is reported for hardware that isn't there.

  NVIDIA : `nvidia-smi` query (needs the binary; under the stock systemd unit /dev/nvidia* is hidden, see docs)
  AMD    : amdgpu sysfs files (gpu_busy_percent, mem_info_vram_*, pp_dpm_sclk), no privileges needed
  Intel  : clock from sysfs (no privileges); utilisation from the i915 PMU via perf_event_open, which needs
           CAP_PERFMON (opt-in systemd drop-in). Integrated GPUs have no dedicated VRAM, so none is reported.

Metrics (label = GPU): gpu_utilization_percent, gpu_clock_mhz, gpu_vram_used_bytes, gpu_vram_total_bytes, gpu_vram_percent.
Anything that can't be read is simply absent; an unreadable utilisation is listed in `unavailable`.
"""
from __future__ import annotations

import csv
import ctypes
import glob
import io
import os
import platform
import re
import shutil
import subprocess
import time
from typing import Callable

Key = tuple[str, str]
GPU_DRIVERS = {"i915", "xe", "amdgpu", "radeon", "nouveau", "nvidia"}
_PERF_SYSCALL = {"x86_64": 298, "aarch64": 241}


def _read(p: str) -> str:
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _num(p: str) -> float | None:
    try:
        return float(_read(p).strip())
    except (OSError, ValueError):
        return None


def list_cards(sys_root: str = "/sys") -> list[dict]:
    """Real GPUs from /sys/class/drm (virtual/display-only devices are skipped by driver name)."""
    out = []
    for p in sorted(glob.glob(f"{sys_root}/class/drm/card[0-9]*")):
        name = os.path.basename(p)
        if "-" in name:                                   # card1-HDMI-A-1 etc. are connectors
            continue
        dev = f"{p}/device"
        drv = os.path.basename(os.path.realpath(f"{dev}/driver"))
        if drv in GPU_DRIVERS:
            out.append({"card": name, "driver": drv, "path": p, "dev": dev, "pci": os.path.basename(os.path.realpath(dev))})
    return out


# ---- Intel i915 PMU (engine busy time) via perf_event_open -------------------------------------
class _PerfAttr(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("config", ctypes.c_uint64),
                ("sample_period", ctypes.c_uint64), ("sample_type", ctypes.c_uint64), ("read_format", ctypes.c_uint64),
                ("flags", ctypes.c_uint64), ("_rest", ctypes.c_uint8 * 72)]


def perf_open(pmu_type: int, config: int) -> int:
    nr = _PERF_SYSCALL.get(platform.machine())
    if nr is None:
        raise OSError(38, f"perf_event_open not wired for {platform.machine()}")
    attr = _PerfAttr()
    attr.type, attr.size, attr.config = pmu_type, ctypes.sizeof(_PerfAttr), config
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.syscall(nr, ctypes.byref(attr), -1, 0, -1, 8)        # pid=-1 cpu=0 (system-wide), PERF_FLAG_FD_CLOEXEC
    if fd < 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return fd


def perf_read(fd: int) -> int:
    return int.from_bytes(os.read(fd, 8), "little")


def _smi(args: list[str]) -> str:
    return subprocess.run(args, capture_output=True, text=True, timeout=5, check=True).stdout


class GpuCollector:
    def __init__(self, sys_root: str = "/sys", clock: Callable[[], float] = time.monotonic,
                 opener: Callable[[int, int], int] = perf_open, reader: Callable[[int], int] = perf_read,
                 run: Callable[[list[str]], str] | None = None):
        self.sys_root, self.clock, self.opener, self.reader = sys_root, clock, opener, reader
        self._run = run or _smi
        self._allow_smi = run is not None or sys_root == "/sys"        # tests with a fake /sys must not call the real binary
        self._pmu: dict[str, dict] = {}
        self.unavailable: list[str] = []
        self.notes: list[str] = []                                      # human-readable reasons, shown by `discover`

    # -- helpers --
    def _intel_pmu(self, c: dict, label: str, out: dict) -> None:
        st = self._pmu.setdefault(c["card"], {"fds": {}, "prev": None})
        if not st["fds"]:
            base = None
            for cand in (f"i915_{c['pci'].replace(':', '_')}", "i915"):
                if os.path.isdir(f"{self.sys_root}/bus/event_source/devices/{cand}"):
                    base = f"{self.sys_root}/bus/event_source/devices/{cand}"
                    break
            if base is None:
                self.notes.append(f"{label}: no i915 PMU in /sys/bus/event_source (kernel too old or driver not i915)")
                self.unavailable.append("gpu_utilization_percent")
                return
            try:
                ptype = int(_read(f"{base}/type").strip())
                events = {}
                for f in sorted(glob.glob(f"{base}/events/*-busy")):
                    m = re.search(r"config=(0x[0-9a-fA-F]+)", _read(f))
                    if m:
                        events[os.path.basename(f)[:-len("-busy")]] = int(m.group(1), 16)
                for eng, cfg in events.items():
                    st["fds"][eng] = self.opener(ptype, cfg)
            except OSError as e:
                for fd in st["fds"].values():
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                st["fds"] = {}
                self.notes.append(f"{label}: cannot read utilisation ({e.strerror or e}); needs CAP_PERFMON (see docs/TROUBLESHOOTING.md)")
                self.unavailable.append("gpu_utilization_percent")
                return
            if not st["fds"]:
                self.notes.append(f"{label}: i915 PMU exposes no engine-busy events")
                self.unavailable.append("gpu_utilization_percent")
                return
        now = self.clock()
        cur = {}
        try:
            for eng, fd in st["fds"].items():
                cur[eng] = self.reader(fd)
        except OSError as e:
            self.notes.append(f"{label}: PMU read failed ({e})")
            self.unavailable.append("gpu_utilization_percent")
            st["fds"] = {}
            return
        if st["prev"] is not None and now > st["prev"][1]:
            dt_ns = (now - st["prev"][1]) * 1e9
            pct = [100.0 * (cur[e] - st["prev"][0][e]) / dt_ns for e in cur if e in st["prev"][0] and cur[e] >= st["prev"][0][e]]
            if pct:
                out[("gpu_utilization_percent", label)] = round(max(0.0, min(100.0, max(pct))), 1)   # busiest engine (render/video/blit)
        st["prev"] = (cur, now)

    def _amd(self, c: dict, label: str, out: dict) -> None:
        d = c["dev"]
        b = _num(f"{d}/gpu_busy_percent")
        if b is not None:
            out[("gpu_utilization_percent", label)] = b
        used, tot = _num(f"{d}/mem_info_vram_used"), _num(f"{d}/mem_info_vram_total")
        if used is not None and tot:
            out[("gpu_vram_used_bytes", label)], out[("gpu_vram_total_bytes", label)] = used, tot
            out[("gpu_vram_percent", label)] = round(100.0 * used / tot, 1)
        mhz = None
        try:
            m = re.search(r"(\d+)\s*[Mm][Hh]z\s*\*", _read(f"{d}/pp_dpm_sclk"))
            mhz = float(m.group(1)) if m else None
        except OSError:
            pass
        if mhz is None:
            for f in glob.glob(f"{d}/hwmon/hwmon*/freq1_input"):
                v = _num(f)
                if v:
                    mhz = v / 1e6
                    break
        if mhz is not None:
            out[("gpu_clock_mhz", label)] = round(mhz, 0)
        if b is None:
            self.unavailable.append("gpu_utilization_percent")
            self.notes.append(f"{label}: gpu_busy_percent not exposed by this amdgpu/kernel")

    def _nvidia(self, out: dict) -> bool:
        exe = shutil.which("nvidia-smi") if self._allow_smi else None
        if not exe:
            return False
        try:
            text = self._run([exe, "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,clocks.gr",
                              "--format=csv,noheader,nounits"])
        except (OSError, subprocess.SubprocessError) as e:
            self.notes.append(f"nvidia-smi failed: {e} (under systemd /dev/nvidia* is hidden unless PrivateDevices=no, see docs)")
            self.unavailable.append("gpu_utilization_percent")
            return True
        rows = [r for r in csv.reader(io.StringIO(text), skipinitialspace=True) if len(r) >= 6]

        def f(x: str) -> float | None:
            try:
                return float(x)
            except ValueError:
                return None                                              # "[N/A]", "[Not Supported]"
        for r in rows:
            label = (r[1] if len(rows) == 1 else f"GPU{r[0]} {r[1]}")[:64]
            util, used, tot, clk = f(r[2]), f(r[3]), f(r[4]), f(r[5])
            if util is not None:
                out[("gpu_utilization_percent", label)] = util
            else:
                self.unavailable.append("gpu_utilization_percent")
            if used is not None and tot:
                out[("gpu_vram_used_bytes", label)], out[("gpu_vram_total_bytes", label)] = used * 1048576, tot * 1048576
                out[("gpu_vram_percent", label)] = round(100.0 * used / tot, 1)
            if clk is not None:
                out[("gpu_clock_mhz", label)] = clk
        return True

    # -- public --
    def collect(self) -> dict[Key, float]:
        self.unavailable, self.notes = [], []
        out: dict[Key, float] = {}
        any_gpu = self._nvidia(out)
        for c in list_cards(self.sys_root):
            if c["driver"] in ("nvidia", "nouveau") and any_gpu:
                continue
            any_gpu = True
            label = f"{c['card']} ({c['driver']})"
            if c["driver"] == "i915":
                for f in ("gt/gt0/rps_act_freq_mhz", "gt_act_freq_mhz", "gt/gt0/rps_cur_freq_mhz", "gt_cur_freq_mhz"):
                    v = _num(f"{c['path']}/{f}")
                    if v is not None:
                        out[("gpu_clock_mhz", label)] = v
                        break
                self._intel_pmu(c, label, out)
            elif c["driver"] in ("amdgpu", "radeon"):
                self._amd(c, label, out)
            else:
                self.notes.append(f"{label}: no reader for driver '{c['driver']}'")
        self.unavailable = sorted(set(self.unavailable))
        return out

    def close(self) -> None:
        for st in self._pmu.values():
            for fd in st["fds"].values():
                try:
                    os.close(fd)
                except OSError:
                    pass
            st["fds"] = {}


def diagnose(sys_root: str = "/sys", sleep: float = 1.0) -> list[str]:
    """Lines for `discover`: which GPUs exist and what can be read from each."""
    cards = list_cards(sys_root)
    g = GpuCollector(sys_root)
    g.collect()
    time.sleep(sleep)
    got = g.collect()
    lines = [f"card {c['card']} driver={c['driver']} pci={c['pci']}" for c in cards]
    if not cards and not got:
        return ["no GPU found (nothing will be reported)"]
    for (name, label), v in sorted(got.items()):
        lines.append(f"  {label}: {name} = {v:g}")
    lines += [f"  ! {n}" for n in g.notes]
    g.close()
    return lines
