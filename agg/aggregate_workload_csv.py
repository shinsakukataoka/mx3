#!/usr/bin/env python3
"""
save_workload_csv.py — Per-workload metric summary across baseline configs.

Extracts from sim.stats.sqlite3 and sim.out:
  1. Performance: runtime, IPC, avg effective frequency proxy
  2. LLC / memory behavior: miss rate, MPKI, DRAM accesses, long-latency stalls
  3. Energy: LLC leakage, LLC dynamic, total LLC, estimated package energy, EDP
  4. Power: estimated avg package power

Writes:
    ~/COSC_498/miniMXE/repro/agg/workload.csv

Usage:
    python3 save_workload_csv.py
    python3 save_workload_csv.py --sizes 32
    python3 save_workload_csv.py --sizes 16,32,128
"""
import os
import csv
import sqlite3
import argparse

# ── paths ──────────────────────────────────────────────────────────────
BASE = "/home/skataoka26/COSC_498/miniMXE/repro"
SIZES = [16, 32, 128]
OUT_CSV = os.path.join(BASE, "agg", "workload.csv")

MAIN_DVFS_ROOT    = os.path.join(BASE, "dvfs", "1_main_dvfs", "runs")
BASELINE_RUN_ROOT = os.path.join(BASE, "dvfs", "12_baseline_run", "runs")
HCA_ROOT          = os.path.join(BASE, "hca", "hca_sunnycove")

P_STATIC_W = 20.08  # sunnycove static
F_BASE_GHZ = 2.2

