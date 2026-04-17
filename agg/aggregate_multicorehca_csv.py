#!/usr/bin/env python3
"""
export_hca_csv.py — Export all HCA simulation results into a flat, structured CSV.

prefers '_fix' data when available, decomposes raw directory names
into clean columnar fields, and writes one CSV row per simulation.

Usage:
    python3 scripts/export_hca_csv.py [--base DIR] [--out FILE]
"""

import sqlite3, os, sys, argparse, csv, re, math
from pathlib import Path

try:
    import yaml as _yaml
except ImportError:
    _yaml = None

def _cheap_yaml_load(text):
    """Minimal YAML parser for run.yaml (flat key: value only)."""
    if _yaml:
        return _yaml.safe_load(text)
    # Fallback: parse simple nested YAML
    import json
    root = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith('#'):
            continue
        indent = len(raw) - len(raw.lstrip())
        m = re.match(r'^\s*([A-Za-z0-9_./-]+):\s*(.*)$', raw)
        if not m:
            continue
        key, rest = m.group(1), m.group(2).strip().strip('"').strip("'")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else root
        if rest == '' or rest in ('|', '>'):
            newd = {}
            parent[key] = newd
            stack.append((indent, newd))
        else:
            if rest.lower() == 'true': parent[key] = True
            elif rest.lower() == 'false': parent[key] = False
            elif rest.lower() == 'null' or rest == '~': parent[key] = None
            else:
                try: parent[key] = int(rest)
                except ValueError:
                    try: parent[key] = float(rest)
                    except ValueError: parent[key] = rest
    return root

# ── DB helpers ───────────────────────────────────────────────────────────
def get_stat(db_path, obj, metric, prefix="roi-end", core=0):
    """
    Backward-compatible single-core getter.
    Kept so existing call sites do not break, but multicore-aware code below
    should prefer get_stats_by_core()/get_delta_by_core().
    """
    conn = sqlite3.connect(str(db_path))
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

def extract_times(db, expected_cores=None):
    """Return per-core ROI times and a makespan-style scalar runtime.

    For multicore exports, thread.elapsed_time can be bogus for some runs
    (e.g. identical tiny values on all cores). We therefore compute both
    thread/performance_model deltas, score them, and choose the safer source.
    """

    def _fetch_by_core(obj, metric, prefix):
        conn = sqlite3.connect(str(db))
        c = conn.cursor()
        c.execute(
            '''SELECT v.core, v.value
               FROM "values" v
               JOIN names n ON v.nameid = n.nameid
               JOIN prefixes p ON v.prefixid = p.prefixid
               WHERE n.objectname=? AND n.metricname=? AND p.prefixname=?
               ORDER BY v.core''',
            (obj, metric, prefix),
        )
        rows = c.fetchall()
        conn.close()
        return {int(core): float(value) for core, value in rows}

    def _delta_by_core(obj, metric):
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

    def _fs_to_s_map(d):
        return {c: v * 1e-15 for c, v in d.items() if v is not None and v > 0}

    def _contiguous_prefix(vals):
        if not vals:
            return False
        cores = sorted(vals)
        return cores == list(range(len(cores)))

    def _score_source(vals, expected_cores=None):
        """
        Lower score is better.
        Penalize:
          - missing/extra cores vs expected
          - non-contiguous core ids
          - absurdly tiny times
          - all cores having exactly the same time
        """
        if not vals:
            return float("inf")

        ts = list(vals.values())
        cores = sorted(vals)

        score = 0.0

        if expected_cores is not None:
            score += 1000.0 * abs(len(ts) - expected_cores)

        if not _contiguous_prefix(vals):
            score += 500.0

        mx = max(ts)
        mn = min(ts)

        # suspicious if everything is tiny
        if mx < 0.02:
            score += 500.0

        # suspicious if all cores are numerically identical
        if max(ts) - min(ts) < 1e-12:
            score += 300.0

        # suspicious if one or more cores are effectively zero
        if mn < 1e-6:
            score += 100.0

        return score

    thread_times_fs = _delta_by_core("thread", "elapsed_time")
    perf_times_fs = _delta_by_core("performance_model", "elapsed_time")

    thread_s = _fs_to_s_map(thread_times_fs)
    perf_s = _fs_to_s_map(perf_times_fs)

    thread_score = _score_source(thread_s, expected_cores=expected_cores)
    perf_score   = _score_source(perf_s,   expected_cores=expected_cores)

    # Prefer thread only if it is at least as sane as perf
    if thread_score <= perf_score:
        chosen = thread_s
        source_name = "thread.elapsed_time"
    else:
        chosen = perf_s
        source_name = "performance_model.elapsed_time"

    times_s = [chosen[c] for c in sorted(chosen)]
    roi_time_s = max(times_s) if times_s else None

    return {
        "n_cores": len(times_s),
        "times_s": "|".join(f"{x:.9f}" for x in times_s) if times_s else "",
        "roi_time_s": roi_time_s,
        "elapsed_time_source": source_name,
    }

