#!/usr/bin/env python3
"""
aggregate_dvfs_csv.py

Write one unified DVFS CSV at:
    ~/COSC_498/miniMXE/repro/agg/dvfs.csv

Behavior:
- No CF columns at all.
- Uses the same workload/path style as print_one_weighted_speedup_table.py
  for n1 and multicore DVFS runs.
- Baseline lookup order:
    1) dvfs/12_baseline_run/runs
    2) dvfs/1_main_dvfs/runs
    3) hca/hca_sunnycove   (n1 fallback only)
- Adds Stage-6-style metrics (tables 1-4 columns) as CSV columns.
- Writes rows for:
    * n1 workloads
    * n4 workloads
    * n8 workloads

Notes:
- n1 rows use makespan + speedup.
- multicore rows use raw per-core times + WS/N + raw WS terms.
- Stage-6 columns are populated from the selected DVFS run whenever
  sniper.log / sim.stats.sqlite3 are available.
"""

import os
import re
import csv
import math
import sqlite3
import argparse
from collections import defaultdict

# -----------------------------------------------------------------------------
# Defaults / config
# -----------------------------------------------------------------------------
DEFAULT_BASE = os.path.expanduser("~/COSC_498/miniMXE/repro")
DEFAULT_OUT = os.path.expanduser("~/COSC_498/miniMXE/repro/agg/dvfs.csv")
SIZES = [16, 32, 128]
BASE_FREQ_GHZ = 2.2

WORKLOADS_N1 = [
    "500.perlbench_r",
    "502.gcc_r",
    "505.mcf_r",
    "520.omnetpp_r",
    "523.xalancbmk_r",
    "531.deepsjeng_r",
    "541.leela_r",
    "557.xz_r",
    "648.exchange2_s",
    "649.fotonik3d_s",
]

WORKLOADS_N4 = [
    "505.mcf_r+500.perlbench_r+648.exchange2_s+649.fotonik3d_s",
    "505.mcf_r+505.mcf_r+502.gcc_r+502.gcc_r",
    "505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r",
    "502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r",
    "523.xalancbmk_r+523.xalancbmk_r+502.gcc_r+502.gcc_r",
]

WORKLOADS_N8 = [
    "505.mcf_r+505.mcf_r+500.perlbench_r+500.perlbench_r+648.exchange2_s+648.exchange2_s+649.fotonik3d_s+649.fotonik3d_s",
    "505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r",
    "505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r",
    "502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r",
    "523.xalancbmk_r+523.xalancbmk_r+523.xalancbmk_r+523.xalancbmk_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r",
]

FIXED_DVFS_ROOT = None
SMART_TTL_ROOT = None
MAIN_DVFS_ROOT = None
BASELINE_RUN_ROOT = None
HCA_ROOT = None


def init_paths(base: str):
    global FIXED_DVFS_ROOT, SMART_TTL_ROOT, MAIN_DVFS_ROOT, BASELINE_RUN_ROOT, HCA_ROOT
    FIXED_DVFS_ROOT = os.path.join(base, "dvfs", "7_fixed_dvfs", "runs")
    SMART_TTL_ROOT = os.path.join(base, "dvfs", "6_smart_dvfs_ttl", "runs")
    MAIN_DVFS_ROOT = os.path.join(base, "dvfs", "1_main_dvfs", "runs")
    BASELINE_RUN_ROOT = os.path.join(base, "dvfs", "12_baseline_run", "runs")
    HCA_ROOT = os.path.join(base, "hca", "hca_sunnycove")


# -----------------------------------------------------------------------------
# SQLite helpers
# -----------------------------------------------------------------------------
def _get_stat(db_path: str, obj: str, metric: str, prefix: str = "roi-end", core: int = 0):
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        c = conn.cursor()
        c.execute(
            '''SELECT v.value FROM "values" v
               JOIN names n ON v.nameid = n.nameid
               JOIN prefixes p ON v.prefixid = p.prefixid
               WHERE n.objectname=? AND n.metricname=? AND p.prefixname=? AND v.core=?''',
            (obj, metric, prefix, core),
        )
        row = c.fetchone()
        conn.close()
        return float(row[0]) if row else None
    except Exception:
        return None


