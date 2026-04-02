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


def get_delta(db_path, obj, metric, core=0):
    end = get_stat(db_path, obj, metric, "roi-end", core)
    begin = get_stat(db_path, obj, metric, "roi-begin", core)
    if end is not None and begin is not None:
        return end - begin
    return end


def extract_metrics(db):
    """Return a dict of key metrics from a sim.stats.sqlite3 file."""
    loads = get_delta(db, "L3", "loads")
    misses = get_delta(db, "L3", "load-misses")
    stores = get_delta(db, "L3", "stores")
    store_misses = get_delta(db, "L3", "store-misses")
    dram_rd = get_delta(db, "dram", "reads")
    dram_wr = get_delta(db, "dram", "writes")

    thread_elapsed_fs = get_delta(db, "thread", "elapsed_time")
    perf_elapsed_fs = get_delta(db, "performance_model", "elapsed_time")

    elapsed_fs = None
    elapsed_time_source = ""
    if thread_elapsed_fs is not None and thread_elapsed_fs > 0:
        elapsed_fs = thread_elapsed_fs
        elapsed_time_source = "thread.elapsed_time"
    elif perf_elapsed_fs is not None and perf_elapsed_fs > 0:
        elapsed_fs = perf_elapsed_fs
        elapsed_time_source = "performance_model.elapsed_time"

    inst = get_delta(db, "performance_model", "instruction_count")

    elapsed_ns = elapsed_fs / 1e6 if elapsed_fs is not None and elapsed_fs > 0 else None
    miss_rate = (misses / loads * 100) if loads and loads > 0 and misses is not None else None
    ipc_val = (inst / elapsed_ns) if inst and elapsed_ns and elapsed_ns > 0 else None  # inst/ns

    # Per-technology hit counts
    l3_rh_sram = get_delta(db, "L3", "l3_read_hits_sram")
    l3_rh_mram = get_delta(db, "L3", "l3_read_hits_mram")
    l3_wh_sram = get_delta(db, "L3", "l3_write_hits_sram")
    l3_wh_mram = get_delta(db, "L3", "l3_write_hits_mram")

    # Energy (picojoules)
    llc_dyn_energy_pJ = get_delta(db, "L3", "llc_dyn_energy_pJ")
    llc_dyn_energy_pJ_sram = get_delta(db, "L3", "llc_dyn_energy_pJ_sram")
    llc_dyn_energy_pJ_mram = get_delta(db, "L3", "llc_dyn_energy_pJ_mram")
    llc_leakage_energy_pJ = get_delta(db, "L3", "llc_leakage_energy_pJ")

    # Migration stats
    hybrid_promotions = get_delta(db, "L3", "hybrid_promotions")
    hybrid_swaps = get_delta(db, "L3", "hybrid_swaps")
    mram_write_bytes = get_delta(db, "L3", "mram_write_bytes")

    return {
        "elapsed_ns": elapsed_ns,
        "elapsed_time_source": elapsed_time_source,
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
    """500_perlbench_r_roi1000M_warm200M → perlbench"""
    parts = name.split("_")
    return parts[1] if len(parts) >= 2 else name


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
    """Walk all studies, preferring _fix dirs. Returns list of row dicts."""
    rows = []
    
    # Enumerate all study directories (both originals and fixes)
    study_dirs = set()
    for p in sorted(base.iterdir()):
        if not p.is_dir():
            continue
        # Normalize: strip _fix suffix to get the canonical study name
        canonical = p.name.replace("_fix", "")
        study_dirs.add(canonical)

    for study in sorted(study_dirs):
        fix_dir = base / f"{study}_fix"
        orig_dir = base / study

        # Determine all leaf run dirs (could be flat or nested with sub-studies)
        candidates = []

        for root_dir in [orig_dir, fix_dir]:
            if not root_dir.is_dir():
                continue
            runs_dir = root_dir / "runs"
            if runs_dir.is_dir():
                # Flat study (e.g. 2_static_policy/runs/)
                candidates.append((root_dir, runs_dir, ""))
            else:
                # Nested study (e.g. 1_cross_node_fix/sram14/runs/ or 6_*/6a_*/runs/)
                for sub in sorted(root_dir.iterdir()):
                    if not sub.is_dir() or sub.name in ("slurm", "jobs.txt"):
                        continue
                    sub_runs = sub / "runs"
                    if sub_runs.is_dir():
                        candidates.append((sub, sub_runs, sub.name))

        # Deduplicate: for each (wl, sz, variant), prefer _fix over original
        seen = {}  # (wl, sz, raw_variant) -> (db, sub_label, is_fix)
        for root_dir, runs_dir, sub_label in candidates:
            is_fix = "_fix" in str(root_dir)
            for wl, sz, raw_var, db in walk_study(runs_dir):
                key = (wl, sz, raw_var)
                if key not in seen or is_fix:
                    seen[key] = (db, sub_label, is_fix)

        # Build rows
        for (wl, sz, raw_var), (db, sub_label, _) in sorted(seen.items()):
            parsed = parse_variant(raw_var)
            metrics = extract_metrics(db)

            # Extract latency scale from sub_label if present (e.g. lat_2x, rd_3x)
            lat_scale = ""
            m = re.match(r"(?:lat|rd)_(\d+)x", sub_label)
            if m:
                lat_scale = m.group(1) + "x"

            row = {
                "workload": short_wl(wl),
                "capacity": sz,
                "device": parsed["device"],
                "lat_scale": lat_scale,
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
        default=str(Path("~/COSC_498/miniMXE/repro/hca/hca_sunnycove").expanduser()),
        help="Root of HCA results"
    )
    parser.add_argument(
    "--out",
    default=str(Path("~/COSC_498/miniMXE/repro/agg/hca.csv").expanduser()),
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