def get_delta(db_path, obj, metric, core=0):
    """
    Backward-compatible single-core delta getter.
    Multicore-aware extraction below does not rely on this function, but we keep
    the signature stable for any other callers.
    """
    end = get_stat(db_path, obj, metric, "roi-end", core)
    begin = get_stat(db_path, obj, metric, "roi-begin", core)
    if end is not None and begin is not None:
        return end - begin
    return end
def extract_metrics(db, expected_cores=None):
    """Return a dict of key metrics from a sim.stats.sqlite3 file, multicore-aware."""

    def _fetch_by_core(obj, metric, prefix):
        conn = sqlite3.connect(str(db))
        c = conn.cursor()
        c.execute(
            '''SELECT v.core, v.value
               FROM "values" v
               JOIN names n ON v.nameid = n.nameid
               JOIN prefixes p ON v.prefixid = p.prefixid
               WHERE n.objectname=? AND n.metricname=? AND p.prefixname=?
               ORDER BY v.core''',
            (obj, metric, prefix),
        )
        rows = c.fetchall()
        conn.close()
        return {int(core): float(value) for core, value in rows}

    def _delta_by_core(obj, metric):
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

    def _sum_delta(obj, metric):
        vals = _delta_by_core(obj, metric)
        return sum(vals.values()) if vals else None

    time_info = extract_times(db, expected_cores=expected_cores)
    roi_time_s = time_info["roi_time_s"]
    elapsed_ns = roi_time_s * 1e9 if roi_time_s is not None and roi_time_s > 0 else None

    loads = _sum_delta("L3", "loads")
    misses = _sum_delta("L3", "load-misses")
    stores = _sum_delta("L3", "stores")
    store_misses = _sum_delta("L3", "store-misses")
    dram_rd = _sum_delta("dram", "reads")
    dram_wr = _sum_delta("dram", "writes")

    inst = _sum_delta("performance_model", "instruction_count")

    miss_rate = (misses / loads * 100) if loads is not None and loads > 0 and misses is not None else None
    ipc_val = (inst / elapsed_ns) if inst is not None and elapsed_ns is not None and elapsed_ns > 0 else None  # inst/ns

    l3_rh_sram = _sum_delta("L3", "l3_read_hits_sram")
    l3_rh_mram = _sum_delta("L3", "l3_read_hits_mram")
    l3_wh_sram = _sum_delta("L3", "l3_write_hits_sram")
    l3_wh_mram = _sum_delta("L3", "l3_write_hits_mram")

    llc_dyn_energy_pJ = _sum_delta("L3", "llc_dyn_energy_pJ")
    llc_dyn_energy_pJ_sram = _sum_delta("L3", "llc_dyn_energy_pJ_sram")
    llc_dyn_energy_pJ_mram = _sum_delta("L3", "llc_dyn_energy_pJ_mram")
    llc_leakage_energy_pJ = _sum_delta("L3", "llc_leakage_energy_pJ")

    hybrid_promotions = _sum_delta("L3", "hybrid_promotions")
    hybrid_swaps = _sum_delta("L3", "hybrid_swaps")
    mram_write_bytes = _sum_delta("L3", "mram_write_bytes")

    return {
        "n_cores": time_info["n_cores"],
        "times_s": time_info["times_s"],
        "roi_time_s": roi_time_s,
        "elapsed_ns": elapsed_ns,
        "elapsed_time_source": time_info["elapsed_time_source"],
        "instructions": inst,
        "ipc": ipc_val,
        "l3_loads": loads,
        "l3_load_misses": misses,
        "l3_miss_rate": miss_rate,
        "l3_stores": stores,
        "l3_store_misses": store_misses,
        "l3_rh_sram": l3_rh_sram,
        "l3_rh_mram": l3_rh_mram,
        "l3_wh_sram": l3_wh_sram,
        "l3_wh_mram": l3_wh_mram,
        "llc_dyn_energy_pJ": llc_dyn_energy_pJ,
        "llc_dyn_energy_pJ_sram": llc_dyn_energy_pJ_sram,
        "llc_dyn_energy_pJ_mram": llc_dyn_energy_pJ_mram,
        "llc_leakage_energy_pJ": llc_leakage_energy_pJ,
        "hybrid_promotions": hybrid_promotions,
        "hybrid_swaps": hybrid_swaps,
        "mram_write_bytes": mram_write_bytes,
        "dram_reads": dram_rd,
        "dram_writes": dram_wr,
    }