def get_delta(db_path: str, obj: str, metric: str, core: int = 0):
    end = _get_stat(db_path, obj, metric, "roi-end", core)
    begin = _get_stat(db_path, obj, metric, "roi-begin", core)
    if end is not None and begin is not None:
        return end - begin
    return end


def get_num_cores(db_path: str) -> int:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        c = conn.cursor()
        c.execute('SELECT MAX(v.core) FROM "values" v')
        row = c.fetchone()
        conn.close()
        return int(row[0]) + 1 if row and row[0] is not None else 1
    except Exception:
        return 1


def fmt_time_vec(xs):
    if not xs:
        return None
    return "|".join(f"{x:.9f}" for x in xs if x is not None and x > 0)

# -----------------------------------------------------------------------------
# Time extraction
# -----------------------------------------------------------------------------
def _parse_sim_out_times(path: str):
    begins, ends, elapsed = [], [], []
    try:
        with open(path) as f:
            for line in f:
                s = line.strip()
                if s.startswith("performance_model.elapsed_time_begin"):
                    begins = [float(x) for x in s.split("=")[1].split(",") if x.strip()]
                elif s.startswith("performance_model.elapsed_time_end"):
                    ends = [float(x) for x in s.split("=")[1].split(",") if x.strip()]
                elif s.startswith("performance_model.elapsed_time"):
                    if "begin" not in s and "end" not in s:
                        elapsed = [float(x) for x in s.split("=")[1].split(",") if x.strip()]
    except Exception:
        pass

    if begins and ends and len(begins) == len(ends):
        return [(e - b) * 1e-15 for b, e in zip(begins, ends)]
    if elapsed:
        return [e * 1e-15 for e in elapsed]
    return None


def get_times_from_dir(run_dir: str):
    if not run_dir or not os.path.isdir(run_dir):
        return None

    sim_out = os.path.join(run_dir, "sim.out")
    times = _parse_sim_out_times(sim_out)
    if times:
        return times

    db_path = os.path.join(run_dir, "sim.stats.sqlite3")
    if not os.path.exists(db_path):
        return None

    n_cores = get_num_cores(db_path)
    result = []
    for core in range(n_cores):
        t = get_delta(db_path, "thread", "elapsed_time", core)
        if t is None or t <= 0:
            t = get_delta(db_path, "performance_model", "elapsed_time", core)
        if t is not None and t > 0:
            result.append(t * 1e-15)
        else:
            result.append(None)

    if any(v is not None and v > 0 for v in result):
        return result
    return None


def active_times_from_dir(run_dir: str):
    ts = get_times_from_dir(run_dir)
    if not ts:
        return None
    vals = [t for t in ts if t is not None and t > 0]
    return vals if vals else None


def makespan_s(run_dir: str):
    ts = active_times_from_dir(run_dir)
    if not ts:
        return None
    return max(ts)


# -----------------------------------------------------------------------------
# Path discovery
# -----------------------------------------------------------------------------
def _has_results(run_dir: str) -> bool:
    if not run_dir or not os.path.isdir(run_dir):
        return False
    if os.path.exists(os.path.join(run_dir, "sim.out")):
        return True
    if os.path.exists(os.path.join(run_dir, "sim.stats.sqlite3")):
        return True
    return False


def _pick_first_matching_dir(sz_dir: str, pred):
    if not os.path.isdir(sz_dir):
        return None
    cands = []
    for d in sorted(os.listdir(sz_dir)):
        full = os.path.join(sz_dir, d)
        if os.path.isdir(full) and pred(d):
            cands.append(full)
    return cands[0] if cands else None


def find_fixed_variant_dir_n1(bench: str, size_mb: int):
    sz_dir = os.path.join(FIXED_DVFS_ROOT, bench, "n1", f"l3_{size_mb}MB")
    if not os.path.isdir(sz_dir):
        return None

    out = []
    for d in sorted(os.listdir(sz_dir)):
        full = os.path.join(sz_dir, d)
        if not os.path.isdir(full):
            continue
        if d.startswith("lc_"):
            out.append(full)
    return out[0] if out else None


