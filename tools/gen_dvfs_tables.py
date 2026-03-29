#!/usr/bin/env python3
"""
gen_dvfs_tables.py — Print paper tables from DVFS + HCA simulation results.

Usage:
    cd /home/skataoka26/COSC_498/miniMXE/repro
    python3 ../mx3/tools/gen_dvfs_tables.py

    # Or specify base dir explicitly:
    python3 mx3/tools/gen_dvfs_tables.py --base repro
"""
import argparse
import math
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=".",
                    help="Base repro directory (default: current dir). "
                         "Should contain hca/ and dvfs/ subdirs.")
    ap.add_argument("--tables", default="1,2,3,4,5,6,7",
                    help="Comma-separated table numbers to print (default: all)")
    return ap.parse_args()


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------
def _get_stat(db_path: str, obj: str, metric: str,
              prefix: str = "roi-end", core: int = 0):
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
    """Return number of simulated cores from the DB."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        c = conn.cursor()
        c.execute('SELECT MAX(v.core) FROM "values" v')
        row = c.fetchone()
        conn.close()
        return int(row[0]) + 1 if row and row[0] is not None else 1
    except Exception:
        return 1


# ---------------------------------------------------------------------------
# Time extraction
# ---------------------------------------------------------------------------
def _parse_sim_out_times(path: str):
    """Parse begin/end elapsed_time per core from sim.out text file."""
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
        # Values are in fs; convert to seconds
        return [(e - b) * 1e-15 for b, e in zip(begins, ends)]
    if elapsed:
        return [e * 1e-15 for e in elapsed]
    return None


def get_times_from_dir(root: str):
    """
    Return list of per-core elapsed times (in seconds).
    Single-core → list of length 1.
    Multi-core  → list of length N (one per core).

    Tries sim.out text first, then falls back to SQLite.
    """
    # 1. sim.out text parsing
    times = _parse_sim_out_times(os.path.join(root, "sim.out"))
    if times:
        return times

    # 2. SQLite fallback — query each core individually
    db_path = os.path.join(root, "sim.stats.sqlite3")
    if not os.path.exists(db_path):
        return None

    n_cores = get_num_cores(db_path)
    result = []
    for core in range(n_cores):
        t = get_delta(db_path, "thread", "elapsed_time", core)
        if t is None or t <= 0:
            t = get_delta(db_path, "performance_model", "elapsed_time", core)
        if t and t > 0:
            # fs → seconds
            result.append(t * 1e-15)
        else:
            result.append(None)

    if any(v is not None and v > 0 for v in result):
        return result
    return None


# ---------------------------------------------------------------------------
# Extract HCA metrics from SQLite
# ---------------------------------------------------------------------------
def extract_hca_metrics(db_path: str) -> dict:
    misses = get_delta(db_path, "L3", "load-misses")
    rh_sram = get_delta(db_path, "L3", "l3_read_hits_sram")
    rh_mram = get_delta(db_path, "L3", "l3_read_hits_mram")
    wh_sram = get_delta(db_path, "L3", "l3_write_hits_sram")
    wh_mram = get_delta(db_path, "L3", "l3_write_hits_mram")
    mwb = get_delta(db_path, "L3", "mram_write_bytes")
    promo = get_delta(db_path, "L3", "hybrid_promotions")
    inst = get_delta(db_path, "performance_model", "instruction_count")

    thread_fs = get_delta(db_path, "thread", "elapsed_time")
    perf_fs = get_delta(db_path, "performance_model", "elapsed_time")
    fs = thread_fs if (thread_fs and thread_fs > 0) else perf_fs
    elapsed_ns = fs / 1e6 if fs else None  # fs → ns

    # Note: throughput = inst / elapsed_ns (inst/ns), NOT classical IPC (inst/cycle)
    throughput = inst / elapsed_ns if (inst and elapsed_ns) else 0

    return {
        "elapsed_ns": elapsed_ns,
        "throughput_inst_ns": throughput,
        "l3_load_misses": misses or 0,
        "l3_rh": (rh_sram or 0) + (rh_mram or 0),
        "l3_wh": (wh_sram or 0) + (wh_mram or 0),
        "mram_write_bytes": mwb or 0,
        "hybrid_promotions": promo or 0,
    }


# ---------------------------------------------------------------------------
# Variant label parsers
# ---------------------------------------------------------------------------
WKLDS_ALL = [
    "mcf", "xalancbmk", "perlbench", "omnetpp", "gcc", "xz",
    "deepsjeng", "leela", "fotonik3d", "exchange2",
]
WKLDS_REP = ["perlbench", "mcf", "omnetpp", "deepsjeng"]


def short_wl(name: str) -> str:
    parts = name.split("_")
    for p in parts:
        if p in WKLDS_ALL:
            return p
    return parts[1] if len(parts) >= 2 else name


def get_hca_config(vd: str):
    vd = vd.replace("_sram14_mram14", "").replace("_sram14", "").replace("_mram14", "")
    mapping = {
        "mram14": "MRAM",
        "baseline_mram_only_mram14": "MRAM",
        "baseline_mram_only": "MRAM",
        "noparity_s4_fillmram": "S4_UNR",
        "noparity_s4_fillmram_p4_c32": "P4C32_UNR",
        "noparity_s4_fillmram_p1_c0": "P1C0_UNR",
        "noparity_s4_fillmram_rf": "S4_RF",
        "noparity_s4_fillmram_rf_p4_c32": "P4C32_RF",
        "noparity_s4_fillmram_rf_p1_c0": "P1C0_RF",
    }
    return mapping.get(vd)


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------
def collect_data(base: str):
    """
    Returns:
        data    : {(bench_str, size_mb, label) -> time_s}   for n=1 runs
        mc_data : {(bench_str, size_mb, label, 'nN') -> [time_s per core]}
        hca_data: {(short_wl, size_mb, cfg_str) -> metrics_dict}
    """
    data = {}
    mc_data = {}
    hca_data = {}

    hca_root = os.path.join(base, "hca", "hca_sunnycove")
    dvfs_root = os.path.join(base, "dvfs")

    # ------------------------------------------------------------------ #
    # 1) HCA baselines + static HCA (old-style path layout)               #
    # ------------------------------------------------------------------ #
    for campaign in ["1_baselines", "2_cross_node/mram14", "2_cross_node/mram32", "3_static_hca"]:
        runs_dir = os.path.join(hca_root, campaign, "runs")
        if not os.path.isdir(runs_dir):
            continue
        for root, _, files in os.walk(runs_dir):
            if "sim.out" not in files:
                continue
            parts = root.split(os.sep)
            vd = parts[-1]
            bench_raw = size = None
            for p in parts:
                if p.startswith("sz") and p.endswith("M"):
                    size = int(p[2:-1])
                if "roi" in p and p[0].isdigit():
                    bench_raw = p.split("_roi")[0]
            if not bench_raw or not size:
                continue

            times = get_times_from_dir(root)
            if not times or times[0] is None:
                continue
            time_s = times[0]  # single-core

            label = None
            if "baseline_sram_only_sram7" in vd:
                label = "sram7"
            elif "baseline_sram_only_sram14" in vd:
                label = "hca_sram14"
            elif "baseline_mram_only_mram14" in vd:
                label = "mram14"
            elif vd.startswith("noparity_s4_fillmram") and not vd.startswith("noparity_s4_fillmram_"):
                label = "hca_s4"
            elif vd.startswith("noparity_s8_fillmram"):
                label = "hca_s8"
            elif vd.startswith("noparity_s12_fillmram"):
                label = "hca_s12"
            if label:
                data[(bench_raw, size, label)] = time_s

    # ------------------------------------------------------------------ #
    # 2) DVFS main (single + multicore)                                    #
    # ------------------------------------------------------------------ #
    for stage_name, label_map in [
        ("1_main_dvfs", {
            "baseline_sram_only": "dvfs_sram14",
            "lc_mram14": "mram_dvfs",
        }),
        ("1b_counterfactual", {
            "sram_lc_mram14": "cf",
        }),
    ]:
        stage_dir = os.path.join(dvfs_root, stage_name, "runs")
        if not os.path.isdir(stage_dir):
            continue
        for root, _, files in os.walk(stage_dir):
            if "sim.out" not in files:
                continue

            parts = root.split(os.sep)
            vd = parts[-1]
            n_val = size = bench = None

            for p in parts:
                if p in ("n1", "n2", "n4", "n8"):
                    n_val = p
                if p.startswith("l3_") and p.endswith("MB"):
                    try:
                        size = int(p[3:-2])
                    except ValueError:
                        pass
                # bench dir: either "NNN.name_r" or multi "NNN.name+NNN.name+..."
                if re.match(r"\d+\.\w", p) or "+" in p:
                    bench = p

            if not n_val or not size or not bench:
                continue

            # Determine label
            label = None
            if "baseline_sram_only" in vd:
                label = "dvfs_sram14"
            elif vd.startswith("lc_") and "mram14" in vd:
                label = "mram_dvfs"
            elif vd.startswith("sram_lc") and "mram14" in vd:
                label = "cf"
            if not label:
                continue

            times = get_times_from_dir(root)
            if not times or all(t is None for t in times):
                continue

            if n_val == "n1":
                # Normalise key: "500.perlbench_r" → "500_perlbench_r"
                bench_key = bench.replace(".", "_", 1)
                data[(bench_key, size, label)] = times[0]
            else:
                mc_data[(bench, size, label, n_val)] = times

    # ------------------------------------------------------------------ #
    # 3) HCA detail metrics from sqlite (Tables 4-6)                      #
    # ------------------------------------------------------------------ #
    for root, _, files in os.walk(hca_root):
        if "sim.stats.sqlite3" not in files:
            continue
        parts = root.split(os.sep)
        vd = parts[-1]
        bench_raw = size = None
        for p in parts:
            if p.startswith("sz") and p.endswith("M"):
                try:
                    size = int(p[2:-1])
                except ValueError:
                    pass
            if "roi" in p or re.match(r"\d+\.\w", p):
                bench_raw = p.split("_roi")[0].replace(".", "_", 1)
        if not bench_raw or not size:
            continue
        wl = short_wl(bench_raw)
        cfg = get_hca_config(vd)
        if not cfg:
            continue
        db_path = os.path.join(root, "sim.stats.sqlite3")
        hca_data[(wl, size, cfg)] = extract_hca_metrics(db_path)

    return data, mc_data, hca_data


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def speedup_str(base_t, new_t) -> str:
    if base_t and new_t and new_t > 0:
        return f"{(base_t / new_t - 1) * 100:>+7.1f}%"
    return f"{'—':>8}"


def mc_speedup_str(base_times, new_times) -> str:
    if not base_times or not new_times:
        return f"{'—':>9}"
    pairs = [(b, n) for b, n in zip(base_times, new_times)
             if b is not None and n is not None and n > 0]
    if not pairs:
        return f"{'—':>9}"
    avg = sum((b / n - 1) for b, n in pairs) / len(pairs) * 100
    return f"{avg:>+8.1f}%"


def geomean(vals):
    vals = [v for v in vals if v and v > 0]
    if not vals:
        return None
    return math.exp(sum(math.log(v) for v in vals) / len(vals))


# ---------------------------------------------------------------------------
# Table printers
# ---------------------------------------------------------------------------
def print_table1(data, sizes):
    benches = sorted(set(k[0] for k in data if k[2] == "dvfs_sram14"))
    print("=" * 75)
    print("TABLE 1: No DVFS, No HCA — Raw Time (ms)")
    print("=" * 75)
    print(f"{'Bench':<20} {'MB':>3} {'SRAM14':>8} {'SRAM7':>8} {'MRAM14':>8} {'MRAM/SRAM7':>11}")
    print("-" * 65)
    for b in benches:
        for sz in sizes:
            s14 = data.get((b, sz, "dvfs_sram14"))
            s7  = data.get((b, sz, "sram7"))
            m14 = data.get((b, sz, "mram14"))
            print(f"{b:<20} {sz:>3}"
                  f" {(s14*1e3 if s14 else 0):>8.2f}"
                  f" {(s7*1e3  if s7  else 0):>8.2f}"
                  f" {(m14*1e3 if m14 else 0):>8.2f}"
                  f" {speedup_str(s7, m14):>11}")
    print()


def print_table2(data, sizes):
    benches = sorted(set(k[0] for k in data if k[2] == "dvfs_sram14"))
    print("=" * 90)
    print("TABLE 2: HCA Static Partitioning (sram14+mram14) — Time (ms) & Speedup vs MRAM14")
    print("=" * 90)
    print(f"{'Bench':<20} {'MB':>3} {'MRAM14':>8} {'s=4':>8} {'s=8':>8} {'s=12':>8}"
          f" {'s4/M':>8} {'s8/M':>8} {'s12/M':>8}")
    print("-" * 90)
    for b in benches:
        for sz in sizes:
            m14 = data.get((b, sz, "mram14"))
            s4  = data.get((b, sz, "hca_s4"))
            s8  = data.get((b, sz, "hca_s8"))
            s12 = data.get((b, sz, "hca_s12"))
            print(f"{b:<20} {sz:>3}"
                  f" {(m14*1e3 if m14 else 0):>8.2f}"
                  f" {(s4*1e3  if s4  else 0):>8.2f}"
                  f" {(s8*1e3  if s8  else 0):>8.2f}"
                  f" {(s12*1e3 if s12 else 0):>8.2f}"
                  f" {speedup_str(m14, s4):>8}"
                  f" {speedup_str(m14, s8):>8}"
                  f" {speedup_str(m14, s12):>8}")
    print()


def print_table3(data, sizes):
    benches = sorted(set(k[0] for k in data if k[2] == "dvfs_sram14"))
    W = 100
    print("=" * W)
    print("TABLE 3: MRAM+DVFS vs baselines (and Counterfactual) — Time (ms)")
    print("  Spd_MRAM = MRAM_base / MRAM+DVFS (pure DVFS gain on top of cache)")
    print("  Spd_DVFS = SRAM14   / MRAM+DVFS (total)  |  gap = Spd_DVFS - Spd_CF")
    print("=" * W)
    print(f"{'Bench':<20} {'MB':>3} {'SRAM14':>8} {'MRAM_b':>8} {'M+DVFS':>8} {'CF':>8}"
          f" {'Spd_MRAM':>9} {'Spd_DVFS':>9} {'Spd_CF':>9} {'gap':>8}")
    print("-" * W)
    gm_m = {s: [] for s in sizes}   # MRAM_base/MRAM+DVFS
    gm_d = {s: [] for s in sizes}   # S14/MRAM+DVFS
    gm_c = {s: [] for s in sizes}   # S14/CF
    for b in benches:
        for sz in sizes:
            s14 = data.get((b, sz, "dvfs_sram14"))
            mb  = data.get((b, sz, "mram14"))       # MRAM baseline, no DVFS
            md  = data.get((b, sz, "mram_dvfs"))
            cf  = data.get((b, sz, "cf"))
            gap_s = "—"
            if md and cf and s14 and md > 0 and cf > 0:
                gap_v = ((s14 / md) - (s14 / cf)) * 100
                gap_s = f"{gap_v:>+7.1f}pp"
            if mb and md and mb > 0 and md > 0:
                gm_m[sz].append(mb / md)
            if md and s14 and md > 0:
                gm_d[sz].append(s14 / md)
            if cf and s14 and cf > 0:
                gm_c[sz].append(s14 / cf)
            print(f"{b:<20} {sz:>3}"
                  f" {(s14*1e3 if s14 else 0):>8.2f}"
                  f" {(mb*1e3  if mb  else 0):>8.2f}"
                  f" {(md*1e3  if md  else 0):>8.2f}"
                  f" {(cf*1e3  if cf  else 0):>8.2f}"
                  f" {speedup_str(mb, md):>9}"
                  f" {speedup_str(s14, md):>9}"
                  f" {speedup_str(s14, cf):>9}"
                  f" {gap_s:>8}")
    print("-" * W)
    for sz in sizes:
        mg_v = geomean(gm_m[sz])
        dg_v = geomean(gm_d[sz])
        cg_v = geomean(gm_c[sz])
        mg = f"{(mg_v - 1) * 100:>+7.1f}%" if mg_v else "—"
        dg = f"{(dg_v - 1) * 100:>+7.1f}%" if dg_v else "—"
        cg = f"{(cg_v - 1) * 100:>+7.1f}%" if cg_v else "—"
        print(f"{'Geomean':<20} {sz:>3} {'':>8} {'':>8} {'':>8} {'':>8} {mg:>9} {dg:>9} {cg:>9}")
    print()


def print_table4(hca_data):
    print("=" * 80)
    print("TABLE 4: Workload behavior at 16 MB all-MRAM design point")
    print("  (throughput = inst/ns; miss rate = load-misses/(read_hits+load-misses))")
    print("=" * 80)
    print(f"{'Workload':<15} {'inst/ns':>8} {'L3 Rd Hits':>13} {'L3 Wr Hits':>13}"
          f" {'Miss%':>7} {'Wr% Hits':>10}")
    print("-" * 80)
    for wl in WKLDS_ALL:
        m = hca_data.get((wl, 16, "MRAM"))
        if not m:
            continue
        rh, wh = m["l3_rh"], m["l3_wh"]
        tot = rh + wh
        miss = m["l3_load_misses"]
        m_rate = miss / (rh + miss) * 100 if (rh + miss) > 0 else 0
        w_pct = wh / tot * 100 if tot > 0 else 0
        print(f"{wl:<15} {m['throughput_inst_ns']:>8.2f}"
              f" {rh:>13,.0f} {wh:>13,.0f}"
              f" {m_rate:>6.1f}% {w_pct:>9.1f}%")
    print()


def print_table5(hca_data):
    CONFIGS_UNR = [("MRAM", "MRAM"), ("S4", "S4_UNR"),
                   ("P4C32", "P4C32_UNR"), ("P1C0", "P1C0_UNR")]
    print("=" * 80)
    print("TABLE 5: Representative 16 MB workloads under unrestricted fill")
    print("=" * 80)
    print(f"{'Workload':<15} {'Cfg':<10} {'Perf':>8} {'L3 miss':>10}"
          f" {'MRAM wr':>10} {'Promo':>12}")
    print("-" * 80)
    for wl in WKLDS_REP:
        base = hca_data.get((wl, 16, "MRAM"))
        if not base:
            continue
        b_ns, b_miss, b_mwb = (base["elapsed_ns"], base["l3_load_misses"],
                                base["mram_write_bytes"])
        for label, cfg in CONFIGS_UNR:
            r = hca_data.get((wl, 16, cfg))
            if not r:
                continue
            spd = b_ns / r["elapsed_ns"] if r["elapsed_ns"] else 0
            miss_n = r["l3_load_misses"] / b_miss if b_miss else 0
            mw_n = r["mram_write_bytes"] / b_mwb if b_mwb else 0
            promo = int(r["hybrid_promotions"])
            wl_str = wl if label == "MRAM" else ""
            print(f"{wl_str:<15} {label:<10} {spd:>8.3f} {miss_n:>10.3f}"
                  f" {mw_n:>10.3f} {promo:>12,}")
        print("-" * 80)
    print()


def print_table6(hca_data):
    print("=" * 110)
    print("TABLE 6: Restricted-fill migration summary (16/32/128 MB)")
    print("=" * 110)
    hdr1 = f"{'Workload':<12} | {'16 MB':^30} | {'32 MB':^30} | {'128 MB':^22}"
    hdr2 = (f"{'':<12} | {'S4-RF':>7} {'P4C32/S':>8} {'P1C0/S':>8} {'P1C0 mr':>7} |"
            f" {'S4-RF':>7} {'P4C32/S':>8} {'P1C0/S':>8} {'P1C0 mr':>7} |"
            f" {'S4-RF':>7} {'P4C32/S':>8} {'P1C0/S':>8}")
    print(hdr1)
    print(hdr2)
    print("-" * 110)
    for wl in WKLDS_REP:
        line = f"{wl:<12} |"
        for sz in [16, 32, 128]:
            base = hca_data.get((wl, sz, "MRAM"))
            st = hca_data.get((wl, sz, "S4_RF"))
            m4 = hca_data.get((wl, sz, "P4C32_RF"))
            m1 = hca_data.get((wl, sz, "P1C0_RF"))
            if not all([base, st, m4, m1]):
                if sz != 128:
                    line += f" {'—':>7} {'—':>8} {'—':>8} {'—':>7} |"
                else:
                    line += f" {'—':>7} {'—':>8} {'—':>8}"
                continue
            b_ns, st_ns = base["elapsed_ns"], st["elapsed_ns"]
            m4_ns, m1_ns = m4["elapsed_ns"], m1["elapsed_ns"]
            s4rf = f"{b_ns / st_ns:.3f}" if st_ns else "—"
            p4c  = f"{st_ns / m4_ns:.3f}" if m4_ns else "—"
            p1c  = f"{st_ns / m1_ns:.3f}" if m1_ns else "—"
            if sz != 128:
                mr = (m1["l3_load_misses"] / st["l3_load_misses"]
                      if st["l3_load_misses"] else 0)
                line += f" {s4rf:>7} {p4c:>8} {p1c:>8} {mr:.2f}x{' ':>1} |"
            else:
                line += f" {s4rf:>7} {p4c:>8} {p1c:>8}"
        print(line)
    print()


def print_table7(mc_data, sizes):
    mc_benches = sorted({k[0] for k in mc_data if k[2] == "dvfs_sram14"})
    if not mc_benches:
        print("TABLE 7: No multicore data found.")
        return
    W = max(len(b) for b in mc_benches)
    print("=" * (W + 75))
    print("TABLE 7: Multi-Core DVFS vs SRAM14 baseline (makespan ms, per-core avg speedup)")
    print("  Spd_DVFS/Spd_CF = per-core average speedup  |  gap = Spd_DVFS - Spd_CF")
    print("=" * (W + 75))
    hdr = (f"{'Workload':<{W}} {'Cores':>5} {'MB':>3} {'SRAM14':>8}"
           f" {'M+DVFS':>8} {'CF':>8} {'Spd_DVFS':>10} {'Spd_CF':>10} {'gap':>9}")
    print(hdr)
    print("-" * len(hdr))
    mc_keys = sorted({(k[0], k[3]) for k in mc_data if k[2] == "dvfs_sram14"},
                     key=lambda x: (x[1], x[0]))

    # Accumulate geomean speedup ratios per (n_val, sz)
    gm_d = {}   # (n_val, sz) -> list of per-core avg speedup ratios
    gm_c = {}
    gm_gap = {}

    for b, n_val in mc_keys:
        for sz in sizes:
            s14_t = mc_data.get((b, sz, "dvfs_sram14", n_val))
            md_t  = mc_data.get((b, sz, "mram_dvfs",   n_val))
            cf_t  = mc_data.get((b, sz, "cf",           n_val))
            if not s14_t:
                continue

            def ms_max(ts): return max(t for t in ts if t) * 1e3 if ts else 0.0

            s14_ms = ms_max(s14_t)
            md_ms  = ms_max(md_t) if md_t else 0.0
            cf_ms  = ms_max(cf_t) if cf_t else 0.0

            d_str = mc_speedup_str(s14_t, md_t)
            c_str = mc_speedup_str(s14_t, cf_t)

            gap_s = "—"
            avg_d = avg_c = None
            if md_t and cf_t and s14_t:
                pairs_d = [(b2, n) for b2, n in zip(s14_t, md_t) if b2 and n and n > 0]
                pairs_c = [(b2, n) for b2, n in zip(s14_t, cf_t) if b2 and n and n > 0]
                if pairs_d and pairs_c and len(pairs_d) == len(pairs_c):
                    avg_d = sum(b2/n for b2, n in pairs_d) / len(pairs_d)  # ratio
                    avg_c = sum(b2/n for b2, n in pairs_c) / len(pairs_c)
                    gap_s = f"{(avg_d - avg_c) * 100:>+8.1f}pp"
            if md_t and s14_t:
                pairs_d2 = [(b2, n) for b2, n in zip(s14_t, md_t) if b2 and n and n > 0]
                if pairs_d2:
                    avg_d2 = sum(b2/n for b2, n in pairs_d2) / len(pairs_d2)
                    gm_d.setdefault((n_val, sz), []).append(avg_d2)
            if cf_t and s14_t:
                pairs_c2 = [(b2, n) for b2, n in zip(s14_t, cf_t) if b2 and n and n > 0]
                if pairs_c2:
                    avg_c2 = sum(b2/n for b2, n in pairs_c2) / len(pairs_c2)
                    gm_c.setdefault((n_val, sz), []).append(avg_c2)
            if avg_d is not None and avg_c is not None:
                gm_gap.setdefault((n_val, sz), []).append(avg_d - avg_c)

            print(f"{b:<{W}} {n_val:>5} {sz:>3}"
                  f" {s14_ms:>8.2f} {md_ms:>8.2f} {cf_ms:>8.2f}"
                  f" {d_str:>10} {c_str:>10} {gap_s:>9}")

    # Geomean rows per (n_val, sz)
    print("-" * len(hdr))
    for n_val in ["n4", "n8"]:
        for sz in sizes:
            gd_list = gm_d.get((n_val, sz), [])
            gc_list = gm_c.get((n_val, sz), [])
            gg_list = gm_gap.get((n_val, sz), [])
            gd_v = geomean(gd_list)
            gc_v = geomean(gc_list)
            gg_v = sum(gg_list) / len(gg_list) if gg_list else None  # arithmetic mean for pp gaps
            gd_s = f"{(gd_v - 1)*100:>+9.1f}%" if gd_v else f"{'—':>10}"
            gc_s = f"{(gc_v - 1)*100:>+9.1f}%" if gc_v else f"{'—':>10}"
            gg_s = f"{gg_v*100:>+8.1f}pp" if gg_v is not None else f"{'—':>9}"
            if gd_list:
                print(f"{'Geomean':<{W}} {n_val:>5} {sz:>3}"
                      f" {'':>8} {'':>8} {'':>8}"
                      f" {gd_s:>10} {gc_s:>10} {gg_s:>9}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
# Table 8 helpers: energy decomposition
# ---------------------------------------------------------------------------
def load_oracle_noncache(base: str, cap_mb: int = 128, base_freq: float = 2.2,
                         ncores: int = 1) -> dict:
    """
    Read oracle_points.csv and return {bench -> (P_noncache_W, P_static_W)}
    for the specified capacity/freq/ncores.
    P_noncache_W = y_PminusLLC (total power minus LLC leak, at base freq)
    P_static_W  = 20.08 (fixed from params)
    """
    csv_path = os.path.join(base, "calibration",
                            "plm_calib_sunnycove", "runs", "oracle_points.csv")
    if not os.path.exists(csv_path):
        return {}
    result = {}
    n_tag = f"/n{ncores}/"
    with open(csv_path) as f:
        header = f.readline().strip().split(",")
        idx = {h: i for i, h in enumerate(header)}
        for line in f:
            parts = line.strip().split(",")
            if len(parts) < len(idx):
                continue
            if n_tag not in parts[idx["run_dir"]]:
                continue
            try:
                sz = int(float(parts[idx["size_mb"]]))
                fg = float(parts[idx["f_ghz"]])
            except ValueError:
                continue
            if sz != cap_mb or abs(fg - base_freq) > 0.05:
                continue
            bench = parts[idx["bench"]]
            try:
                p_noncache = float(parts[idx["y_PminusLLC"]])
            except (ValueError, KeyError):
                continue
            result[bench] = p_noncache  # already includes static
    return result


def _find_hca_rundir(hca_root: str, bench_long: str, size_mb: int,
                     variant_substr: str) -> Optional[str]:
    """
    Search hca_sunnycove campaigns for a run dir matching bench, size, and
    variant substring. Returns the first matching leaf dir that has sim.stats.sqlite3.
    HCA bench dirs use underscores: 500_perlbench_r_roi1000M_warm200M
    """
    sz_tag = f"sz{size_mb}M"
    # bench_long e.g. "500.perlbench_r" → prefix with either . or _
    bench_prefix_dot = bench_long                          # "500.perlbench_r"
    bench_prefix_us  = bench_long.replace(".", "_", 1)    # "500_perlbench_r"
    for campaign in ["1_baselines", "2_cross_node/mram14", "2_cross_node/mram32",
                     "3_static_hca"]:
        runs_dir = os.path.join(hca_root, campaign, "runs")
        if not os.path.isdir(runs_dir):
            continue
        for root, _, files in os.walk(runs_dir):
            if "sim.stats.sqlite3" not in files:
                continue
            parts = root.split(os.sep)
            vd = parts[-1]
            if variant_substr not in vd:
                continue
            if sz_tag not in parts:
                continue
            # bench match: any path segment starts with either prefix form
            if not any(p.startswith(bench_prefix_dot) or p.startswith(bench_prefix_us)
                       for p in parts):
                continue
            return root
    return None


def _energy_row(run_dir: str, rd_metric: str, wr_metric: str,
                r_pj: float, w_pj: float, leak_mw: float,
                p_noncache: float, P_STATIC_W: float) -> Optional[dict]:
    """Compute energy components for one run dir."""
    db_path = os.path.join(run_dir, "sim.stats.sqlite3")
    if not os.path.exists(db_path):
        return None
    elapsed_s_list = get_times_from_dir(run_dir)
    if not elapsed_s_list or elapsed_s_list[0] is None:
        return None
    elapsed_s = elapsed_s_list[0]
    rd = get_delta(db_path, "L3", rd_metric) or 0
    wr = get_delta(db_path, "L3", wr_metric) or 0
    e_llc_dyn   = (rd * r_pj + wr * w_pj) * 1e-12
    e_llc_leak  = (leak_mw * 1e-3) * elapsed_s
    e_nc_static = P_STATIC_W * elapsed_s
    p_nc_dyn    = max(0.0, p_noncache - P_STATIC_W)
    e_nc_dyn    = p_nc_dyn * elapsed_s
    e_total     = e_llc_dyn + e_llc_leak + e_nc_static + e_nc_dyn
    return {"elapsed_ms": elapsed_s * 1e3,
            "e_llc_dyn": e_llc_dyn, "e_llc_leak": e_llc_leak,
            "e_nc_static": e_nc_static, "e_nc_dyn": e_nc_dyn,
            "e_total": e_total}


def print_table8(data: dict, base: str):
    """
    Table 8: Energy decomposition at 128MB, n=1.

    Components:
      E_llc_dyn   = LLC reads x R_pJ + LLC writes x W_pJ  (per-device params)
      E_llc_leak  = device_leak_mW x elapsed_s
      E_nc_static = P_static (20.08W) x elapsed_s
      E_nc_dyn    = (P_noncache_oracle - P_static) x elapsed_s   [same for all configs]
    Three configs compared: SRAM7, MRAM14 (no DVFS), MRAM14+DVFS.
    """
    CAP_MB = 128
    P_STATIC_W = 20.08

    # Device params at 128MB
    CFGS = [
        # name, rd_metric, wr_metric, r_pj, w_pj, leak_mw, find_dir_fn
        ("SRAM7",   "l3_read_hits_sram", "l3_write_hits_sram",  727.0,  694.0, 1000.2, "sram7"),
        ("MRAM14",  "l3_read_hits_mram", "l3_write_hits_mram",  685.0,  668.0,  204.8, "mram14"),
        ("M+DVFS",  "l3_read_hits_mram", "l3_write_hits_mram",  685.0,  668.0,  204.8, "dvfs"),
    ]

    oracle = load_oracle_noncache(base, cap_mb=CAP_MB, ncores=1)
    if not oracle:
        print("TABLE 8: oracle_points.csv not found — skipping.")
        return

    hca_root = os.path.join(base, "hca", "hca_sunnycove")
    dvfs_dir = os.path.join(base, "dvfs", "1_main_dvfs", "runs")

    W = 95
    print("=" * W)
    print("TABLE 8: Energy decomposition — n=1, 128MB (Joules per workload run)")
    print("  E_llc_dyn   = LLC accesses x pJ/access  |  E_llc_leak = leak_mW x elapsed")
    print("  E_nc_static = 20.08W x elapsed           |  E_nc_dyn   = oracle_dyn_power x elapsed")
    print("=" * W)
    print(f"{'Bench':<12} {'Config':<9} {'elapsed':>7}"
          f" {'E_llc_dyn':>10} {'E_llc_leak':>11}"
          f" {'E_nc_stat':>10} {'E_nc_dyn':>10} {'E_total':>10}"
          f" {'LLC_dyn%':>8} {'LLC_lk%':>8}")
    print("-" * W)

    for bench_long, p_noncache in sorted(oracle.items()):
        bench_short = bench_long.split(".")[1].split("_")[0]
        printed_bench = False

        for cfg_name, rd_m, wr_m, r_pj, w_pj, leak_mw, find_tag in CFGS:
            run_dir = None
            if find_tag == "dvfs":
                candidate = os.path.join(dvfs_dir, bench_long, "n1", "l3_128MB")
                if os.path.isdir(candidate):
                    for d in os.listdir(candidate):
                        if d.startswith("lc_c") and "mram14" in d:
                            run_dir = os.path.join(candidate, d)
                            break
            elif find_tag == "sram7":
                run_dir = _find_hca_rundir(hca_root, bench_long, CAP_MB,
                                           "baseline_sram_only_sram7")
            else:  # mram14
                run_dir = _find_hca_rundir(hca_root, bench_long, CAP_MB,
                                           "baseline_mram_only_mram14")

            if not run_dir:
                row_lab = bench_short if not printed_bench else ""
                print(f"{row_lab:<12} {cfg_name:<9} {'—':>7} {'—':>10} {'—':>11}"
                      f" {'—':>10} {'—':>10} {'—':>10} {'—':>8} {'—':>8}")
                printed_bench = True
                continue

            r = _energy_row(run_dir, rd_m, wr_m, r_pj, w_pj, leak_mw,
                            p_noncache, P_STATIC_W)
            if not r:
                row_lab = bench_short if not printed_bench else ""
                print(f"{row_lab:<12} {cfg_name:<9} {'—':>7} {'—':>10} {'—':>11}"
                      f" {'—':>10} {'—':>10} {'—':>10} {'—':>8} {'—':>8}")
                printed_bench = True
                continue

            t = r["e_total"]
            pct_d = r["e_llc_dyn"]  / t * 100 if t else 0
            pct_l = r["e_llc_leak"] / t * 100 if t else 0
            row_lab = bench_short if not printed_bench else ""
            print(f"{row_lab:<12} {cfg_name:<9} {r['elapsed_ms']:>7.1f}"
                  f" {r['e_llc_dyn']:>10.4f} {r['e_llc_leak']:>11.4f}"
                  f" {r['e_nc_static']:>10.4f} {r['e_nc_dyn']:>10.4f}"
                  f" {r['e_total']:>10.4f}"
                  f" {pct_d:>7.1f}% {pct_l:>7.1f}%")
            printed_bench = True

        print("-" * W)
    print()


def _get_dvfs_stats(db_path: str, n_cores: int,
                    pi_cycles: int = 2_000_000) -> Optional[dict]:
    """
    Return per-core governor stats from a sim.stats.sqlite3.
    Returns dict with lists indexed by core:
      n_trans[c]  = number of governor step events on core c
      elapsed_s[c] = elapsed time in seconds for core c
    """
    if not os.path.exists(db_path):
        return None
    elapsed, trans = {}, {}
    for metric, store in [("elapsed_time", elapsed), ("cpiSyncDvfsTransition", trans)]:
        obj = "performance_model" if metric == "elapsed_time" else "performance_model"
        if metric == "cpiSyncDvfsTransition":
            obj = "performance_model"
        for prefix in ["roi-begin", "roi-end"]:
            rows = _get_stat_multi(db_path, obj, metric, prefix, n_cores)
            for core, val in rows:
                store.setdefault(core, {})[prefix] = float(val)
    if not elapsed:
        return None
    result = {"n_trans": [], "elapsed_s": []}
    for c in range(n_cores):
        # elapsed_time in femtoseconds
        begin_e = elapsed.get(c, {}).get("roi-begin", 0)
        end_e   = elapsed.get(c, {}).get("roi-end", 0)
        elapsed_s = (end_e - begin_e) * 1e-15
        begin_t = trans.get(c, {}).get("roi-begin", 0)
        end_t   = trans.get(c, {}).get("roi-end", 0)
        trans_cyc = end_t - begin_t
        n_t = int(trans_cyc / pi_cycles) if trans_cyc > 0 else 0
        result["n_trans"].append(n_t)
        result["elapsed_s"].append(elapsed_s)
    return result


def _get_stat_multi(db_path: str, obj: str, metric: str,
                    prefix: str, n_cores: int) -> list:
    """Query (core, value) for all cores up to n_cores."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        c = conn.cursor()
        c.execute(
            '''SELECT v.core, v.value FROM "values" v
               JOIN names n ON v.nameid=n.nameid
               JOIN prefixes p ON v.prefixid=p.prefixid
               WHERE n.objectname=? AND n.metricname=? AND p.prefixname=?
               AND v.core < ?''',
            (obj, metric, prefix, n_cores),
        )
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def print_table9(base: str, sizes=None):
    """
    Table 9: Governor controller behavior — MRAM+DVFS vs sram-DVFS (CF).
    Metrics per (workload, LLC size, core count):
      N_trans   = avg per-core # of governor step events
      f_eff_GHz = effective avg freq = f_base × (S14_elapsed / DVFS_elapsed)
    """
    if sizes is None:
        sizes = [16, 32, 128]
    F_BASE = 2.2   # GHz
    PI_CYC = 2_000_000

    dvfs_root = os.path.join(base, "dvfs", "1_main_dvfs", "runs")
    cf_root   = os.path.join(base, "dvfs", "1b_counterfactual", "runs")

    W = 100
    print("=" * W)
    print("TABLE 9: Governor controller behavior — MRAM+DVFS vs sram-DVFS (CF)")
    print(f"  N_trans = avg per-core governor step count  (pi=2M instr interval)")
    print(f"  f_eff   = f_base × (S14_elapsed / variant_elapsed), GHz  [proxy for avg boost freq]")
    print("=" * W)
    hdr = (f"{'Workload':<45} {'Cores':>5} {'MB':>3}"
           f" {'D_ntrans':>9} {'CF_ntrans':>10}"
           f" {'D_feff':>8} {'CF_feff':>8} {'Δf_eff':>8}")
    print(hdr)
    print("-" * W)

    for n_tag in ["n1", "n4", "n8"]:
        n_cores = int(n_tag[1:])
        # find all bench dirs at this n_cores
        n_path = os.path.join(dvfs_root)
        benches = sorted({
            entry for entry in os.listdir(dvfs_root)
            if os.path.isdir(os.path.join(dvfs_root, entry,
                                          n_tag))
        }) if os.path.isdir(dvfs_root) else []

        for bench in benches:
            for sz in sizes:
                sz_dir = os.path.join(dvfs_root, bench, n_tag, f"l3_{sz}MB")
                if not os.path.isdir(sz_dir):
                    continue

                # find variant dirs
                dvfs_dir = cf_dir = s14_dir = None
                for d in os.listdir(sz_dir):
                    full = os.path.join(sz_dir, d)
                    if d.startswith("lc_c") and "mram14" in d:
                        dvfs_dir = full
                    elif "baseline_sram_only" in d:
                        s14_dir = full

                cf_sz_dir = os.path.join(cf_root, bench, n_tag, f"l3_{sz}MB")
                if os.path.isdir(cf_sz_dir):
                    for d in os.listdir(cf_sz_dir):
                        if "mram14" in d:
                            cf_dir = os.path.join(cf_sz_dir, d)

                if not all([dvfs_dir, cf_dir, s14_dir]):
                    continue

                # S14 elapsed (reference)
                s14_db = os.path.join(s14_dir, "sim.stats.sqlite3")
                dvfs_db = os.path.join(dvfs_dir, "sim.stats.sqlite3")
                cf_db   = os.path.join(cf_dir, "sim.stats.sqlite3")
                if not all(os.path.exists(p) for p in [s14_db, dvfs_db, cf_db]):
                    continue

                dvfs_st = _get_dvfs_stats(dvfs_db, n_cores, PI_CYC)
                cf_st   = _get_dvfs_stats(cf_db,   n_cores, PI_CYC)
                s14_st  = _get_dvfs_stats(s14_db,  n_cores, PI_CYC)
                if not all([dvfs_st, cf_st, s14_st]):
                    continue

                # Per-core averages
                def avg(lst): return sum(lst) / len(lst) if lst else 0.0

                d_ntrans = avg(dvfs_st["n_trans"])
                c_ntrans = avg(cf_st["n_trans"])

                # f_eff proxy: f_base × s14_elapsed / dvfs_elapsed (per-core avg)
                d_feff_list, c_feff_list = [], []
                for i in range(n_cores):
                    s14_e = s14_st["elapsed_s"][i] if i < len(s14_st["elapsed_s"]) else None
                    d_e   = dvfs_st["elapsed_s"][i] if i < len(dvfs_st["elapsed_s"]) else None
                    c_e   = cf_st["elapsed_s"][i]   if i < len(cf_st["elapsed_s"])   else None
                    if s14_e and d_e and d_e > 0:
                        d_feff_list.append(F_BASE * s14_e / d_e)
                    if s14_e and c_e and c_e > 0:
                        c_feff_list.append(F_BASE * s14_e / c_e)

                d_feff = avg(d_feff_list)
                c_feff = avg(c_feff_list)
                delta_f = d_feff - c_feff

                bench_short = bench[:44]
                print(f"{bench_short:<45} {n_tag:>5} {sz:>3}"
                      f" {d_ntrans:>9.0f} {c_ntrans:>10.0f}"
                      f" {d_feff:>8.3f} {c_feff:>8.3f} {delta_f:>+8.3f}")
        if n_tag != "n8":
            print("-" * W)
    print()