# ── variant parser ───────────────────────────────────────────────────────

DEVICE_TAGS = [
    "_sram14_mram14_r2x_w1x", "_sram14_mram14_r3x_w1x",
    "_sram14_mram14_r4x_w1x", "_sram14_mram14_r5x_w1x",
    "_sram14_mram14_r2x_w2x", "_sram14_mram14_r3x_w3x",
    "_sram14_mram14_r4x_w4x", "_sram14_mram14_r5x_w5x",
    "_sram14_mram14_rd2x", "_sram14_mram14_rd3x",
    "_sram14_mram14_rd4x", "_sram14_mram14_rd5x",
    "_mram14_rd2x", "_mram14_rd3x",
    "_mram14_rd4x", "_mram14_rd5x",
    "_sram14_mram14", "_sram7_mram14", "_sram32_mram14",
    "_sram14_mram32", "_sram7_mram32", "_sram32_mram32",
    "_rwhca45", "_apm22",
    "_mram14", "_mram32", "_sram14", "_sram7", "_sram32",
]


def parse_variant(raw_dir_name):
    """Decompose a raw variant directory name into structured fields."""
    name = raw_dir_name

    # 1. Extract device tag
    device = ""
    for tag in DEVICE_TAGS:
        if name.endswith(tag):
            device = tag.lstrip("_")
            name = name[: -len(tag)]
            break

    # 2. Baselines (non-HCA)
    if name.startswith("baseline_"):
        return {
            "device": device,
            "hca": "no",
            "way_policy": "",
            "static_or_migration": "",
            "restricted_fill": "",
            "s": "",
            "p": "",
            "c": "",
            "variant_raw": raw_dir_name,
        }

    # Pure tech baselines (e.g. raw dir = "sram14", "mram32")
    if name in ("", "sram7", "sram14", "sram32", "mram14", "mram32"):
        if not device:
            device = raw_dir_name  # the whole name IS the device
        return {
            "device": device,
            "hca": "no",
            "way_policy": "",
            "static_or_migration": "",
            "restricted_fill": "",
            "s": "",
            "p": "",
            "c": "",
            "variant_raw": raw_dir_name,
        }

    # 3. HCA variants
    # Pattern: {noparity|grid}_s{N}_fillmram[_rf][_p{N}_c{N}]
    way_policy = ""
    if name.startswith("noparity_"):
        way_policy = "noparity"
        name = name[len("noparity_"):]
    elif name.startswith("grid_"):
        way_policy = "grid"
        name = name[len("grid_"):]

    # Extract s parameter
    s_val = ""
    m = re.match(r"s(\d+)_fillmram(.*)", name)
    if m:
        s_val = m.group(1)
        name = m.group(2)

    # Restricted fill
    rf = "no"
    if name.startswith("_rf"):
        rf = "yes"
        name = name[3:]  # strip "_rf"

    # Migration parameters
    p_val = ""
    c_val = ""
    m = re.match(r"_p(\d+)_c(\d+)(.*)", name)
    if m:
        p_val = m.group(1)
        c_val = m.group(2)
        name = m.group(3)

    static_or_mig = "static" if not p_val else "migration"

    return {
        "device": device,
        "hca": "yes",
        "way_policy": way_policy,
        "static_or_migration": static_or_mig,
        "restricted_fill": rf,
        "s": s_val,
        "p": p_val,
        "c": c_val,
        "variant_raw": raw_dir_name,
    }


