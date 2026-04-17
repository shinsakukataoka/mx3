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
        default=str(Path("/home/skataoka26/COSC_498/miniMXE/repro/hca/hca_multicore/1_multicore_hca/runs").expanduser()),
        help="Base multicore HCA runs directory.",
    )
    ap.add_argument(
        "--out",
        default=str(Path("/home/skataoka26/COSC_498/miniMXE/repro/agg/hca_multicore_workload.csv").expanduser()),
        help="Output CSV path.",
    )
    return ap.parse_args()

def _get_stat(db_path: str, obj: str, metric: str,
              prefix: str = "roi-end", core: int = 0) -> Optional[float]:
    """
    Backward-compatible single-core getter.
    Kept for compatibility with any old callers; multicore-aware extraction below
    uses per-core aggregation helpers inside extract_hca_metrics().
    """
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
    """
    Backward-compatible single-core delta getter.
    Multicore-aware extraction below does not rely on this function, but we keep
    the signature stable for any other callers.
    """
    end = _get_stat(db_path, obj, metric, "roi-end", core)
    begin = _get_stat(db_path, obj, metric, "roi-begin", core)
    if end is not None and begin is not None:
        return end - begin
    return end


def short_wl(name: str) -> str:
    """
    Preserve multicore workload identity.

    Examples
    --------
    502_gcc_r                                  -> gcc
    505_mcf_r+505_mcf_r+502_gcc_r+502_gcc_r    -> mcfx2+gccx2
    557_xz_r+557_xz_r+557_xz_r+557_xz_r        -> xzx4
    """
    parts = str(name).split("+")
    cleaned = []
    for p in parts:
        p = p.replace(".", "_")
        toks = p.split("_")
        if len(toks) >= 2:
            cleaned.append(toks[1])
        else:
            cleaned.append(p)

    counts = {}
    order = []
    for c in cleaned:
        if c not in counts:
            counts[c] = 0
            order.append(c)
        counts[c] += 1

    if len(order) == 1 and counts[order[0]] == 1:
        return order[0]

    return "+".join(f"{k}x{counts[k]}" for k in order)

def get_hca_config(vd: str) -> Optional[str]:
    """
    Map a raw variant directory name to a compact config label.
    """
    if vd.startswith("baseline_mram_only"):
        return "MRAM"
    if vd.startswith("baseline_sram_only"):
        return "SRAM"

    base = vd
    for suffix in [
        "_sram14_mram14", "_sram14", "_mram14",
        "_sram32_mram32", "_sram32", "_mram32",
        "_sram7_mram14", "_sram7_mram32", "_sram7",
    ]:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break

    mapping = {
        "noparity_s4_fillmram": "S4",
        "noparity_s8_fillmram": "S8",
        "noparity_s12_fillmram": "S12",
        "noparity_s4_fillmram_p4_c32": "P4C32",
        "noparity_s4_fillmram_p1_c0": "P1C0",
        "noparity_s4_fillmram_rf": "S4_RF",
        "noparity_s8_fillmram_rf": "S8_RF",
        "noparity_s12_fillmram_rf": "S12_RF",
        "noparity_s4_fillmram_rf_p4_c32": "P4C32_RF",
        "noparity_s4_fillmram_rf_p1_c0": "P1C0_RF",
    }
    return mapping.get(base)
    
