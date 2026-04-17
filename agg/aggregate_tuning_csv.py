#!/usr/bin/env python3
"""
aggregate_tuning_csv.py

Write a CSV for Stage 15 DVFS tuning sweep at:
    ~/COSC_498/miniMXE/repro/agg/tuning.csv

Columns:
  workload, workload_short, size_mb, interval_ins, hysteresis_w,
  variant, speedup_vs_sram7, elapsed_s, sram7_elapsed_s,
  mean_power_w, frac_over_pcap, num_intervals, num_transitions,
  transition_rate, avg_freq_ghz, max_freq_ghz

Usage:
  python3 mx3/agg/aggregate_tuning_csv.py
  python3 mx3/agg/aggregate_tuning_csv.py --out repro/agg/tuning.csv
"""

import os
import re
import csv
import sqlite3
import argparse

# -----------------------------------------------------------------------------
# Defaults
# -----------------------------------------------------------------------------
DEFAULT_BASE = os.path.expanduser("~/COSC_498/miniMXE/repro")
DEFAULT_OUT = os.path.expanduser("~/COSC_498/miniMXE/repro/agg/tuning.csv")

# -----------------------------------------------------------------------------
# SQLite helpers (same as aggregate_dvfs_csv.py)
# -----------------------------------------------------------------------------
def _get_stat(db_path, obj, metric, prefix="roi-end", core=0):
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


def get_delta(db_path, obj, metric, core=0):
    end = _get_stat(db_path, obj, metric, "roi-end", core)
    begin = _get_stat(db_path, obj, metric, "roi-begin", core)
    if end is not None and begin is not None:
        return end - begin
    return end


# -----------------------------------------------------------------------------
# Time extraction
# -----------------------------------------------------------------------------
def _read_sim_n(run_dir):
    try:
        with open(os.path.join(run_dir, "run.yaml")) as f:
            for line in f:
                line = line.strip()
                if line.startswith("sim_n:"):
                    return int(line.split(":")[1].strip())
    except Exception:
        pass
    return None


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


def makespan_s(run_dir):
    if not run_dir or not os.path.isdir(run_dir):
        return None

    sim_n = _read_sim_n(run_dir)

    sim_out = os.path.join(run_dir, "sim.out")
    times = _parse_sim_out_times(sim_out)
    if times:
        if sim_n and len(times) > sim_n:
            times = times[:sim_n]
        vals = [t for t in times if t is not None and t > 0]
        return max(vals) if vals else None

    db_path = os.path.join(run_dir, "sim.stats.sqlite3")
    if not os.path.exists(db_path):
        return None

    from aggregate_dvfs_csv import get_num_cores  # avoid duplication
    n_cores = sim_n or 1
    t = get_delta(db_path, "thread", "elapsed_time", 0)
    if t is None or t <= 0:
        t = get_delta(db_path, "performance_model", "elapsed_time", 0)
    return t * 1e-15 if t is not None and t > 0 else None


# -----------------------------------------------------------------------------
# Result detection
# -----------------------------------------------------------------------------
def _has_results(run_dir):
    if not run_dir or not os.path.isdir(run_dir):
        return False
    return (
        os.path.exists(os.path.join(run_dir, "sim.out"))
        or os.path.exists(os.path.join(run_dir, "sim.stats.sqlite3"))
    )


# -----------------------------------------------------------------------------
# Baseline lookup
# -----------------------------------------------------------------------------
def find_sram7_baseline(base, bench, size_mb):
    leaf = "baseline_sram_only_sram7"

    # 12_baseline_run
    d = os.path.join(base, "dvfs", "12_baseline_run", "runs", bench, "n1", f"l3_{size_mb}MB", leaf)
    if _has_results(d):
        return d

    # 1_main_dvfs
    d = os.path.join(base, "dvfs", "1_main_dvfs", "runs", bench, "n1", f"l3_{size_mb}MB", leaf)
    if _has_results(d):
        return d

    # hca fallback
    hca_root = os.path.join(base, "hca", "hca_sunnycove")
    bench_us = bench.replace(".", "_", 1)
    sz_tag = f"sz{size_mb}M"
    for campaign in ["1_baselines", "2_cross_node/mram14", "2_cross_node/mram32", "3_static_hca"]:
        runs_dir = os.path.join(hca_root, campaign, "runs")
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
            if _has_results(root):
                return root

    return None


# -----------------------------------------------------------------------------
# Workload shortener
# -----------------------------------------------------------------------------
def shorten_workload(wl):
    m = re.match(r"\d+\.(\w+?)(_[rs])?$", wl)
    return m.group(1) if m else wl


# -----------------------------------------------------------------------------
# Variant name parsing
# -----------------------------------------------------------------------------
def parse_variant(variant):
    """Extract hysteresis (W) and interval (instructions) from variant name."""
    m_h = re.search(r"_h([\dp]+)_f", variant)
    m_pi = re.search(r"_pi(\d+)_", variant)

    h_w = None
    if m_h:
        h_w = float(m_h.group(1).replace("p", "."))

    pi_ins = None
    if m_pi:
        pi_ins = int(m_pi.group(1))

    return h_w, pi_ins


# -----------------------------------------------------------------------------
# Sniper.log parsing
# -----------------------------------------------------------------------------
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


