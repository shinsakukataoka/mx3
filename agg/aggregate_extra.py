#!/usr/bin/env python3
"""
Build one unified CSV for the three extra tables (16MB only):

  - Table 8  : energy decomposition
  - Table 10 : read-latency sweep
  - Table 12 : cap +/- MAE

Output:
  ~/COSC_498/miniMXE/repro/agg/extra.csv

Run from anywhere:
  python3 build_extra_csv.py --base ~/COSC_498/miniMXE/repro
"""

import argparse
import csv
import os
import re
import sqlite3
from pathlib import Path
from typing import Optional

# -----------------------------
# CLI
# -----------------------------
def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="~/COSC_498/miniMXE/repro")
    ap.add_argument("--out", default="~/COSC_498/miniMXE/repro/agg/extra.csv")
    return ap.parse_args()


# -----------------------------
# SQLite helpers
# -----------------------------
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
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        c = conn.cursor()
        c.execute('SELECT MAX(v.core) FROM "values" v')
        row = c.fetchone()
        conn.close()
        return int(row[0]) + 1 if row and row[0] is not None else 1
    except Exception:
        return 1


# -----------------------------
# Time helpers
# -----------------------------
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


def get_times_from_dir(root: str):
    times = _parse_sim_out_times(os.path.join(root, "sim.out"))
    if times:
        return times

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
            result.append(t * 1e-15)
        else:
            result.append(None)

    if any(v is not None and v > 0 for v in result):
        return result
    return None


def _get_makespan(run_dir: str) -> Optional[float]:
    if not run_dir:
        return None
    times = get_times_from_dir(run_dir)
    if not times:
        return None
    valid = [t for t in times if t is not None and t > 0]
    return max(valid) if valid else None


# -----------------------------
# Path finders
# -----------------------------
def _find_variant_dir(runs_root: str, bench: str, n_tag: str,
                      l3_mb: int, pattern: str) -> Optional[str]:
    sz_dir = os.path.join(runs_root, bench, n_tag, f"l3_{l3_mb}MB")
    if not os.path.isdir(sz_dir):
        return None
    for d in os.listdir(sz_dir):
        if pattern in d:
            full = os.path.join(sz_dir, d)
            if os.path.isdir(full):
                return full
    return None


def _find_sweep_dir(sweep_root: str, bench: str, n_tag: str,
                    l3_mb: int, suffix: str) -> Optional[str]:
    sz_dir = os.path.join(sweep_root, bench, n_tag, f"l3_{l3_mb}MB")
    if not os.path.isdir(sz_dir):
        return None
    for d in os.listdir(sz_dir):
        if d.endswith(suffix) and d.startswith("lc_c"):
            full = os.path.join(sz_dir, d)
            if os.path.isdir(full):
                return full
    return None


def _find_hca_rundir(hca_root: str, bench_long: str, size_mb: int,
                     variant_substr: str) -> Optional[str]:
    sz_tag = f"sz{size_mb}M"
    bench_prefix_dot = bench_long
    bench_prefix_us = bench_long.replace(".", "_", 1)

    for campaign in [
        "1_baselines",
        "2_cross_node/mram14",
        "2_cross_node/mram32",
        "3_static_hca",
    ]:
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
            if not any(p.startswith(bench_prefix_dot) or p.startswith(bench_prefix_us) for p in parts):
                continue
            return root
    return None


# -----------------------------
# Energy helpers
# -----------------------------
def load_oracle_noncache(base: str, cap_mb: int = 16, base_freq: float = 2.2,
                         ncores: int = 1) -> dict:
    csv_path = os.path.join(base, "calibration", "plm_calib_sunnycove", "runs", "oracle_points.csv")
    if not os.path.exists(csv_path):
        return {}

    result = {}
    n_tag = f"/n{ncores}/"
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            run_dir = row.get("run_dir", "")
            if n_tag not in run_dir:
                continue
            try:
                sz = int(float(row["size_mb"]))
                fg = float(row["f_ghz"])
                p_noncache = float(row["y_PminusLLC"])
            except Exception:
                continue
            if sz != cap_mb or abs(fg - base_freq) > 0.05:
                continue
            bench = row["bench"]
            result[bench] = p_noncache
    return result