def find_smart_ttl_dir(bench: str, n_tag: str, size_mb: int):
    sz_dir = os.path.join(SMART_TTL_ROOT, bench, n_tag, f"l3_{size_mb}MB")
    return _pick_first_matching_dir(
        sz_dir,
        lambda d: d.startswith("lc_c") and d.endswith("_mram14")
    )


def find_main_baseline_dir(bench: str, n_tag: str, size_mb: int, leaf_exact: str):
    sz_dir = os.path.join(MAIN_DVFS_ROOT, bench, n_tag, f"l3_{size_mb}MB")
    if not os.path.isdir(sz_dir):
        return None
    exact = os.path.join(sz_dir, leaf_exact)
    if os.path.isdir(exact):
        return exact
    for d in os.listdir(sz_dir):
        full = os.path.join(sz_dir, d)
        if os.path.isdir(full) and d == leaf_exact:
            return full
    return None


def find_baseline_run_dir(bench: str, n_tag: str, size_mb: int, leaf_exact: str):
    d = os.path.join(BASELINE_RUN_ROOT, bench, n_tag, f"l3_{size_mb}MB", leaf_exact)
    return d if os.path.isdir(d) else None


def find_hca_baseline_dir_n1(bench: str, size_mb: int, leaf_exact: str):
    bench_dot = bench
    bench_us = bench.replace(".", "_", 1)
    sz_tag = f"sz{size_mb}M"
    campaigns = [
        "1_baselines",
        "2_cross_node/mram14",
        "2_cross_node/mram32",
        "3_static_hca",
    ]
    for campaign in campaigns:
        runs_dir = os.path.join(HCA_ROOT, campaign, "runs")
        if not os.path.isdir(runs_dir):
            continue
        for root, _, _ in os.walk(runs_dir):
            parts = root.split(os.sep)
            if parts[-1] != leaf_exact:
                continue
            if sz_tag not in parts:
                continue
            if not any(p.startswith(bench_dot) or p.startswith(bench_us) for p in parts):
                continue
            return root
    return None


def find_sram7_baseline(bench: str, n_tag: str, size_mb: int):
    # 1) 12_baseline_run first
    d = find_baseline_run_dir(bench, n_tag, size_mb, "baseline_sram_only_sram7")
    if d and _has_results(d):
        return d

    # 2) 1_main_dvfs
    d = find_main_baseline_dir(bench, n_tag, size_mb, "baseline_sram_only_sram7")
    if d and _has_results(d):
        return d

    # 3) hca fallback only for n1
    if n_tag == "n1":
        hca = find_hca_baseline_dir_n1(bench, size_mb, "baseline_sram_only_sram7")
        if hca and _has_results(hca):
            return hca

    return None


def find_mram14_baseline(bench: str, n_tag: str, size_mb: int):
    # 1) 12_baseline_run first
    d = find_baseline_run_dir(bench, n_tag, size_mb, "baseline_mram_only_mram14")
    if d and _has_results(d):
        return d

    # 2) 1_main_dvfs
    d = find_main_baseline_dir(bench, n_tag, size_mb, "baseline_mram_only_mram14")
    if d and _has_results(d):
        return d

    # 3) hca fallback only for n1
    if n_tag == "n1":
        hca = find_hca_baseline_dir_n1(bench, size_mb, "baseline_mram_only_mram14")
        if hca and _has_results(hca):
            return hca

    return None


# -----------------------------------------------------------------------------
# Weighted speedup helpers
# -----------------------------------------------------------------------------
def single_speedup_ratio(base_dir: str, variant_dir: str):
    b = makespan_s(base_dir)
    v = makespan_s(variant_dir)
    if b is None or v is None or v <= 0:
        return None
    return b / v


