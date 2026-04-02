#!/usr/bin/env python3
"""
agg_hca_workload_csv.py

Aggregate HCA run-level metrics from:
    repro/hca/hca_sunnycove/

and write a single CSV to:
    ~/COSC_498/miniMXE/repro/agg/hca_workload.csv

Fields written per run:
    workload
    bench_raw
    size_mb
    cfg
    run_dir
    elapsed_ns
    instruction_count
    throughput_inst_ns
    l3_load_misses
    mram_write_bytes
    hybrid_promotions

Usage:
    python3 agg_hca_workload_csv.py

Optional:
    python3 agg_hca_workload_csv.py --base ~/COSC_498/miniMXE/repro
    python3 agg_hca_workload_csv.py --out ~/COSC_498/miniMXE/repro/agg/hca_workload.csv
"""

import argparse
import csv
import os
import re
import sqlite3
from pathlib import Path
from typing import Optional


WKLDS_ALL = [
    "mcf", "xalancbmk", "perlbench", "omnetpp", "gcc", "xz",
    "deepsjeng", "leela", "fotonik3d", "exchange2",
]


def parse_args():
    ap = argparse.ArgumentParser(description="Aggregate HCA workload metrics into one CSV.")
    ap.add_argument(
        "--base",
        default=str(Path("~/COSC_498/miniMXE/repro").expanduser()),
        help="Base repro directory. Default: ~/COSC_498/miniMXE/repro",
    )
    ap.add_argument(
        "--out",
        default=str(Path("~/COSC_498/miniMXE/repro/agg/hca_workload.csv").expanduser()),
        help="Output CSV path. Default: ~/COSC_498/miniMXE/repro/agg/hca_workload.csv",
    )
    return ap.parse_args()


def _get_stat(db_path: str, obj: str, metric: str,
              prefix: str = "roi-end", core: int = 0) -> Optional[float]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        c = conn.cursor()
        c.execute(
            '''SELECT v.value
               FROM "values" v
               JOIN names n ON v.nameid = n.nameid
               JOIN prefixes p ON v.prefixid = p.prefixid
               WHERE n.objectname = ?
                 AND n.metricname = ?
                 AND p.prefixname = ?
                 AND v.core = ?''',
            (obj, metric, prefix, core),
        )
        row = c.fetchone()
        conn.close()
        return float(row[0]) if row else None
    except Exception:
        return None


def get_delta(db_path: str, obj: str, metric: str, core: int = 0) -> Optional[float]:
    end = _get_stat(db_path, obj, metric, "roi-end", core)
    begin = _get_stat(db_path, obj, metric, "roi-begin", core)
    if end is not None and begin is not None:
        return end - begin
    return end


def short_wl(name: str) -> str:
    parts = name.split("_")
    for p in parts:
        if p in WKLDS_ALL:
            return p
    return parts[1] if len(parts) >= 2 else name


def get_hca_config(vd: str) -> Optional[str]:
    vd = vd.replace("_sram14_mram14", "").replace("_sram14", "").replace("_mram14", "")
    mapping = {
        "mram14": "MRAM",
        "baseline_mram_only_mram14": "MRAM",
        "baseline_mram_only": "MRAM",
        "noparity_s4_fillmram": "S4",
        "noparity_s4_fillmram_p4_c32": "P4C32",
        "noparity_s4_fillmram_p1_c0": "P1C0",
        "noparity_s4_fillmram_rf": "S4_RF",
        "noparity_s4_fillmram_rf_p4_c32": "P4C32_RF",
        "noparity_s4_fillmram_rf_p1_c0": "P1C0_RF",
    }
    return mapping.get(vd)


def extract_hca_metrics(db_path: str) -> dict:
    l3_load_misses = get_delta(db_path, "L3", "load-misses") or 0
    mram_write_bytes = get_delta(db_path, "L3", "mram_write_bytes") or 0
    hybrid_promotions = get_delta(db_path, "L3", "hybrid_promotions") or 0
    instruction_count = get_delta(db_path, "performance_model", "instruction_count") or 0

    thread_fs = get_delta(db_path, "thread", "elapsed_time")
    perf_fs = get_delta(db_path, "performance_model", "elapsed_time")
    fs = thread_fs if (thread_fs and thread_fs > 0) else perf_fs
    elapsed_ns = fs / 1e6 if fs else None  # fs -> ns

    throughput_inst_ns = (
        instruction_count / elapsed_ns
        if instruction_count and elapsed_ns and elapsed_ns > 0
        else None
    )

    return {
        "elapsed_ns": elapsed_ns,
        "instruction_count": instruction_count,
        "throughput_inst_ns": throughput_inst_ns,
        "l3_load_misses": l3_load_misses,
        "mram_write_bytes": mram_write_bytes,
        "hybrid_promotions": hybrid_promotions,
    }


def collect_hca_rows(base: str):
    rows = []
    hca_root = os.path.join(base, "hca", "hca_sunnycove")

    if not os.path.isdir(hca_root):
        raise FileNotFoundError(f"HCA root not found: {hca_root}")

    for root, _, files in os.walk(hca_root):
        if "sim.stats.sqlite3" not in files:
            continue

        parts = root.split(os.sep)
        vd = parts[-1]

        bench_raw = None
        size_mb = None

        for p in parts:
            if p.startswith("sz") and p.endswith("M"):
                try:
                    size_mb = int(p[2:-1])
                except ValueError:
                    pass

            if "roi" in p or re.match(r"\d+\.\w", p):
                bench_raw = p.split("_roi")[0].replace(".", "_", 1)

        if not bench_raw or size_mb is None:
            continue

        cfg = get_hca_config(vd)
        if not cfg:
            continue

        db_path = os.path.join(root, "sim.stats.sqlite3")
        metrics = extract_hca_metrics(db_path)

        rows.append({
            "workload": short_wl(bench_raw),
            "bench_raw": bench_raw,
            "size_mb": size_mb,
            "cfg": cfg,
            "run_dir": root,
            "elapsed_ns": metrics["elapsed_ns"],
            "instruction_count": metrics["instruction_count"],
            "throughput_inst_ns": metrics["throughput_inst_ns"],
            "l3_load_misses": metrics["l3_load_misses"],
            "mram_write_bytes": metrics["mram_write_bytes"],
            "hybrid_promotions": int(metrics["hybrid_promotions"]),
        })

    rows.sort(key=lambda r: (r["workload"], r["size_mb"], r["cfg"], r["bench_raw"]))
    return rows


def write_csv(rows, out_path: str):
    out_path = Path(out_path).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "workload",
        "bench_raw",
        "size_mb",
        "cfg",
        "run_dir",
        "elapsed_ns",
        "instruction_count",
        "throughput_inst_ns",
        "l3_load_misses",
        "mram_write_bytes",
        "hybrid_promotions",
    ]

    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    args = parse_args()
    base = str(Path(args.base).expanduser().resolve())
    out = str(Path(args.out).expanduser())

    rows = collect_hca_rows(base)
    write_csv(rows, out)

    print(f"[INFO] Wrote {len(rows)} rows to {out}")


if __name__ == "__main__":
    main()