def extract_hca_metrics(db_path: str, expected_cores: Optional[int] = None) -> dict:
    def _fetch_by_core(obj: str, metric: str, prefix: str) -> dict[int, float]:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            c = conn.cursor()
            c.execute(
                '''SELECT v.core, v.value
                   FROM "values" v
                   JOIN names n ON v.nameid = n.nameid
                   JOIN prefixes p ON v.prefixid = p.prefixid
                   WHERE n.objectname = ?
                     AND n.metricname = ?
                     AND p.prefixname = ?
                   ORDER BY v.core''',
                (obj, metric, prefix),
            )
            rows = c.fetchall()
            conn.close()
            return {int(core): float(value) for core, value in rows}
        except Exception:
            return {}

    def _delta_by_core(obj: str, metric: str) -> dict[int, float]:
        begin = _fetch_by_core(obj, metric, "roi-begin")
        end = _fetch_by_core(obj, metric, "roi-end")
        cores = sorted(set(begin) | set(end))
        out = {}
        for core in cores:
            e = end.get(core)
            b = begin.get(core)
            if e is not None and b is not None:
                out[core] = e - b
            elif e is not None:
                out[core] = e
        return out

    def _sum_delta(obj: str, metric: str) -> Optional[float]:
        vals = _delta_by_core(obj, metric)
        return sum(vals.values()) if vals else None

    def _fs_to_s_map(d: dict[int, float]) -> dict[int, float]:
        return {c: v * 1e-15 for c, v in d.items() if v is not None and v > 0}

    def _contiguous_prefix(vals: dict[int, float]) -> bool:
        if not vals:
            return False
        cores = sorted(vals)
        return cores == list(range(len(cores)))

    def _score_source(vals: dict[int, float], expected_cores: Optional[int] = None) -> float:
        if not vals:
            return float("inf")

        ts = list(vals.values())
        score = 0.0

        if expected_cores is not None:
            score += 1000.0 * abs(len(ts) - expected_cores)

        if not _contiguous_prefix(vals):
            score += 500.0

        mx = max(ts)
        mn = min(ts)

        if mx < 0.02:
            score += 500.0

        if mx - mn < 1e-12:
            score += 300.0

        if mn < 1e-6:
            score += 100.0

        return score

    # choose sane elapsed-time source
    thread_fs = _delta_by_core("thread", "elapsed_time")
    perf_fs = _delta_by_core("performance_model", "elapsed_time")

    thread_s = _fs_to_s_map(thread_fs)
    perf_s = _fs_to_s_map(perf_fs)

    thread_score = _score_source(thread_s, expected_cores=expected_cores)
    perf_score = _score_source(perf_s, expected_cores=expected_cores)

    if thread_score <= perf_score:
        chosen_times = thread_s
        elapsed_time_source = "thread.elapsed_time"
    else:
        chosen_times = perf_s
        elapsed_time_source = "performance_model.elapsed_time"

    elapsed_ns = (max(chosen_times.values()) * 1e9) if chosen_times else None

    # aggregate counters
    l3_loads = _sum_delta("L3", "loads")
    l3_load_misses = _sum_delta("L3", "load-misses")
    l3_stores = _sum_delta("L3", "stores")
    l3_store_misses = _sum_delta("L3", "store-misses")
    instruction_count = _sum_delta("performance_model", "instruction_count")
    mram_write_bytes = _sum_delta("L3", "mram_write_bytes")
    hybrid_promotions = _sum_delta("L3", "hybrid_promotions")
    l3_rh_sram = _sum_delta("L3", "l3_read_hits_sram")
    l3_rh_mram = _sum_delta("L3", "l3_read_hits_mram")
    l3_wh_sram = _sum_delta("L3", "l3_write_hits_sram")
    l3_wh_mram = _sum_delta("L3", "l3_write_hits_mram")

    throughput_inst_ns = (
        instruction_count / elapsed_ns
        if instruction_count is not None and elapsed_ns is not None and elapsed_ns > 0
        else None
    )

    total_hits = 0.0
    for x in [l3_rh_sram, l3_rh_mram, l3_wh_sram, l3_wh_mram]:
        total_hits += float(x or 0.0)

    total_write_hits = float(l3_wh_sram or 0.0) + float(l3_wh_mram or 0.0)
    write_frac_hits = (100.0 * total_write_hits / total_hits) if total_hits > 0 else None

    miss_rate = (
        100.0 * l3_load_misses / l3_loads
        if l3_loads is not None and l3_loads > 0 and l3_load_misses is not None
        else None
    )

    return {
        "elapsed_ns": elapsed_ns,
        "elapsed_time_source": elapsed_time_source,
        "instruction_count": instruction_count or 0,
        "throughput_inst_ns": throughput_inst_ns,
        "l3_loads": l3_loads or 0,
        "l3_load_misses": l3_load_misses or 0,
        "l3_stores": l3_stores or 0,
        "l3_store_misses": l3_store_misses or 0,
        "l3_rh_sram": l3_rh_sram or 0,
        "l3_rh_mram": l3_rh_mram or 0,
        "l3_wh_sram": l3_wh_sram or 0,
        "l3_wh_mram": l3_wh_mram or 0,
        "write_frac_hits": write_frac_hits,
        "miss_rate": miss_rate,
        "mram_write_bytes": mram_write_bytes or 0,
        "hybrid_promotions": hybrid_promotions or 0,
    }