def paired_active_times(base_dir: str, variant_dir: str):
    bt = get_times_from_dir(base_dir)
    vt = get_times_from_dir(variant_dir)
    if not bt or not vt:
        return None

    pairs = []
    for b, v in zip(bt, vt):
        if b is None or v is None or b <= 0 or v <= 0:
            continue
        pairs.append((b, v))

    return pairs if pairs else None


def per_core_weighted_speedups(base_dir: str, variant_dir: str):
    pairs = paired_active_times(base_dir, variant_dir)
    if not pairs:
        return None
    return [b / v for b, v in pairs]


def normalized_weighted_speedup(base_dir: str, variant_dir: str):
    vals = per_core_weighted_speedups(base_dir, variant_dir)
    if not vals:
        return None
    return sum(vals) / len(vals)


# -----------------------------------------------------------------------------
# Stage 6 helpers
# -----------------------------------------------------------------------------
def shorten_workload(wl: str) -> str:
    parts = wl.split("+")

    def short(b):
        m = re.match(r"\d+\.(\w+?)(_[rs])?$", b)
        return m.group(1) if m else b

    counts = []
    prev, cnt = None, 0
    for p in parts:
        s = short(p)
        if s == prev:
            cnt += 1
        else:
            if prev is not None:
                counts.append((prev, cnt))
            prev, cnt = s, 1
    if prev is not None:
        counts.append((prev, cnt))

    pieces = []
    for name, c in counts:
        pieces.append(f"{name}x{c}" if c > 1 else name)
    return "+".join(pieces)


_LC_CHANGE_RE = re.compile(
    r"\[LC\] DVFS Change \[PLM\]: "
    r"P_est=([\d.]+)W "
    r"\(llc_leak=([\d.]+)W P_nocache=([\d.]+)W\) "
    r"Target=([\d.]+)W "
    r"f_lookup=([\d.]+)GHz\(\w+\) "
    r"u_sum=([\d.]+) "
    r"ipc=([\d.]+) "
    r"u_sum_x_ipc=([\d.]+) "
    r"boosted=(\d+)/(\d+) "
    r"f\[min/avg/max\]=\[([\d.]+)/([\d.]+)/([\d.]+)\] GHz"
)

_LC_FINAL_RE = re.compile(
    r"\[LC\] Final: "
    r"P_est=([\d.]+)W "
    r"base_f=([\d.]+)GHz "
    r"f\[min/avg/max\]=\[([\d.]+)/([\d.]+)/([\d.]+)\] GHz "
    r"llc_leak=([\d.]+)W "
    r"selective=(\w+) k=(\d+) "
    r"power_model=(\w+)"
)


def parse_sniper_log(log_path: str) -> dict:
    intervals = []
    final = None

    if not os.path.exists(log_path):
        return {"intervals": [], "final": None}

    try:
        with open(log_path) as f:
            for line in f:
                m = _LC_CHANGE_RE.search(line)
                if m:
                    intervals.append({
                        "P_est": float(m.group(1)),
                        "llc_leak": float(m.group(2)),
                        "P_nocache": float(m.group(3)),
                        "Target": float(m.group(4)),
                        "f_lookup": float(m.group(5)),
                        "u_sum": float(m.group(6)),
                        "ipc": float(m.group(7)),
                        "u_sum_x_ipc": float(m.group(8)),
                        "boosted_k": int(m.group(9)),
                        "boosted_n": int(m.group(10)),
                        "f_min": float(m.group(11)),
                        "f_avg": float(m.group(12)),
                        "f_max": float(m.group(13)),
                    })
                    continue
                m = _LC_FINAL_RE.search(line)
                if m:
                    final = {
                        "P_est": float(m.group(1)),
                        "base_f": float(m.group(2)),
                        "f_min": float(m.group(3)),
                        "f_avg": float(m.group(4)),
                        "f_max": float(m.group(5)),
                        "llc_leak": float(m.group(6)),
                        "selective": m.group(7),
                        "k": int(m.group(8)),
                        "power_model": m.group(9),
                    }
    except Exception:
        return {"intervals": [], "final": None}

    return {"intervals": intervals, "final": final}