def short_wl(name):
    """
    Preserve enough of the workload identity so multicore mixes do not collapse.

    Examples
    --------
    502.gcc_r                                  -> gcc
    505.mcf_r+505.mcf_r+502.gcc_r+502.gcc_r    -> mcfx2+gccx2
    557.xz_r+557.xz_r+557.xz_r+557.xz_r        -> xzx4
    """
    parts = str(name).split("+")
    cleaned = []
    for p in parts:
        p = p.replace(".", "_")
        toks = p.split("_")
        # 502.gcc_r -> gcc
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


# ── directory walkers ────────────────────────────────────────────────────

def walk_study_legacy(runs_dir):
    """Yield (workload, size, raw_variant, db_path) from old-style layout.
    Expected: runs/<workload>/<size>/<variant>/sim.stats.sqlite3"""
    if not runs_dir.is_dir():
        return
    for wl_dir in sorted(runs_dir.iterdir()):
        if not wl_dir.is_dir():
            continue
        for sz_dir in sorted(wl_dir.iterdir()):
            if not sz_dir.is_dir():
                continue
            for var_dir in sorted(sz_dir.iterdir()):
                if not var_dir.is_dir():
                    continue
                db = var_dir / "sim.stats.sqlite3"
                if db.exists():
                    yield wl_dir.name, sz_dir.name, var_dir.name, db


def walk_study_yaml(runs_dir):
    """Yield (workload, size_label, raw_variant, db_path) from plan-hca layout.
    Expected: runs/<index>/sim.stats.sqlite3 + run.yaml"""
    if not runs_dir.is_dir():
        return
    for job_dir in sorted(runs_dir.iterdir(), key=lambda p: p.name):
        if not job_dir.is_dir():
            continue
        db = job_dir / "sim.stats.sqlite3"
        ry = job_dir / "run.yaml"
        if not db.exists():
            continue
        if ry.exists():
            y = _cheap_yaml_load(ry.read_text())
            run = y.get("run", y)
            bench = run.get("bench", run.get("workload", job_dir.name))
            # Shorten bench: "500.perlbench_r" -> "perlbench"
            wl = short_wl(bench.replace(".", "_"))
            l3_kb = run.get("l3_size_kb", 0)
            sz_mb = int(l3_kb) // 1024 if l3_kb else 0
            sz_label = f"sz{sz_mb}M"
            variant = run.get("variant", "")
            tech = run.get("tech", "")
            # Strip latency multiplier suffix from tech (e.g. sram14_mram14_rd2x -> sram14_mram14)
            lat_m = re.search(r'_rd(\d+)x$', tech)
            if lat_m:
                tech = tech[:lat_m.start()]
            # Reconstruct raw_variant with tech suffix (for parse_variant)
            raw_var = f"{variant}_{tech}" if tech and not variant.endswith(f"_{tech}") else variant
            yield wl, sz_label, raw_var, db
        else:
            # No run.yaml — skip or use dir name
            yield job_dir.name, "", "", db