def collect_hca_rows(base: str):
    rows = []
    root = Path(base)

    if not root.is_dir():
        raise FileNotFoundError(f"HCA root not found: {root}")

    for wl_dir in sorted(root.iterdir()):
        if not wl_dir.is_dir():
            continue

        for n_dir in sorted(wl_dir.iterdir()):
            if not n_dir.is_dir():
                continue

            m_nc = re.match(r"n(\d+)$", n_dir.name)
            expected_cores = int(m_nc.group(1)) if m_nc else None

            for cap_dir in sorted(n_dir.iterdir()):
                if not cap_dir.is_dir():
                    continue

                m_sz = re.match(r"l3_(\d+)MB$", cap_dir.name)
                if not m_sz:
                    continue
                size_mb = int(m_sz.group(1))

                for var_dir in sorted(cap_dir.iterdir()):
                    if not var_dir.is_dir():
                        continue

                    db_path = var_dir / "sim.stats.sqlite3"
                    if not db_path.exists():
                        continue

                    cfg = get_hca_config(var_dir.name)
                    if not cfg:
                        continue

                    bench_raw = wl_dir.name
                    workload = short_wl(bench_raw)
                    metrics = extract_hca_metrics(str(db_path), expected_cores=expected_cores)

                    rows.append({
                        "workload": workload,
                        "bench_raw": bench_raw,
                        "size_mb": size_mb,
                        "n_cores": expected_cores,
                        "cfg": cfg,
                        "run_dir": str(var_dir),
                        "elapsed_ns": metrics["elapsed_ns"],
                        "elapsed_time_source": metrics["elapsed_time_source"],
                        "instruction_count": metrics["instruction_count"],
                        "throughput_inst_ns": metrics["throughput_inst_ns"],
                        "l3_loads": metrics["l3_loads"],
                        "l3_load_misses": metrics["l3_load_misses"],
                        "l3_stores": metrics["l3_stores"],
                        "l3_store_misses": metrics["l3_store_misses"],
                        "l3_rh_sram": metrics["l3_rh_sram"],
                        "l3_rh_mram": metrics["l3_rh_mram"],
                        "l3_wh_sram": metrics["l3_wh_sram"],
                        "l3_wh_mram": metrics["l3_wh_mram"],
                        "miss_rate": metrics["miss_rate"],
                        "write_frac_hits": metrics["write_frac_hits"],
                        "mram_write_bytes": metrics["mram_write_bytes"],
                        "hybrid_promotions": int(metrics["hybrid_promotions"]),
                    })

    rows.sort(key=lambda r: (r["workload"], r["size_mb"], r["n_cores"], r["cfg"], r["bench_raw"]))
    return rows

def write_csv(rows, out_path: str):
    out_path = Path(out_path).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "workload",
        "bench_raw",
        "size_mb",
        "n_cores",
        "cfg",
        "run_dir",
        "elapsed_ns",
        "elapsed_time_source",
        "instruction_count",
        "throughput_inst_ns",
        "l3_loads",
        "l3_load_misses",
        "l3_stores",
        "l3_store_misses",
        "l3_rh_sram",
        "l3_rh_mram",
        "l3_wh_sram",
        "l3_wh_mram",
        "miss_rate",
        "write_frac_hits",
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