WORKLOADS_N1 = [
    "500.perlbench_r", "502.gcc_r", "505.mcf_r", "520.omnetpp_r",
    "523.xalancbmk_r", "531.deepsjeng_r", "541.leela_r", "557.xz_r",
    "648.exchange2_s", "649.fotonik3d_s",
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

# ── SQLite helpers ─────────────────────────────────────────────────────
def _get_stat(db_path, obj, metric, prefix="roi-end", core=0):
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        c = conn.cursor()
        c.execute(
            '''SELECT v.value FROM "values" v
               JOIN names n ON v.nameid = n.nameid
               JOIN prefixes p ON v.prefixid = p.prefixid
               WHERE n.objectname=? AND n.metricname=? AND p.prefixname=? AND v.core=?''',
            (obj, metric, prefix, core))
        row = c.fetchone()
        conn.close()
        return float(row[0]) if row else None
    except Exception:
        return None


def get_delta(db_path, obj, metric, core=0):
    end = _get_stat(db_path, obj, metric, "roi-end", core)
    begin = _get_stat(db_path, obj, metric, "roi-begin", core)
    if end is not None and begin is not None:
        return end - begin
    return end


def get_num_cores(db_path):
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        c = conn.cursor()
        c.execute('SELECT MAX(v.core) FROM "values" v')
        row = c.fetchone()
        conn.close()
        return int(row[0]) + 1 if row and row[0] is not None else 1
    except Exception:
        return 1


# ── time extraction ────────────────────────────────────────────────────
def _parse_sim_out_times(path):
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


def get_times_from_dir(run_dir):
    if not run_dir or not os.path.isdir(run_dir):
        return None

    times = _parse_sim_out_times(os.path.join(run_dir, "sim.out"))
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
        result.append(t * 1e-15 if t and t > 0 else None)

    return result if any(v is not None and v > 0 for v in result) else None


# ── directory discovery ────────────────────────────────────────────────
def _has_results(run_dir):
    if not run_dir or not os.path.isdir(run_dir):
        return False
    return os.path.exists(os.path.join(run_dir, "sim.out"))


def find_main_baseline_dir(bench, n_tag, size_mb, leaf):
    sz_dir = os.path.join(MAIN_DVFS_ROOT, bench, n_tag, f"l3_{size_mb}MB")
    if not os.path.isdir(sz_dir):
        return None
    exact = os.path.join(sz_dir, leaf)
    return exact if os.path.isdir(exact) else None


def find_hca_baseline_dir_n1(bench, size_mb, leaf):
    bench_us = bench.replace(".", "_", 1)
    sz_tag = f"sz{size_mb}M"
    for campaign in ["1_baselines", "2_cross_node/mram14", "2_cross_node/mram32", "3_static_hca"]:
        runs_dir = os.path.join(HCA_ROOT, campaign, "runs")
        if not os.path.isdir(runs_dir):
            continue
        for root, _, _ in os.walk(runs_dir):
            parts = root.split(os.sep)
            if parts[-1] != leaf:
                continue
            if sz_tag not in parts:
                continue
            if not any(p.startswith(bench) or p.startswith(bench_us) for p in parts):
                continue
            return root
    return None


def find_sram7_baseline(bench, n_tag, size_mb):
    fb = os.path.join(BASELINE_RUN_ROOT, bench, n_tag, f"l3_{size_mb}MB", "baseline_sram_only_sram7")
    if _has_results(fb):
        return fb

    d = find_main_baseline_dir(bench, n_tag, size_mb, "baseline_sram_only_sram7")
    if d and _has_results(d):
        return d

    if n_tag == "n1":
        d2 = find_hca_baseline_dir_n1(bench, size_mb, "baseline_sram_only_sram7")
        return d2 if d2 and _has_results(d2) else None

    return None


def find_mram14_baseline(bench, n_tag, size_mb):
    fb = os.path.join(BASELINE_RUN_ROOT, bench, n_tag, f"l3_{size_mb}MB", "baseline_mram_only_mram14")
    if _has_results(fb):
        return fb

    d = find_main_baseline_dir(bench, n_tag, size_mb, "baseline_mram_only_mram14")
    if d and _has_results(d):
        return d

    if n_tag == "n1":
        hca = find_hca_baseline_dir_n1(bench, size_mb, "baseline_mram_only_mram14")
        return hca if hca and _has_results(hca) else None

    return None

# ── metric extraction ──────────────────────────────────────────────────
def extract_all_metrics(run_dir):
    """Extract all available metrics from a run directory. Returns dict."""
    if not run_dir or not os.path.isdir(run_dir):
        return None

    db_path = os.path.join(run_dir, "sim.stats.sqlite3")
    if not os.path.exists(db_path):
        return None

    n_cores = get_num_cores(db_path)
    m = {}

    def sum_cores(obj, metric):
        total = 0
        found_any = False
        for c in range(n_cores):
            v = get_delta(db_path, obj, metric, c)
            if v is not None:
                total += v
                found_any = True
        return total if found_any else None

    times = get_times_from_dir(run_dir)
    if not times:
        return None

    valid_times = [t for t in times if t is not None and t > 0]
    if not valid_times:
        return None

    m["n_cores"] = n_cores
    m["elapsed_s_per_core"] = valid_times
    m["makespan_s"] = max(valid_times)
    m["avg_elapsed_s"] = sum(valid_times) / len(valid_times)

    total_insns = sum_cores("performance_model", "instruction_count")
    m["total_instructions"] = total_insns

    m["throughput_inst_ns"] = (
        total_insns / (m["makespan_s"] * 1e9)
        if total_insns is not None and m["makespan_s"] > 0 else None
    )

    total_uops = sum_cores("rob_timer", "uops_total")
    m["total_uops"] = total_uops

    cpi_components = {}
    for comp in [
        "cpiBase", "cpiBranchPredictor", "cpiDataCacheL1",
        "cpiDataCacheL2", "cpiDataCacheL3", "cpiDataCachedram",
        "cpiDataCachedram-local", "cpiDataCachedram-remote",
        "cpiRSFull", "cpiSerialization"
    ]:
        v = sum_cores("rob_timer", comp)
        if v is not None and v > 0:
            cpi_components[comp] = v
    m["cpi_components"] = cpi_components

    total_cpi_fs = sum(cpi_components.values()) if cpi_components else None

    total_elapsed_fs = 0
    found_elapsed = False
    for c in range(n_cores):
        v = get_delta(db_path, "performance_model", "elapsed_time", c)
        if v is not None:
            total_elapsed_fs += v
            found_elapsed = True

    avg_elapsed_fs = (total_elapsed_fs / n_cores) if found_elapsed and n_cores > 0 else 0
    if avg_elapsed_fs > 0 and total_insns is not None and total_insns > 0:
        cycles_at_base = avg_elapsed_fs * F_BASE_GHZ * 1e-6
        m["avg_ipc"] = (total_insns / n_cores) / cycles_at_base
    else:
        m["avg_ipc"] = None

    ll_cycles = sum_cores("rob_timer", "outstandingLongLatencyCycles")
    m["long_lat_cycles"] = ll_cycles
    if ll_cycles is not None and total_cpi_fs and total_cpi_fs > 0:
        m["long_lat_frac"] = ll_cycles / total_cpi_fs
    else:
        m["long_lat_frac"] = None

    mem_cpi = (
        sum(v for k, v in cpi_components.items() if "DataCache" in k and k != "cpiDataCacheL1")
        if cpi_components else 0
    )
    m["mem_cpi_frac"] = mem_cpi / total_cpi_fs if total_cpi_fs and total_cpi_fs > 0 else None

    l3_loads = sum_cores("L3", "loads")
    l3_stores = sum_cores("L3", "stores")
    l3_load_misses = sum_cores("L3", "load-misses")
    l3_store_misses = sum_cores("L3", "store-misses")
    l3_rh = sum_cores("L3", "l3_read_hits")
    l3_wh = sum_cores("L3", "l3_write_hits")

    m["l3_loads"] = l3_loads
    m["l3_stores"] = l3_stores
    m["l3_load_misses"] = l3_load_misses
    m["l3_store_misses"] = l3_store_misses
    m["l3_read_hits"] = l3_rh
    m["l3_write_hits"] = l3_wh
    m["l3_total_accesses"] = (
        (l3_loads or 0) + (l3_stores or 0)
        if (l3_loads is not None or l3_stores is not None) else None
    )

    if l3_loads is not None and l3_loads > 0 and l3_load_misses is not None:
        m["l3_miss_rate"] = l3_load_misses / l3_loads
    else:
        m["l3_miss_rate"] = None

    if total_insns is not None and total_insns > 0 and l3_load_misses is not None:
        m["l3_mpki"] = l3_load_misses / total_insns * 1000
    else:
        m["l3_mpki"] = None

    dram_reads = sum_cores("dram", "reads")
    dram_writes = sum_cores("dram", "writes")
    m["dram_reads"] = dram_reads
    m["dram_writes"] = dram_writes
    m["dram_total"] = (
        (dram_reads or 0) + (dram_writes or 0)
        if (dram_reads is not None or dram_writes is not None) else None
    )

    if total_insns is not None and total_insns > 0 and m["dram_total"] is not None:
        m["dram_mpki"] = m["dram_total"] / total_insns * 1000
    else:
        m["dram_mpki"] = None

    llc_dyn_pj = sum_cores("L3", "llc_dyn_energy_pJ")
    llc_leak_raw = sum_cores("L3", "llc_leakage_energy_pJ")
    m["llc_dyn_energy_j"] = llc_dyn_pj * 1e-12 if llc_dyn_pj is not None else None
    m["llc_leak_energy_j"] = llc_leak_raw * 1e-21 if llc_leak_raw is not None and llc_leak_raw > 0 else None

    if m["llc_dyn_energy_j"] is not None and m["llc_leak_energy_j"] is not None:
        m["llc_total_energy_j"] = m["llc_dyn_energy_j"] + m["llc_leak_energy_j"]
    elif m["llc_dyn_energy_j"] is not None:
        m["llc_total_energy_j"] = m["llc_dyn_energy_j"]
    elif m["llc_leak_energy_j"] is not None:
        m["llc_total_energy_j"] = m["llc_leak_energy_j"]
    else:
        m["llc_total_energy_j"] = None

    dvfs_trans_fs = sum_cores("performance_model", "cpiSyncDvfsTransition")
    m["dvfs_transition_ms"] = dvfs_trans_fs * 1e-12 if dvfs_trans_fs is not None else 0.0

    m["_raw_makespan_for_feff"] = m["makespan_s"]
    return m


# ── row building ───────────────────────────────────────────────────────
def pkg_energy_j(m):
    if m is None or m.get("makespan_s") is None:
        return None
    e_static = P_STATIC_W * m["makespan_s"]
    e_llc = m.get("llc_total_energy_j") or 0.0
    return e_static + e_llc


def pkg_power_w(m):
    e = pkg_energy_j(m)
    if e is None or m is None or m.get("makespan_s") is None or m["makespan_s"] <= 0:
        return None
    return e / m["makespan_s"]


def edp_j_s(m):
    e = pkg_energy_j(m)
    if e is None or m is None or m.get("makespan_s") is None:
        return None
    return e * m["makespan_s"]


def build_row(bench, n_tag, size_mb, config, run_dir, metrics, sram7_makespan=None):
    row = {
        "workload": bench,
        "n_tag": n_tag,
        "size_mb": size_mb,
        "config": config,
        "run_dir": run_dir,

        "n_cores": metrics.get("n_cores"),
        "makespan_s": metrics.get("makespan_s"),
        "avg_elapsed_s": metrics.get("avg_elapsed_s"),
        "throughput_inst_ns": metrics.get("throughput_inst_ns"),
        "avg_ipc": metrics.get("avg_ipc"),

        "speedup_vs_sram7_pct": None,
        "eff_freq_proxy_ghz": None,
        "ws_n_vs_sram7": None,

        "total_instructions": metrics.get("total_instructions"),
        "total_uops": metrics.get("total_uops"),

        "l3_loads": metrics.get("l3_loads"),
        "l3_stores": metrics.get("l3_stores"),
        "l3_load_misses": metrics.get("l3_load_misses"),
        "l3_store_misses": metrics.get("l3_store_misses"),
        "l3_read_hits": metrics.get("l3_read_hits"),
        "l3_write_hits": metrics.get("l3_write_hits"),
        "l3_total_accesses": metrics.get("l3_total_accesses"),
        "l3_miss_rate": metrics.get("l3_miss_rate"),
        "l3_mpki": metrics.get("l3_mpki"),

        "dram_reads": metrics.get("dram_reads"),
        "dram_writes": metrics.get("dram_writes"),
        "dram_total": metrics.get("dram_total"),
        "dram_mpki": metrics.get("dram_mpki"),

        "long_lat_cycles": metrics.get("long_lat_cycles"),
        "long_lat_frac": metrics.get("long_lat_frac"),
        "mem_cpi_frac": metrics.get("mem_cpi_frac"),

        "llc_dyn_energy_j": metrics.get("llc_dyn_energy_j"),
        "llc_leak_energy_j": metrics.get("llc_leak_energy_j"),
        "llc_total_energy_j": metrics.get("llc_total_energy_j"),

        "static_energy_j": (P_STATIC_W * metrics["makespan_s"]) if metrics.get("makespan_s") is not None else None,
        "pkg_energy_est_j": pkg_energy_j(metrics),
        "avg_pkg_power_est_w": pkg_power_w(metrics),
        "edp_j_s": edp_j_s(metrics),

        "dvfs_transition_ms": metrics.get("dvfs_transition_ms"),
    }

    if sram7_makespan is not None and metrics.get("makespan_s") is not None and metrics["makespan_s"] > 0:
        row["speedup_vs_sram7_pct"] = (sram7_makespan / metrics["makespan_s"] - 1.0) * 100.0
        row["eff_freq_proxy_ghz"] = F_BASE_GHZ * sram7_makespan / metrics["makespan_s"]

    return row


def maybe_add_ws_n(rows_for_group, metrics_by_config):
    """
    Adds ws_n_vs_sram7 for multicore rows in-place.
    WS/N = avg_i (T_sram7_i / T_cfg_i)
    """
    if "SRAM7" not in metrics_by_config:
        return

    s7_times = metrics_by_config["SRAM7"].get("elapsed_s_per_core")
    if not s7_times:
        return

    for row in rows_for_group:
        if row["n_tag"] == "n1":
            continue
        cfg = row["config"]
        m = metrics_by_config.get(cfg)
        if not m:
            continue
        cfg_times = m.get("elapsed_s_per_core")
        if not cfg_times:
            continue

        pairs = [(b, v) for b, v in zip(s7_times, cfg_times) if b is not None and v is not None and v > 0]
        if not pairs:
            continue

        row["ws_n_vs_sram7"] = sum(b / v for b, v in pairs) / len(pairs)


# ── main ───────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sizes", default="16,32,128",
                    help="Comma-separated LLC sizes (default: 16,32,128)")
    args = ap.parse_args()
    sizes = [int(x) for x in args.sizes.split(",")]

    rows = []

    for bench in WORKLOADS_N1:
        for size_mb in sizes:
            configs = {}
            d = find_sram7_baseline(bench, "n1", size_mb)
            if d:
                configs["SRAM7"] = d
            d = find_mram14_baseline(bench, "n1", size_mb)
            if d:
                configs["MRAM14"] = d

            if not configs:
                continue

            metrics_by_config = {}
            for label, run_dir in configs.items():
                m = extract_all_metrics(run_dir)
                if m:
                    metrics_by_config[label] = m

            if not metrics_by_config:
                continue

            sram7_makespan = metrics_by_config.get("SRAM7", {}).get("makespan_s")

            group_rows = []
            for label, m in metrics_by_config.items():
                group_rows.append(build_row(
                    bench=bench,
                    n_tag="n1",
                    size_mb=size_mb,
                    config=label,
                    run_dir=configs[label],
                    metrics=m,
                    sram7_makespan=sram7_makespan,
                ))

            maybe_add_ws_n(group_rows, metrics_by_config)
            rows.extend(group_rows)

    for n_tag, workloads in [("n4", WORKLOADS_N4), ("n8", WORKLOADS_N8)]:
        for bench in workloads:
            for size_mb in sizes:
                configs = {}
                d = find_sram7_baseline(bench, n_tag, size_mb)
                if d:
                    configs["SRAM7"] = d
                d = find_mram14_baseline(bench, n_tag, size_mb)
                if d:
                    configs["MRAM14"] = d

                if not configs:
                    continue

                metrics_by_config = {}
                for label, run_dir in configs.items():
                    m = extract_all_metrics(run_dir)
                    if m:
                        metrics_by_config[label] = m

                if not metrics_by_config:
                    continue

                sram7_makespan = metrics_by_config.get("SRAM7", {}).get("makespan_s")

                group_rows = []
                for label, m in metrics_by_config.items():
                    group_rows.append(build_row(
                        bench=bench,
                        n_tag=n_tag,
                        size_mb=size_mb,
                        config=label,
                        run_dir=configs[label],
                        metrics=m,
                        sram7_makespan=sram7_makespan,
                    ))

                maybe_add_ws_n(group_rows, metrics_by_config)
                rows.extend(group_rows)

    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)

    fieldnames = [
        "workload", "n_tag", "size_mb", "config", "run_dir",
        "n_cores", "makespan_s", "avg_elapsed_s", "throughput_inst_ns", "avg_ipc",
        "speedup_vs_sram7_pct", "eff_freq_proxy_ghz", "ws_n_vs_sram7",
        "total_instructions", "total_uops",
        "l3_loads", "l3_stores", "l3_load_misses", "l3_store_misses",
        "l3_read_hits", "l3_write_hits", "l3_total_accesses", "l3_miss_rate", "l3_mpki",
        "dram_reads", "dram_writes", "dram_total", "dram_mpki",
        "long_lat_cycles", "long_lat_frac", "mem_cpi_frac",
        "llc_dyn_energy_j", "llc_leak_energy_j", "llc_total_energy_j",
        "static_energy_j", "pkg_energy_est_j", "avg_pkg_power_est_w", "edp_j_s",
        "dvfs_transition_ms",
    ]

    with open(OUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows to {OUT_CSV}")


if __name__ == "__main__":
    main()