def detect_layout(runs_dir):
    """Auto-detect: old-style (workload/size/variant) vs plan-hca (numbered dirs with run.yaml)."""
    if not runs_dir.is_dir():
        return "empty"
    for child in runs_dir.iterdir():
        if child.is_dir():
            if (child / "run.yaml").exists():
                return "yaml"
            # Check if child has subdirs (old layout: workload/size/variant)
            for grandchild in child.iterdir():
                if grandchild.is_dir():
                    return "legacy"
    return "legacy"


def walk_study(runs_dir):
    """Auto-detect layout and yield (workload, size, raw_variant, db_path)."""
    layout = detect_layout(runs_dir)
    if layout == "yaml":
        yield from walk_study_yaml(runs_dir)
    else:
        yield from walk_study_legacy(runs_dir)

def collect_all_runs(base):
    """Walk multicore HCA runs laid out as:
       runs/<workload>/<n_tag>/<l3_XMB>/<variant>/sim.stats.sqlite3
    """
    rows = []

    for wl_dir in sorted(base.iterdir()):
        if not wl_dir.is_dir():
            continue

        for n_dir in sorted(wl_dir.iterdir()):
            if not n_dir.is_dir():
                continue

            for cap_dir in sorted(n_dir.iterdir()):
                if not cap_dir.is_dir():
                    continue

                mcap = re.match(r"l3_(\d+)MB$", cap_dir.name)
                if not mcap:
                    continue
                capacity = f"sz{mcap.group(1)}M"

                for var_dir in sorted(cap_dir.iterdir()):
                    if not var_dir.is_dir():
                        continue

                    db = var_dir / "sim.stats.sqlite3"
                    if not db.exists():
                        continue

                    parsed = parse_variant(var_dir.name)

                    mcores = re.match(r"n(\d+)$", n_dir.name)
                    expected_cores = int(mcores.group(1)) if mcores else None
                    metrics = extract_metrics(db, expected_cores=expected_cores)

                    row = {
                        "workload": short_wl(wl_dir.name.replace(".", "_")),
                        "capacity": capacity,
                        "device": parsed["device"],
                        "lat_scale": n_dir.name,
                        "hca": parsed["hca"],
                        "way_policy": parsed["way_policy"],
                        "static_or_migration": parsed["static_or_migration"],
                        "restricted_fill": parsed["restricted_fill"],
                        "s": parsed["s"],
                        "p": parsed["p"],
                        "c": parsed["c"],
                        **metrics,
                    }
                    rows.append(row)

    return rows


# ── main ─────────────────────────────────────────────────────────────────
CSV_COLUMNS = [
    "workload", "capacity", "device", "lat_scale",
    "hca", "way_policy", "static_or_migration", "restricted_fill",
    "s", "p", "c",
    "n_cores", "times_s", "roi_time_s",
    "elapsed_ns", "elapsed_time_source", "instructions", "ipc",
    "l3_loads", "l3_load_misses", "l3_miss_rate",
    "l3_stores", "l3_store_misses",
    "l3_rh_sram", "l3_rh_mram", "l3_wh_sram", "l3_wh_mram",
    "llc_dyn_energy_pJ", "llc_dyn_energy_pJ_sram", "llc_dyn_energy_pJ_mram",
    "llc_leakage_energy_pJ",
    "hybrid_promotions", "hybrid_swaps", "mram_write_bytes",
    "dram_reads", "dram_writes",
]
def main():
    parser = argparse.ArgumentParser(description="Export HCA results to CSV")
    parser.add_argument(
        "--base",
        default=str(Path("/home/skataoka26/COSC_498/miniMXE/repro/hca/hca_multicore/1_multicore_hca/runs").expanduser()),
        help="Root of multicore HCA results"
    )
    parser.add_argument(
        "--out",
        default=str(Path("/home/skataoka26/COSC_498/miniMXE/repro/agg/hca_multicore.csv").expanduser()),
        help="Output CSV file path"
)
    args = parser.parse_args()
    base = Path(args.base).resolve()

    print(f"Scanning {base} ...")
    rows = collect_all_runs(base)
    print(f"Found {len(rows)} simulation results.")

    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    print(f"Saved to {args.out}")


if __name__ == "__main__":
    main()