# ---------------------------------------------------------------------------
# Helpers for sweep tables 10/11
# ---------------------------------------------------------------------------
def _find_sweep_dir(sweep_root: str, bench: str, n_tag: str,
                    l3_mb: int, suffix: str) -> Optional[str]:
    """Find a sweep variant dir by suffix pattern (e.g. '_rdx2' or '_lk0.25')."""
    sz_dir = os.path.join(sweep_root, bench, n_tag, f"l3_{l3_mb}MB")
    if not os.path.isdir(sz_dir):
        return None
    for d in os.listdir(sz_dir):
        if d.endswith(suffix) and d.startswith("lc_c"):
            full = os.path.join(sz_dir, d)
            if os.path.isdir(full):
                return full
    return None


def _find_variant_dir(runs_root: str, bench: str, n_tag: str,
                      l3_mb: int, pattern: str) -> Optional[str]:
    """Find a variant dir matching a pattern (e.g. 'baseline_sram_only', 'lc_c*mram14')."""
    sz_dir = os.path.join(runs_root, bench, n_tag, f"l3_{l3_mb}MB")
    if not os.path.isdir(sz_dir):
        return None
    for d in os.listdir(sz_dir):
        if pattern in d:
            full = os.path.join(sz_dir, d)
            if os.path.isdir(full):
                return full
    return None