def _energy_row(run_dir: str, rd_metric: str, wr_metric: str,
                r_pj: float, w_pj: float, leak_mw: float,
                p_noncache: float, p_static_w: float) -> Optional[dict]:
    db_path = os.path.join(run_dir, "sim.stats.sqlite3")
    if not os.path.exists(db_path):
        return None

    elapsed_s_list = get_times_from_dir(run_dir)
    if not elapsed_s_list or elapsed_s_list[0] is None:
        return None
    elapsed_s = elapsed_s_list[0]

    rd = get_delta(db_path, "L3", rd_metric) or 0
    wr = get_delta(db_path, "L3", wr_metric) or 0

    e_llc_dyn = (rd * r_pj + wr * w_pj) * 1e-12
    e_llc_leak = (leak_mw * 1e-3) * elapsed_s
    e_nc_static = p_static_w * elapsed_s
    p_nc_dyn = max(0.0, p_noncache - p_static_w)
    e_nc_dyn = p_nc_dyn * elapsed_s
    e_total = e_llc_dyn + e_llc_leak + e_nc_static + e_nc_dyn

    return {
        "elapsed_ms": elapsed_s * 1e3,
        "llc_reads": rd,
        "llc_writes": wr,
        "e_llc_dyn": e_llc_dyn,
        "e_llc_leak": e_llc_leak,
        "e_nc_static": e_nc_static,
        "e_nc_dyn": e_nc_dyn,
        "e_total": e_total,
    }


# -----------------------------
# Bench helpers
# -----------------------------
def short_bench(bench: str) -> str:
    if "." in bench:
        return bench.split(".", 1)[1].split("_")[0]
    return bench.split("_")[1] if "_" in bench else bench


def list_benches_in_dvfs_root(dvfs_root: str, n_tag: str, l3_mb: int):
    if not os.path.isdir(dvfs_root):
        return []
    out = []
    for entry in os.listdir(dvfs_root):
        p = os.path.join(dvfs_root, entry, n_tag, f"l3_{l3_mb}MB")
        if os.path.isdir(p):
            out.append(entry)
    return sorted(out)