def get_per_core_metrics(db_path: str, n_cores: int) -> list:
    cores = []
    for c in range(n_cores):
        instr = get_delta(db_path, "performance_model", "instruction_count", c)
        elapsed = get_delta(db_path, "thread", "elapsed_time", c)
        nonidle = get_delta(db_path, "thread", "nonidle_elapsed_time", c)
        idle = get_delta(db_path, "performance_model", "idle_elapsed_time", c)

        cpi_base = get_delta(db_path, "rob_timer", "cpiBase", c)
        cpi_l3 = get_delta(db_path, "rob_timer", "cpiDataCacheL3", c)
        cpi_dram = get_delta(db_path, "rob_timer", "cpiDataCachedram", c)
        cpi_dram_cache = None
        for metric_name in ["cpiDataCachedram-cache", "cpiDataCachedram_cache"]:
            cpi_dram_cache = get_delta(db_path, "rob_timer", metric_name, c)
            if cpi_dram_cache is not None:
                break
        cpi_dvfs = get_delta(db_path, "performance_model", "cpiSyncDvfsTransition", c)
        outstanding = get_delta(db_path, "rob_timer", "outstandingLongLatencyCycles", c)

        l3_loads = get_delta(db_path, "L3", "loads", c)
        l3_misses = get_delta(db_path, "L3", "load-misses", c)

        cores.append({
            "instruction_count": instr,
            "elapsed_time": elapsed,
            "nonidle_elapsed_time": nonidle,
            "idle_elapsed_time": idle,
            "cpiBase": cpi_base,
            "cpiDataCacheL3": cpi_l3,
            "cpiDataCachedram": cpi_dram,
            "cpiDataCachedram_cache": cpi_dram_cache,
            "cpiSyncDvfsTransition": cpi_dvfs,
            "outstandingLongLatencyCycles": outstanding,
            "l3_loads": l3_loads,
            "l3_load_misses": l3_misses,
        })
    return cores


def _safe_std(vals):
    if not vals:
        return None
    avg = sum(vals) / len(vals)
    return (sum((v - avg) ** 2 for v in vals) / len(vals)) ** 0.5