def _get_makespan(run_dir: str) -> Optional[float]:
    """Get makespan in seconds from a run dir. Returns max per-core elapsed."""
    if not run_dir:
        return None
    times = get_times_from_dir(run_dir)
    if not times:
        return None
    valid = [t for t in times if t is not None and t > 0]
    return max(valid) if valid else None


def _print_sweep_table(base: str, table_num: int, title: str,
                       sweep_stage: str, suffix_fn, param_values: list,
                       param_label: str, l3_mb: int = 32):
    """
    Generic sweep table printer.
    suffix_fn(val) -> suffix string, e.g. lambda m: f"_rdx{m}"
    param_values: list of sweep param values, e.g. [2,3,4,5] or [0.25,0.50,0.75]
    Shows: workload | cores | baseline_ms | {param: dvfs_ms dvfs_spdup cf_ms cf_spdup}
    """
    dvfs_root = os.path.join(base, "dvfs", "1_main_dvfs", "runs")
    cf_root = os.path.join(base, "dvfs", "1b_counterfactual", "runs")
    sweep_root = os.path.join(base, "dvfs", sweep_stage, "runs")

    # Build header
    param_hdrs = []
    for v in param_values:
        param_hdrs.append(f"D_{param_label}{v}")
        param_hdrs.append(f"CF_{param_label}{v}")
    col_w = 9
    W = 42 + len(param_values) * 2 * (col_w + 1)

    print("=" * W)
    print(f"TABLE {table_num}: {title}")
    print(f"  Values: speedup (%) vs SRAM14 baseline at {l3_mb}MB")
    print("=" * W)
    hdr = f"{'Workload':<35} {'N':>3} {'Base_ms':>8}"
    for v in param_values:
        hdr += f" {'D_'+str(v):>{col_w}} {'CF_'+str(v):>{col_w}}"
    print(hdr)
    print("-" * W)

    for n_tag in ["n1", "n4", "n8"]:
        n_cores = int(n_tag[1:])
        # Find all benchmarks that have a baseline at this size/cores
        if not os.path.isdir(dvfs_root):
            continue
        benches = sorted({
            entry for entry in os.listdir(dvfs_root)
            if os.path.isdir(os.path.join(dvfs_root, entry, n_tag,
                                          f"l3_{l3_mb}MB"))
        })
        for bench in benches:
            # SRAM14 baseline
            s14_dir = _find_variant_dir(dvfs_root, bench, n_tag, l3_mb,
                                        "baseline_sram_only")
            base_t = _get_makespan(s14_dir)

            cells = []
            for v in param_values:
                sfx = suffix_fn(v)
                # DVFS sweep variant
                d_dir = _find_sweep_dir(sweep_root, bench, n_tag, l3_mb, sfx)
                d_t = _get_makespan(d_dir)
                if base_t and d_t and d_t > 0:
                    cells.append(f"{(base_t/d_t - 1)*100:>+{col_w}.2f}")
                else:
                    cells.append(f"{'—':>{col_w}}")

                # CF variant — same suffix in the sweep dir but using CF
                # CF runs live in 1b_counterfactual, without sweep suffix
                # (CF is only at the base cap, not at sweep variants)
                # So we just show the CF baseline speedup once
                cf_dir = _find_variant_dir(cf_root, bench, n_tag, l3_mb,
                                           "mram14")
                cf_t = _get_makespan(cf_dir)
                if base_t and cf_t and cf_t > 0:
                    cells.append(f"{(base_t/cf_t - 1)*100:>+{col_w}.2f}")
                else:
                    cells.append(f"{'—':>{col_w}}")

            bench_short = bench[:34]
            base_ms = f"{base_t*1e3:>8.1f}" if base_t else f"{'—':>8}"
            print(f"{bench_short:<35} {n_tag:>3} {base_ms} {' '.join(cells)}")

        if n_tag != "n8":
            print("-" * W)
    print()