# -----------------------------
# Main aggregation
# -----------------------------
def build_rows(base: str):
    L3_MB = 16
    P_STATIC_W = 20.08

    hca_root = os.path.join(base, "hca", "hca_sunnycove")
    dvfs_main_root = os.path.join(base, "dvfs", "1_main_dvfs", "runs")
    cf_root = os.path.join(base, "dvfs", "1b_counterfactual", "runs")
    readlat_root = os.path.join(base, "dvfs", "9_fixed_read_latency", "runs")
    mae_root = os.path.join(base, "dvfs", "11_fixed_cap_mae", "runs")

    rows = []

    # -------------------------
    # Table 8 data
    # -------------------------
    oracle = load_oracle_noncache(base, cap_mb=L3_MB, ncores=1)
    energy_cfgs = [
        ("SRAM7", "energy", "baseline", "l3_read_hits_sram", "l3_write_hits_sram", 727.0, 694.0, 1000.2),
        ("MRAM14", "energy", "baseline", "l3_read_hits_mram", "l3_write_hits_mram", 685.0, 668.0, 204.8),
        ("M+DVFS", "energy", "dvfs", "l3_read_hits_mram", "l3_write_hits_mram", 685.0, 668.0, 204.8),
    ]

    dvfs_fixed_root = os.path.join(base, "dvfs", "7_fixed_dvfs", "runs")

    for bench_long, p_noncache in sorted(oracle.items()):
        for cfg_name, section, variant_kind, rd_m, wr_m, r_pj, w_pj, leak_mw in energy_cfgs:
            run_dir = None
            source_root = None
            variant_name = None

            if cfg_name == "SRAM7":
                run_dir = _find_hca_rundir(hca_root, bench_long, L3_MB, "baseline_sram_only_sram7")
            elif cfg_name == "MRAM14":
                run_dir = _find_hca_rundir(hca_root, bench_long, L3_MB, "baseline_mram_only_mram14")
            else:
                candidate = os.path.join(dvfs_fixed_root, bench_long, "n1", f"l3_{L3_MB}MB")
                if os.path.isdir(candidate):
                    for d in os.listdir(candidate):
                        if d.startswith("lc_"):
                            run_dir = os.path.join(candidate, d)
                            break

            if run_dir:
                source_root = run_dir
                variant_name = os.path.basename(run_dir)

            enr = _energy_row(run_dir, rd_m, wr_m, r_pj, w_pj, leak_mw, p_noncache, P_STATIC_W) if run_dir else None

            rows.append({
                "table_group": "table8_energy",
                "bench": bench_long,
                "bench_short": short_bench(bench_long),
                "ncores": 1,
                "n_tag": "n1",
                "l3_mb": L3_MB,
                "scenario": section,
                "variant": cfg_name,
                "sweep_param": "",
                "sweep_value": "",
                "cap_value_w": "",
                "base_ms": "",
                "run_ms": enr["elapsed_ms"] if enr else "",
                "speedup_pct_vs_sram7": "",
                "oracle_p_noncache_w": p_noncache,
                "llc_reads": enr["llc_reads"] if enr else "",
                "llc_writes": enr["llc_writes"] if enr else "",
                "e_llc_dyn_j": enr["e_llc_dyn"] if enr else "",
                "e_llc_leak_j": enr["e_llc_leak"] if enr else "",
                "e_nc_static_j": enr["e_nc_static"] if enr else "",
                "e_nc_dyn_j": enr["e_nc_dyn"] if enr else "",
                "e_total_j": enr["e_total"] if enr else "",
                "source_dir": source_root or "",
                "source_variant_dirname": variant_name or "",
            })

    # -------------------------
    # Table 10 data
    # -------------------------
    readlat_values = [2, 3, 4, 5]

    for n_tag in ["n1", "n4", "n8"]:
        ncores = int(n_tag[1:])
        benches = list_benches_in_dvfs_root(dvfs_main_root, n_tag, L3_MB)

        for bench in benches:
            s7_dir = _find_variant_dir(dvfs_main_root, bench, n_tag, L3_MB, "baseline_sram_only_sram7")
            if not s7_dir:
                s7_dir = _find_hca_rundir(hca_root, bench, L3_MB, "baseline_sram_only_sram7")
            base_t = _get_makespan(s7_dir)

            # CF baseline once per bench/n
            cf_dir = _find_variant_dir(cf_root, bench, n_tag, L3_MB, "mram14")
            cf_t = _get_makespan(cf_dir)
            cf_speedup = ((base_t / cf_t) - 1) * 100 if base_t and cf_t and cf_t > 0 else ""

            for v in readlat_values:
                suffix = f"_rdx{v}"
                d_dir = _find_sweep_dir(readlat_root, bench, n_tag, L3_MB, suffix)
                d_t = _get_makespan(d_dir)
                d_speedup = ((base_t / d_t) - 1) * 100 if base_t and d_t and d_t > 0 else ""

                rows.append({
                    "table_group": "table10_readlat",
                    "bench": bench,
                    "bench_short": short_bench(bench),
                    "ncores": ncores,
                    "n_tag": n_tag,
                    "l3_mb": L3_MB,
                    "scenario": "read_latency_sweep",
                    "variant": "M+DVFS",
                    "sweep_param": "read_latency_scale",
                    "sweep_value": v,
                    "cap_value_w": "",
                    "base_ms": base_t * 1e3 if base_t else "",
                    "run_ms": d_t * 1e3 if d_t else "",
                    "speedup_pct_vs_sram7": d_speedup,
                    "oracle_p_noncache_w": "",
                    "llc_reads": "",
                    "llc_writes": "",
                    "e_llc_dyn_j": "",
                    "e_llc_leak_j": "",
                    "e_nc_static_j": "",
                    "e_nc_dyn_j": "",
                    "e_total_j": "",
                    "source_dir": d_dir or "",
                    "source_variant_dirname": os.path.basename(d_dir) if d_dir else "",
                })

                rows.append({
                    "table_group": "table10_readlat",
                    "bench": bench,
                    "bench_short": short_bench(bench),
                    "ncores": ncores,
                    "n_tag": n_tag,
                    "l3_mb": L3_MB,
                    "scenario": "read_latency_sweep",
                    "variant": "CF",
                    "sweep_param": "read_latency_scale",
                    "sweep_value": v,
                    "cap_value_w": "",
                    "base_ms": base_t * 1e3 if base_t else "",
                    "run_ms": cf_t * 1e3 if cf_t else "",
                    "speedup_pct_vs_sram7": cf_speedup,
                    "oracle_p_noncache_w": "",
                    "llc_reads": "",
                    "llc_writes": "",
                    "e_llc_dyn_j": "",
                    "e_llc_leak_j": "",
                    "e_nc_static_j": "",
                    "e_nc_dyn_j": "",
                    "e_total_j": "",
                    "source_dir": cf_dir or "",
                    "source_variant_dirname": os.path.basename(cf_dir) if cf_dir else "",
                })

    # -------------------------
    # Table 12 data
    # -------------------------
    try:
        import yaml
    except Exception:
        yaml = None

    if yaml is not None:
        params_yaml = os.path.join(Path(__file__).resolve().parent, "..", "config", "params.yaml")
        if not os.path.exists(params_yaml):
            params_yaml = os.path.join(base, "..", "mx3", "config", "params.yaml")

        caps_all = {}
        if os.path.exists(params_yaml):
            try:
                with open(params_yaml) as f:
                    params = yaml.safe_load(f)
                caps_all = params.get("uarch", {}).get("sunnycove", {}).get("plm_cap_w", {})
            except Exception:
                caps_all = {}

        MAE = {1: 0.640, 4: 0.480, 8: 1.015}

        for n_tag in ["n1"]:
            n = int(n_tag[1:])
            mae = MAE[n]
            wl_caps = caps_all.get(f"n{n}", {})
            benches = sorted(
                wl for wl in wl_caps
                if isinstance(wl_caps[wl], dict) and L3_MB in wl_caps[wl]
            )

            def lbl(v):
                return f"{v:.2f}".replace(".", "p")

            for bench in benches:
                cap = wl_caps[bench][L3_MB]
                cap_minus = max(cap - mae, 1.0)
                cap_plus = cap + mae

                s7_dir = _find_variant_dir(dvfs_main_root, bench, n_tag, L3_MB, "baseline_sram_only_sram7")
                if not s7_dir:
                    s7_dir = _find_hca_rundir(hca_root, bench, L3_MB, "baseline_sram_only_sram7")
                base_t = _get_makespan(s7_dir)

                minus_dir = _find_variant_dir(mae_root, bench, n_tag, L3_MB, f"lc_c{lbl(cap_minus)}")
                plus_dir = _find_variant_dir(mae_root, bench, n_tag, L3_MB, f"lc_c{lbl(cap_plus)}")

                minus_t = _get_makespan(minus_dir)
                plus_t = _get_makespan(plus_dir)

                minus_speedup = ((base_t / minus_t) - 1) * 100 if base_t and minus_t and minus_t > 0 else ""
                plus_speedup = ((base_t / plus_t) - 1) * 100 if base_t and plus_t and plus_t > 0 else ""

                rows.append({
                    "table_group": "table12_cap_mae",
                    "bench": bench,
                    "bench_short": short_bench(bench),
                    "ncores": n,
                    "n_tag": n_tag,
                    "l3_mb": L3_MB,
                    "scenario": "cap_mae",
                    "variant": "cap_minus_mae",
                    "sweep_param": "cap_w",
                    "sweep_value": cap_minus,
                    "cap_value_w": cap_minus,
                    "base_ms": base_t * 1e3 if base_t else "",
                    "run_ms": minus_t * 1e3 if minus_t else "",
                    "speedup_pct_vs_sram7": minus_speedup,
                    "oracle_p_noncache_w": "",
                    "llc_reads": "",
                    "llc_writes": "",
                    "e_llc_dyn_j": "",
                    "e_llc_leak_j": "",
                    "e_nc_static_j": "",
                    "e_nc_dyn_j": "",
                    "e_total_j": "",
                    "source_dir": minus_dir or "",
                    "source_variant_dirname": os.path.basename(minus_dir) if minus_dir else "",
                })

                rows.append({
                    "table_group": "table12_cap_mae",
                    "bench": bench,
                    "bench_short": short_bench(bench),
                    "ncores": n,
                    "n_tag": n_tag,
                    "l3_mb": L3_MB,
                    "scenario": "cap_mae",
                    "variant": "cap_plus_mae",
                    "sweep_param": "cap_w",
                    "sweep_value": cap_plus,
                    "cap_value_w": cap_plus,
                    "base_ms": base_t * 1e3 if base_t else "",
                    "run_ms": plus_t * 1e3 if plus_t else "",
                    "speedup_pct_vs_sram7": plus_speedup,
                    "oracle_p_noncache_w": "",
                    "llc_reads": "",
                    "llc_writes": "",
                    "e_llc_dyn_j": "",
                    "e_llc_leak_j": "",
                    "e_nc_static_j": "",
                    "e_nc_dyn_j": "",
                    "e_total_j": "",
                    "source_dir": plus_dir or "",
                    "source_variant_dirname": os.path.basename(plus_dir) if plus_dir else "",
                })

    return rows


def write_csv(rows, out_csv: str):
    out_path = Path(os.path.expanduser(out_csv))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "table_group",
        "bench",
        "bench_short",
        "ncores",
        "n_tag",
        "l3_mb",
        "scenario",
        "variant",
        "sweep_param",
        "sweep_value",
        "cap_value_w",
        "base_ms",
        "run_ms",
        "speedup_pct_vs_sram7",
        "oracle_p_noncache_w",
        "llc_reads",
        "llc_writes",
        "e_llc_dyn_j",
        "e_llc_leak_j",
        "e_nc_static_j",
        "e_nc_dyn_j",
        "e_total_j",
        "source_dir",
        "source_variant_dirname",
    ]

    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row)

    print(f"Wrote {len(rows)} rows to {out_path}")


def main():
    args = parse_args()
    base = os.path.abspath(os.path.expanduser(args.base))
    rows = build_rows(base)
    write_csv(rows, args.out)


if __name__ == "__main__":
    main()