def stage6_metrics_from_run(run_dir: str) -> dict:
    out = {
        # table 1
        "t1_num_intervals": None,
        "t1_num_transitions": None,
        "t1_transition_rate": None,
        "t1_avg_freq_ghz": None,
        "t1_max_freq_ghz": None,
        "t1_residency_bins": None,
        # table 2
        "t2_cap_w": None,
        "t2_avg_power_w": None,
        "t2_avg_slack_w": None,
        "t2_pct_over": None,
        "t2_pct_band": None,
        "t2_pct_room": None,
        # table 3
        "t3_avg_util": None,
        "t3_std_util": None,
        "t3_top1_minus_top2_util": None,
        "t3_avg_ipc": None,
        "t3_std_ipc": None,
        "t3_instr_m_per_core": None,
        # table 4
        "t4_mpki": None,
        "t4_mem_cpi_frac": None,
        "t4_ll_stall_frac": None,
        "t4_dvfs_ovh_frac": None,
        "t4_mpki_per_core": None,
    }

    if not run_dir or not os.path.isdir(run_dir):
        return out

    # -------------------------
    # tables 1 and 2 from sniper.log
    # -------------------------
    log_path = os.path.join(run_dir, "sniper.log")
    lc = parse_sniper_log(log_path)
    intervals = lc["intervals"]

    if intervals:
        n_intv = len(intervals)
        n_trans = sum(
            1 for i in range(1, n_intv)
            if abs(intervals[i]["f_max"] - intervals[i - 1]["f_max"]) > 0.001
        )
        t_rate = n_trans / n_intv if n_intv > 0 else None
        avg_f = sum(iv["f_max"] for iv in intervals) / n_intv
        max_f = max(iv["f_max"] for iv in intervals)

        freq_counts = defaultdict(int)
        for iv in intervals:
            f_bin = round(iv["f_max"], 1)
            freq_counts[f_bin] += 1

        residency_parts = []
        for f_bin in sorted(freq_counts.keys()):
            pct = freq_counts[f_bin] / n_intv * 100.0
            if pct >= 1.0:
                residency_parts.append(f"{f_bin:.1f}:{pct:.0f}%")

        out["t1_num_intervals"] = n_intv
        out["t1_num_transitions"] = n_trans
        out["t1_transition_rate"] = t_rate
        out["t1_avg_freq_ghz"] = avg_f
        out["t1_max_freq_ghz"] = max_f
        out["t1_residency_bins"] = " ".join(residency_parts) if residency_parts else None

        cap = intervals[0]["Target"]
        avg_p = sum(iv["P_est"] for iv in intervals) / n_intv
        avg_slack = cap - avg_p

        n_over = sum(1 for iv in intervals if iv["P_est"] > iv["Target"])
        hyst = 0.10
        n_band = sum(
            1 for iv in intervals
            if 0 <= (iv["Target"] - iv["P_est"]) <= hyst
        )
        n_room = sum(
            1 for iv in intervals
            if (iv["Target"] - iv["P_est"]) > hyst
        )

        out["t2_cap_w"] = cap
        out["t2_avg_power_w"] = avg_p
        out["t2_avg_slack_w"] = avg_slack
        out["t2_pct_over"] = n_over / n_intv
        out["t2_pct_band"] = n_band / n_intv
        out["t2_pct_room"] = n_room / n_intv

    # -------------------------
    # tables 3 and 4 from sqlite
    # -------------------------
    db_path = os.path.join(run_dir, "sim.stats.sqlite3")
    if not os.path.exists(db_path):
        return out

    n_cores = get_num_cores(db_path)
    cores = get_per_core_metrics(db_path, n_cores)
    if not cores:
        return out

    # table 3
    utils = []
    ipcs = []
    instrs_m = []

    for cm in cores:
        elapsed = cm["elapsed_time"]
        nonidle = cm["nonidle_elapsed_time"]
        instr = cm["instruction_count"]

        if elapsed and elapsed > 0 and nonidle is not None:
            utils.append(nonidle / elapsed)
        else:
            utils.append(0.0)

        if nonidle and nonidle > 0 and instr:
            approx_cycles = nonidle * BASE_FREQ_GHZ * 1e9 / 1e15
            ipcs.append(instr / approx_cycles if approx_cycles > 0 else 0.0)
        else:
            ipcs.append(0.0)

        instrs_m.append((instr / 1e6) if instr else 0.0)

    if utils:
        avg_u = sum(utils) / len(utils)
        std_u = _safe_std(utils)
        sorted_u = sorted(utils, reverse=True)
        gap12 = sorted_u[0] - sorted_u[1] if len(sorted_u) >= 2 else 0.0

        out["t3_avg_util"] = avg_u
        out["t3_std_util"] = std_u
        out["t3_top1_minus_top2_util"] = gap12

    if ipcs:
        out["t3_avg_ipc"] = sum(ipcs) / len(ipcs)
        out["t3_std_ipc"] = _safe_std(ipcs)

    if instrs_m:
        out["t3_instr_m_per_core"] = "|".join(f"{x:.3f}" for x in instrs_m)

    # table 4
    tot_instr = 0.0
    tot_l3_misses = 0.0
    tot_mem_cpi = 0.0
    tot_nonidle = 0.0
    tot_outstanding = 0.0
    tot_dvfs = 0.0
    per_core_mpki = []

    for cm in cores:
        instr = cm["instruction_count"] or 0.0
        tot_instr += instr

        misses = cm["l3_load_misses"] or 0.0
        tot_l3_misses += misses
        per_core_mpki.append(misses / instr * 1000.0 if instr > 0 else 0.0)

        nonidle = cm["nonidle_elapsed_time"] or 0.0
        tot_nonidle += nonidle

        cpi_l3 = cm["cpiDataCacheL3"] or 0.0
        cpi_dram = cm["cpiDataCachedram"] or 0.0
        cpi_dram_c = cm["cpiDataCachedram_cache"] or 0.0
        tot_mem_cpi += cpi_l3 + cpi_dram + cpi_dram_c

        tot_outstanding += cm["outstandingLongLatencyCycles"] or 0.0
        tot_dvfs += cm["cpiSyncDvfsTransition"] or 0.0

    if tot_instr > 0:
        out["t4_mpki"] = tot_l3_misses / tot_instr * 1000.0
    if tot_nonidle > 0:
        out["t4_mem_cpi_frac"] = tot_mem_cpi / tot_nonidle
        out["t4_ll_stall_frac"] = tot_outstanding / tot_nonidle
        out["t4_dvfs_ovh_frac"] = tot_dvfs / tot_nonidle
    if per_core_mpki:
        out["t4_mpki_per_core"] = "|".join(f"{x:.3f}" for x in per_core_mpki)

    return out