def print_table10(base: str):
    """Table 10: Read Latency Sweep at 32MB."""
    _print_sweep_table(
        base, 10,
        "Read-Latency Sensitivity — 32MB (MRAM+DVFS vs CF)",
        "2_read_latency",
        lambda m: f"_rdx{m}",
        [2, 3, 4, 5],
        "x", l3_mb=32,
    )


def print_table11(base: str):
    """Table 11: Leakage Gap Sweep at 32MB."""
    _print_sweep_table(
        base, 11,
        "Leakage-Gap Sensitivity — 32MB (MRAM+DVFS vs CF)",
        "3_leakage_gap",
        lambda f: f"_lk{f}",
        ["0.25", "0.50", "0.75"],
        "lk", l3_mb=32,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def print_table12(base: str):
    """Table 12: Cap +/- MAE at 32MB, MRAM+DVFS only."""
    import yaml
    params_yaml = os.path.join(os.path.dirname(__file__), "..", "config", "params.yaml")
    if not os.path.exists(params_yaml):
        params_yaml = os.path.join(base, "..", "mx3", "config", "params.yaml")
    try:
        params = yaml.safe_load(open(params_yaml))
    except Exception:
        print("TABLE 12: Cannot load params.yaml -- skipping.")
        return

    caps_all = params.get("uarch", {}).get("sunnycove", {}).get("plm_cap_w", {})
    MAE = {1: 0.640, 4: 0.480, 8: 1.015}
    L3 = 32

    dvfs_root = os.path.join(base, "dvfs", "1_main_dvfs", "runs")
    mae_root = os.path.join(base, "dvfs", "4_cap_mae", "runs")

    W = 80
    print("=" * W)
    print("TABLE 12: Cap +/- MAE Sensitivity -- 32MB, MRAM+DVFS only")
    print(f"  MAE: n1={MAE[1]:.3f}W  n4={MAE[4]:.3f}W  n8={MAE[8]:.3f}W")
    print("  Values: speedup (%) vs SRAM14 baseline")
    print("=" * W)
    print(f"{'Workload':<35} {'N':>3} {'Base_ms':>8} {'cap-MAE':>9} {'cap+MAE':>9}")
    print("-" * W)

    def lbl(v):
        return f"{v:.2f}".replace(".", "p")

    for n_tag in ["n1", "n4", "n8"]:
        n = int(n_tag[1:])
        mae = MAE[n]
        wl_caps = caps_all.get(f"n{n}", {})
        benches = sorted(wl for wl in wl_caps
                         if isinstance(wl_caps[wl], dict) and L3 in wl_caps[wl])
        for bench in benches:
            cap = wl_caps[bench][L3]
            cap_minus = max(cap - mae, 1.0)
            cap_plus = cap + mae

            s14_dir = _find_variant_dir(dvfs_root, bench, n_tag, L3,
                                        "baseline_sram_only")
            base_t = _get_makespan(s14_dir)

            minus_dir = _find_variant_dir(mae_root, bench, n_tag, L3,
                                          f"lc_c{lbl(cap_minus)}")
            plus_dir = _find_variant_dir(mae_root, bench, n_tag, L3,
                                         f"lc_c{lbl(cap_plus)}")
            minus_t = _get_makespan(minus_dir)
            plus_t = _get_makespan(plus_dir)

            base_ms = f"{base_t*1e3:>8.1f}" if base_t else f"{'---':>8}"
            if base_t and minus_t and minus_t > 0:
                m_cell = f"{(base_t/minus_t - 1)*100:>+9.2f}"
            else:
                m_cell = f"{'---':>9}"
            if base_t and plus_t and plus_t > 0:
                p_cell = f"{(base_t/plus_t - 1)*100:>+9.2f}"
            else:
                p_cell = f"{'---':>9}"

            bench_short = bench[:34]
            print(f"{bench_short:<35} {n_tag:>3} {base_ms} {m_cell} {p_cell}")

        if n_tag != "n8":
            print("-" * W)
    print()


def main():
    args = parse_args()
    base = os.path.abspath(args.base)
    tables_to_print = {int(t) for t in args.tables.split(",")}
    sizes = [16, 32, 128]

    print(f"[INFO] Collecting data from: {base}", file=sys.stderr)
    data, mc_data, hca_data = collect_data(base)
    print(f"[INFO] n1 entries: {len(data)}, mc entries: {len(mc_data)}, "
          f"hca_detail entries: {len(hca_data)}", file=sys.stderr)
    print()

    if 1 in tables_to_print:
        print_table1(data, sizes)
    if 2 in tables_to_print:
        print_table2(data, sizes)
    if 3 in tables_to_print:
        print_table3(data, sizes)
    if 4 in tables_to_print:
        print_table4(hca_data)
    if 5 in tables_to_print:
        print_table5(hca_data)
    if 6 in tables_to_print:
        print_table6(hca_data)
    if 7 in tables_to_print:
        print_table7(mc_data, sizes)
    if 8 in tables_to_print:
        print_table8(data, base)
    if 9 in tables_to_print:
        print_table9(base, sizes)
    if 10 in tables_to_print:
        print_table10(base)
    if 11 in tables_to_print:
        print_table11(base)
    if 12 in tables_to_print:
        print_table12(base)


if __name__ == "__main__":
    main()