def parse_sniper_log_metrics(log_path):
    out = {
        "mean_power_w": None,
        "frac_over_pcap": None,
        "num_intervals": None,
        "num_transitions": None,
        "transition_rate": None,
        "avg_freq_ghz": None,
        "max_freq_ghz": None,
    }

    if not os.path.exists(log_path):
        return out

    intervals = []
    try:
        with open(log_path) as f:
            for line in f:
                m = _LC_CHANGE_RE.search(line)
                if m:
                    intervals.append({
                        "P_est": float(m.group(1)),
                        "Target": float(m.group(4)),
                        "f_max": float(m.group(13)),
                    })
    except Exception:
        return out

    if not intervals:
        return out

    n = len(intervals)
    n_trans = sum(
        1 for i in range(1, n)
        if abs(intervals[i]["f_max"] - intervals[i - 1]["f_max"]) > 0.001
    )

    out["mean_power_w"] = sum(iv["P_est"] for iv in intervals) / n
    out["frac_over_pcap"] = sum(1 for iv in intervals if iv["P_est"] > iv["Target"]) / n
    out["num_intervals"] = n
    out["num_transitions"] = n_trans
    out["transition_rate"] = n_trans / n if n > 0 else None
    out["avg_freq_ghz"] = sum(iv["f_max"] for iv in intervals) / n
    out["max_freq_ghz"] = max(iv["f_max"] for iv in intervals)

    return out


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
FIELDNAMES = [
    "workload",
    "workload_short",
    "size_mb",
    "interval_ins",
    "hysteresis_w",
    "variant",
    "run_dir",
    "sram7_run_dir",
    "speedup_vs_sram7",
    "elapsed_s",
    "sram7_elapsed_s",
    "mean_power_w",
    "frac_over_pcap",
    "num_intervals",
    "num_transitions",
    "transition_rate",
    "avg_freq_ghz",
    "max_freq_ghz",
]


def main():
    ap = argparse.ArgumentParser(description="Aggregate Stage-15 DVFS tuning sweep")
    ap.add_argument("--base", type=str, default=DEFAULT_BASE,
                    help="Base repro dir")
    ap.add_argument("--out", type=str, default=DEFAULT_OUT,
                    help="Output CSV path")
    args = ap.parse_args()

    base = os.path.expanduser(args.base)
    out = os.path.expanduser(args.out)
    tuning_root = os.path.join(base, "dvfs", "15_tuning", "runs")

    if not os.path.isdir(tuning_root):
        print(f"[error] tuning root not found: {tuning_root}")
        return

    rows = []
    sram7_cache = {}

    for bench in sorted(os.listdir(tuning_root)):
        bench_dir = os.path.join(tuning_root, bench)
        if not os.path.isdir(bench_dir):
            continue

        n1_dir = os.path.join(bench_dir, "n1")
        if not os.path.isdir(n1_dir):
            continue

        for l3_name in sorted(os.listdir(n1_dir)):
            l3_dir = os.path.join(n1_dir, l3_name)
            if not os.path.isdir(l3_dir):
                continue

            m = re.match(r"l3_(\d+)MB$", l3_name)
            if not m:
                continue
            size_mb = int(m.group(1))

            cache_key = (bench, size_mb)
            if cache_key not in sram7_cache:
                sram7_cache[cache_key] = find_sram7_baseline(base, bench, size_mb)
            sram7_dir = sram7_cache[cache_key]

            sram7_ms = makespan_s(sram7_dir) if sram7_dir else None

            for variant in sorted(os.listdir(l3_dir)):
                run_dir = os.path.join(l3_dir, variant)
                if not os.path.isdir(run_dir) or not _has_results(run_dir):
                    continue

                h_w, pi_ins = parse_variant(variant)
                dvfs_ms = makespan_s(run_dir)

                speedup = None
                if sram7_ms and dvfs_ms and dvfs_ms > 0:
                    speedup = sram7_ms / dvfs_ms

                mets = parse_sniper_log_metrics(os.path.join(run_dir, "sniper.log"))

                rows.append({
                    "workload": bench,
                    "workload_short": shorten_workload(bench),
                    "size_mb": size_mb,
                    "interval_ins": pi_ins,
                    "hysteresis_w": h_w,
                    "variant": variant,
                    "run_dir": run_dir,
                    "sram7_run_dir": sram7_dir,
                    "speedup_vs_sram7": speedup,
                    "elapsed_s": dvfs_ms,
                    "sram7_elapsed_s": sram7_ms,
                    **mets,
                })

    # Sort by workload, interval, hysteresis
    rows.sort(key=lambda r: (
        r["workload"],
        r["size_mb"],
        r["interval_ins"] or 0,
        r["hysteresis_w"] or 0,
    ))

    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        w.writerows(rows)

    print(f"[wrote csv] {out}")
    print(f"[rows] {len(rows)}")

    # Print summary pivot
    if rows:
        from collections import defaultdict
        grid = defaultdict(list)
        for r in rows:
            if r["speedup_vs_sram7"] is not None:
                grid[(r["interval_ins"], r["hysteresis_w"])].append(r["speedup_vs_sram7"])

        intervals = sorted(set(r["interval_ins"] for r in rows if r["interval_ins"]))
        hystereses = sorted(set(r["hysteresis_w"] for r in rows if r["hysteresis_w"] is not None))

        print("\n[summary] Mean speedup vs SRAM7 (across workloads):")
        header = f"{'interval':>12}" + "".join(f"  h={h:.2f}W" for h in hystereses)
        print(header)
        for pi in intervals:
            vals = []
            for h in hystereses:
                ss = grid.get((pi, h), [])
                vals.append(f"  {sum(ss)/len(ss):.4f}" if ss else "     N/A")
            print(f"{pi:>12}" + "".join(vals))


if __name__ == "__main__":
    main()