# -----------------------------------------------------------------------------
# Row builders
# -----------------------------------------------------------------------------
def build_n1_rows():
    rows = []
    for bench in WORKLOADS_N1:
        for size_mb in SIZES:
            dvfs_dir = find_fixed_variant_dir_n1(bench, size_mb)
            sram7_dir = find_sram7_baseline(bench, "n1", size_mb)
            mram14_dir = find_mram14_baseline(bench, "n1", size_mb)

            # Use active_times_from_dir so behavior matches your original code
            dvfs_times = active_times_from_dir(dvfs_dir)
            sram7_times = active_times_from_dir(sram7_dir)
            mram14_times = active_times_from_dir(mram14_dir)

            stage6 = stage6_metrics_from_run(dvfs_dir)

            row = {
                "workload": bench,
                "workload_short": shorten_workload(bench),
                "n_tag": "n1",
                "n_cores": 1,
                "size_mb": size_mb,
                "variant": "selectiveDVFS",
                "dvfs_run_dir": dvfs_dir,
                "sram7_run_dir": sram7_dir,
                "mram14_run_dir": mram14_dir,

                "dvfs_makespan_s": makespan_s(dvfs_dir),
                "sram7_makespan_s": makespan_s(sram7_dir),
                "mram14_makespan_s": makespan_s(mram14_dir),

                "dvfs_times_s": fmt_time_vec(dvfs_times),
                "sram7_times_s": fmt_time_vec(sram7_times),
                "mram14_times_s": fmt_time_vec(mram14_times),

                "speedup_vs_sram7_ratio": single_speedup_ratio(sram7_dir, dvfs_dir),
                "speedup_vs_mram14_ratio": single_speedup_ratio(mram14_dir, dvfs_dir),

                "wsn_vs_sram7": None,
                "wsn_vs_mram14": None,
                "ws_terms_vs_sram7": None,
                "ws_terms_vs_mram14": None,
            }
            row.update(stage6)
            rows.append(row)
    return rows

