"""resmon — v2 A1(Step 1): 翻訳スタックの重さを一定間隔で記録する。

プロセスごとのメモリ・CPU・電力量と、GPU・メモリ逼迫・発熱をまとめて 1 行の JSONL にする
(logs/resmon_<日時>.jsonl・rid="RES")。会議の自動起動(run_meeting.command)と再現会議ベンチ
(src/bench/run_bench.py)が、スタックと一緒に起動する。

1 回の採取 = phase "res_sample":
  procs: 役割ごとのプロセス(下の ROLES。Zoom は "zoom:<実行ファイル名>")
    fp_mb   phys_footprint(アクティビティモニタの「メモリ」。MLX の GPU バッファも含む)
    rss_mb  常駐サイズ
    cpu_pct 前回の採取からの CPU 時間 ÷ 経過時間(100 = 1 コアぶん)
    mj      前回の採取からの消費電力量(mJ。ri_energy_nj。CPU 側のみで GPU は含まない)
  sys:  gpu_util / gpu_mem_mb(ioreg の PerformanceStatistics。システム全体)・
        swap_used_mb・free_mb・compressed_mb・wired_mb(vm_stat)・pressure(1 正常 / 2 警告 / 4 危険)
終了時(SIGTERM・時間切れ)に phase "res_summary"(役割ごとの最大・平均)を 1 行。

実行:
    spikes/.venv-ml/bin/python src/resmon.py [秒] [--interval 5]
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import plistlib
import re
import signal
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import Metrics  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# コマンド行 → 役割。上から順に当てる。
ROLES: list[tuple[str, re.Pattern]] = [(name, re.compile(p)) for name, p in (
    ("send", r"src/pipeline\.py"),
    ("recv", r"src/receive\.py"),
    ("translator", r"src/translate_server\.py"),
    ("stt_helper", r"stt-helper"),
    ("tap_helper", r"tap-helper"),
    ("panel", r"control-panel"),
    ("subtitle", r"subtitle-window"),
    ("hotkey", r"hotkey-helper"),
    ("watcher", r"src/meeting_watcher\.py"),
    ("bypass", r"src/passthrough\.py"),
    ("gui", r"transcue"),
    ("launcher", r"run_meeting\.command"),
    ("bench", r"src/bench/run_bench\.py"),
    ("resmon", r"src/resmon\.py"),
    ("afplay", r"(^|/)afplay\b"),
)]
ZOOM_PREFIX = "/Applications/zoom.us.app/"


# --- proc_pid_rusage(libproc)。SDK の sys/resource.h の rusage_info_v6 と同じ並び -------------------
_U64 = ("ri_user_time ri_system_time ri_pkg_idle_wkups ri_interrupt_wkups ri_pageins ri_wired_size "
        "ri_resident_size ri_phys_footprint ri_proc_start_abstime ri_proc_exit_abstime ri_child_user_time "
        "ri_child_system_time ri_child_pkg_idle_wkups ri_child_interrupt_wkups ri_child_pageins "
        "ri_child_elapsed_abstime ri_diskio_bytesread ri_diskio_byteswritten ri_cpu_time_qos_default "
        "ri_cpu_time_qos_maintenance ri_cpu_time_qos_background ri_cpu_time_qos_utility "
        "ri_cpu_time_qos_legacy ri_cpu_time_qos_user_initiated ri_cpu_time_qos_user_interactive "
        "ri_billed_system_time ri_serviced_system_time ri_logical_writes ri_lifetime_max_phys_footprint "
        "ri_instructions ri_cycles ri_billed_energy ri_serviced_energy ri_interval_max_phys_footprint "
        "ri_runnable_time ri_flags ri_user_ptime ri_system_ptime ri_pinstructions ri_pcycles ri_energy_nj "
        "ri_penergy_nj ri_secure_time_in_system ri_secure_ptime_in_system ri_neural_footprint "
        "ri_lifetime_max_neural_footprint ri_interval_max_neural_footprint ri_conclave_footprint "
        "ri_page_wait_time_mach ri_page_cache_hits").split()


class RUsageInfoV6(ctypes.Structure):
    # 末尾の _pad は将来カーネル側の構造体が伸びたときの保険(書き込みがはみ出さないように)。
    _fields_ = ([("ri_uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in _U64]
                + [("ri_reserved", ctypes.c_uint64 * 6), ("_pad", ctypes.c_uint64 * 32)])


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
_libc.proc_pid_rusage.restype = ctypes.c_int
_tb = _Timebase()
_libc.mach_timebase_info(ctypes.byref(_tb))
# ri_user_time / ri_system_time は mach の時刻単位(Apple Silicon では 1 単位 = 125/3 ns)。
_NS_PER_TICK = _tb.numer / _tb.denom if _tb.denom else 1.0


def rusage(pid: int) -> RUsageInfoV6 | None:
    ri = RUsageInfoV6()
    if _libc.proc_pid_rusage(pid, 6, ctypes.byref(ri)) != 0:
        return None
    return ri


def cpu_ns(ri: RUsageInfoV6) -> float:
    return (ri.ri_user_time + ri.ri_system_time) * _NS_PER_TICK


# --- システム全体 ------------------------------------------------------------------------------

def _run(cmd: list[str], timeout: float = 5.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def gpu_stats() -> dict:
    """ioreg の GPU 統計(sudo 不要)。取れなければ空。"""
    try:
        out = subprocess.run(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator", "-a"], capture_output=True,
                             timeout=5).stdout
        for dev in plistlib.loads(out) if out else []:
            ps = dev.get("PerformanceStatistics") or {}
            if "Device Utilization %" in ps:
                return {"gpu_util": ps.get("Device Utilization %"),
                        "gpu_renderer": ps.get("Renderer Utilization %"),
                        "gpu_mem_mb": round(ps.get("In use system memory", 0) / 2**20),
                        "gpu_alloc_mb": round(ps.get("Alloc system memory", 0) / 2**20)}
    except (OSError, subprocess.TimeoutExpired, plistlib.InvalidFileException, ValueError):
        pass
    return {}


def mem_stats() -> dict:
    out = _run(["vm_stat"])
    m = re.search(r"page size of (\d+) bytes", out)
    page = int(m.group(1)) if m else 16384

    def pages(label: str) -> int:
        mm = re.search(rf"^{re.escape(label)}:\s+(\d+)", out, re.M)
        return int(mm.group(1)) if mm else 0

    d = {"free_mb": round((pages("Pages free") + pages("Pages speculative")) * page / 2**20),
         "compressed_mb": round(pages("Pages occupied by compressor") * page / 2**20),
         "wired_mb": round(pages("Pages wired down") * page / 2**20)}
    sw = re.search(r"used = ([\d.]+)M", _run(["sysctl", "-n", "vm.swapusage"]))
    if sw:
        d["swap_used_mb"] = round(float(sw.group(1)))
    lvl = _run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip()
    if lvl.isdigit():
        d["pressure"] = int(lvl)
    return d


# --- 採取 ----------------------------------------------------------------------------------------

def classify(args: str) -> str | None:
    if args.startswith(ZOOM_PREFIX):
        return "zoom:" + args.split(" ", 1)[0].rsplit("/", 1)[-1]
    for name, pat in ROLES:
        if pat.search(args):
            if name == "stt_helper":
                loc = re.search(r"stt-helper\s+([a-z]{2}-[A-Z]{2})", args)
                return f"stt_helper:{loc.group(1)}" if loc else name
            return name
    return None


class Sampler:
    def __init__(self, m: Metrics, interval_s: float):
        self.m = m
        self.interval_s = interval_s
        self._prev: dict[int, tuple[float, float, int]] = {}   # pid → (時刻, cpu_ns, energy_nj)
        self._stats: dict[str, dict] = {}                       # role → 集計(res_summary 用)
        self._sys_stats: dict[str, list[float]] = {}
        self.n = 0

    def sample(self) -> dict:
        now = time.monotonic()
        procs = []
        seen: set[int] = set()
        for line in _run(["ps", "-axww", "-o", "pid=,args="]).splitlines():
            line = line.strip()
            pid_s, _, args = line.partition(" ")
            if not pid_s.isdigit():
                continue
            role = classify(args.strip())
            if role is None:
                continue
            pid = int(pid_s)
            ri = rusage(pid)
            if ri is None:
                continue
            seen.add(pid)
            c, e = cpu_ns(ri), int(ri.ri_energy_nj)
            rec = {"role": role, "pid": pid, "fp_mb": round(ri.ri_phys_footprint / 2**20, 1),
                   "rss_mb": round(ri.ri_resident_size / 2**20, 1)}
            prev = self._prev.get(pid)
            if prev and now > prev[0]:
                rec["cpu_pct"] = round((c - prev[1]) / 1e9 / (now - prev[0]) * 100, 1)
                rec["mj"] = round((e - prev[2]) / 1e6, 1)
            self._prev[pid] = (now, c, e)
            procs.append(rec)
            st = self._stats.setdefault(role, {"fp_max": 0.0, "fp_sum": 0.0, "n": 0, "cpu_sum": 0.0,
                                               "cpu_n": 0, "cpu_max": 0.0, "mj": 0.0})
            st["fp_max"] = max(st["fp_max"], rec["fp_mb"])
            st["fp_sum"] += rec["fp_mb"]
            st["n"] += 1
            if "cpu_pct" in rec:
                st["cpu_sum"] += rec["cpu_pct"]
                st["cpu_n"] += 1
                st["cpu_max"] = max(st["cpu_max"], rec["cpu_pct"])
                st["mj"] += rec["mj"]
        for pid in list(self._prev):
            if pid not in seen:
                del self._prev[pid]
        sysd = {**gpu_stats(), **mem_stats()}
        for k, v in sysd.items():
            if isinstance(v, (int, float)):
                self._sys_stats.setdefault(k, []).append(float(v))
        zrt = [p for p in procs if not p["role"].startswith("zoom:") and p["role"] != "resmon"]
        self.n += 1
        return self.m.emit("RES", "res_sample", n=self.n, procs=procs, sys=sysd,
                           zrt_fp_mb=round(sum(p["fp_mb"] for p in zrt), 1),
                           zrt_cpu_pct=round(sum(p.get("cpu_pct", 0) for p in zrt), 1),
                           zoom_fp_mb=round(sum(p["fp_mb"] for p in procs if p["role"].startswith("zoom:")), 1))

    def summary(self) -> dict:
        roles = {r: {"fp_max_mb": round(s["fp_max"]), "fp_avg_mb": round(s["fp_sum"] / s["n"]) if s["n"] else 0,
                     "cpu_avg_pct": round(s["cpu_sum"] / s["cpu_n"], 1) if s["cpu_n"] else None,
                     "cpu_max_pct": round(s["cpu_max"], 1), "energy_j": round(s["mj"] / 1000, 1)}
                 for r, s in sorted(self._stats.items())}
        sysd = {k: {"avg": round(sum(v) / len(v), 1), "max": round(max(v), 1)} for k, v in self._sys_stats.items() if v}
        return self.m.emit("RES", "res_summary", samples=self.n, interval_s=self.interval_s, roles=roles, sys=sysd)


def main() -> int:
    argv = sys.argv[1:]
    with (Path(__file__).resolve().parent / "config.toml").open("rb") as f:
        cfg = tomllib.load(f)
    cfg["_root"] = str(ROOT)
    rc = cfg.get("resmon", {})
    if not rc.get("enabled", True):
        return 0
    interval = float(rc.get("interval_s", 5.0))
    if "--interval" in argv and argv.index("--interval") + 1 < len(argv):
        interval = float(argv[argv.index("--interval") + 1])
    nums = [a for a in argv if a.replace(".", "", 1).isdigit()]
    max_s = float(nums[0]) if nums else 8 * 3600.0
    m = Metrics.from_config(cfg, run_tag="resmon")
    m.echo = False   # 標準エラーへは出さない(採取は数百行になる)
    stop = threading.Event()
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, lambda *_: stop.set())
    sampler = Sampler(m, interval)
    m.emit("RES", "res_boot", interval_s=interval, max_s=max_s, ns_per_tick=_NS_PER_TICK, pid=os.getpid(),
           bench=os.environ.get("ZRT_BENCH_ID"))
    t_end = time.monotonic() + max_s
    try:
        while not stop.is_set() and time.monotonic() < t_end:
            sampler.sample()
            stop.wait(interval)
    finally:
        sampler.summary()
        m.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