def build_multicore_rows():
    rows = []
    workloads_by_n = [("n4", WORKLOADS_N4), ("n8", WORKLOADS_N8)]

    for n_tag, workloads in workloads_by_n:
        n_cores = int(n_tag[1:])
        for bench in workloads:
            for size_mb in SIZES:
                dvfs_dir = find_smart_ttl_dir(bench, n_tag, size_mb)
                sram7_dir = find_sram7_baseline(bench, n_tag, size_mb)
                mram14_dir = find_mram14_baseline(bench, n_tag, size_mb)

                dvfs_times = active_times_from_dir(dvfs_dir)
                sram7_times = active_times_from_dir(sram7_dir)
                mram14_times = active_times_from_dir(mram14_dir)

                ws_s7 = per_core_weighted_speedups(sram7_dir, dvfs_dir)
                ws_m14 = per_core_weighted_speedups(mram14_dir, dvfs_dir)

                stage6 = stage6_metrics_from_run(dvfs_dir)

                row = {
                    "workload": bench,
                    "workload_short": shorten_workload(bench),
                    "n_tag": n_tag,
                    "n_cores": n_cores,
                    "size_mb": size_mb,
                    "variant": "smartDVFS+TTL",
                    "dvfs_run_dir": dvfs_dir,
                    "sram7_run_dir": sram7_dir,
                    "mram14_run_dir": mram14_dir,

                    "dvfs_makespan_s": makespan_s(dvfs_dir),
                    "sram7_makespan_s": makespan_s(sram7_dir),
                    "mram14_makespan_s": makespan_s(mram14_dir),

                    "dvfs_times_s": fmt_time_vec(dvfs_times),
                    "sram7_times_s": fmt_time_vec(sram7_times),
                    "mram14_times_s": fmt_time_vec(mram14_times),

                    "speedup_vs_sram7_ratio": single_speedup_ratio(sram7_dir, dvfs_dir),
                    "speedup_vs_mram14_ratio": single_speedup_ratio(mram14_dir, dvfs_dir),

                    "wsn_vs_sram7": normalized_weighted_speedup(sram7_dir, dvfs_dir),
                    "wsn_vs_mram14": normalized_weighted_speedup(mram14_dir, dvfs_dir),
                    "ws_terms_vs_sram7": "|".join(f"{x:.9f}" for x in ws_s7) if ws_s7 else None,
                    "ws_terms_vs_mram14": "|".join(f"{x:.9f}" for x in ws_m14) if ws_m14 else None,
                }
                row.update(stage6)
                rows.append(row)

    n_order = {"n1": 1, "n4": 4, "n8": 8}
    rows.sort(key=lambda r: (n_order[r["n_tag"]], r["size_mb"], r["workload"]))
    return rows

# -----------------------------------------------------------------------------
# CSV writing
# -----------------------------------------------------------------------------
CSV_COLUMNS = [
    "workload",
    "workload_short",
    "n_tag",
    "n_cores",
    "size_mb",
    "variant",

    "dvfs_run_dir",
    "sram7_run_dir",
    "mram14_run_dir",

    "dvfs_makespan_s",
    "sram7_makespan_s",
    "mram14_makespan_s",

    "dvfs_times_s",
    "sram7_times_s",
    "mram14_times_s",

    "speedup_vs_sram7_ratio",
    "speedup_vs_mram14_ratio",

    "wsn_vs_sram7",
    "wsn_vs_mram14",
    "ws_terms_vs_sram7",
    "ws_terms_vs_mram14",

    # stage6 table 1
    "t1_num_intervals",
    "t1_num_transitions",
    "t1_transition_rate",
    "t1_avg_freq_ghz",
    "t1_max_freq_ghz",
    "t1_residency_bins",

    # stage6 table 2
    "t2_cap_w",
    "t2_avg_power_w",
    "t2_avg_slack_w",
    "t2_pct_over",
    "t2_pct_band",
    "t2_pct_room",

    # stage6 table 3
    "t3_avg_util",
    "t3_std_util",
    "t3_top1_minus_top2_util",
    "t3_avg_ipc",
    "t3_std_ipc",
    "t3_instr_m_per_core",

    # stage6 table 4
    "t4_mpki",
    "t4_mem_cpi_frac",
    "t4_ll_stall_frac",
    "t4_dvfs_ovh_frac",
    "t4_mpki_per_core",
]


def write_csv(csv_path: str, rows):
    parent = os.path.dirname(csv_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", type=str, default=DEFAULT_BASE,
                    help="Base repro dir (default: ~/COSC_498/miniMXE/repro)")
    ap.add_argument("--out", type=str, default=DEFAULT_OUT,
                    help="Output CSV path (default: ~/COSC_498/miniMXE/repro/agg/dvfs.csv)")
    args = ap.parse_args()

    base = os.path.expanduser(args.base)
    out = os.path.expanduser(args.out)
    init_paths(base)

    rows = []
    rows.extend(build_n1_rows())
    rows.extend(build_multicore_rows())

    n_order = {"n1": 1, "n4": 4, "n8": 8}
    rows.sort(key=lambda r: (n_order[r["n_tag"]], r["size_mb"], r["workload"]))

    write_csv(out, rows)
    print(f"[wrote csv] {out}")
    print(f"[rows] {len(rows)}")


if __name__ == "__main__":
    main()