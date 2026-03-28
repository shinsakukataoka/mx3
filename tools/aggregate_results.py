#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mx2 Unified Aggregation Script.

Scans a results tree for Sniper runs (directories containing sim.stats.sqlite3),
reads run.yaml + sniper.log + sqlite stats, and writes a single CSV.

Differences vs utils/aggregate_results.py:
  - Mode detection uses run.yaml keys (bench/workload/microbench/kernel), NOT directory names
  - run.yaml parsing includes run.microbench (mx2 stores microbench name under run:)
  - Base frequency fallback is parsed from cmd.info when DVFS is inactive (else default=2.66)
"""

from __future__ import annotations
import argparse, csv, os, re, sqlite3
from collections import defaultdict
from typing import Dict, Tuple, List, Optional

LINE_SIZE = 64

# ==========================================
# 1) YAML parsing (minimal + optional PyYAML)
# ==========================================
try:
    import yaml
except Exception:
    yaml = None

def _coerce_scalar(val: str):
    v = val.strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        v = v[1:-1]
    low = v.lower()
    if low == 'true': return True
    if low == 'false': return False
    try:
        if any(ch in v for ch in ('.', 'e', 'E')): return float(v)
        return int(v)
    except Exception:
        return v

def cheap_yaml_load(text: str):
    root: Dict[str, object] = {}
    stack: List[Tuple[int, Dict[str, object]]] = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith('#'): continue
        indent = len(raw) - len(raw.lstrip(' '))
        m = re.match(r'^\s*([A-Za-z0-9_.-]+):\s*(.*)$', raw)
        if not m: continue
        key, rest = m.group(1), m.group(2)
        while stack and stack[-1][0] >= indent: stack.pop()
        parent = stack[-1][1] if stack else root
        if rest == '' or rest in ('|', '>'):
            newd: Dict[str, object] = {}
            parent[key] = newd
            stack.append((indent, newd))
        else:
            parent[key] = _coerce_scalar(rest)
    return root

def load_run_yaml(yaml_path: str) -> Dict[str, object]:
    with open(yaml_path, 'r') as f: text = f.read()
    if yaml is not None:
        try: return yaml.safe_load(text)
        except Exception: pass
    return cheap_yaml_load(text)

def from_yaml_fields(y: Dict[str, object], run_dir: str) -> Dict[str, object]:
    run = y.get("run", {}) if isinstance(y, dict) else {}
    knobs = y.get("knobs", {}) if isinstance(y, dict) else {}
    mig = knobs.get("migration", {}) if isinstance(knobs, dict) else {}

    # mx2 FIX: include run.get("microbench")
    bench_raw = (
        (run.get("kernel") if isinstance(run, dict) else None) or
        (run.get("bench") if isinstance(run, dict) else None) or
        (run.get("workload") if isinstance(run, dict) else None) or
        (run.get("microbench") if isinstance(run, dict) else None) or
        ""
    )
    # legacy fallback (some older yamls used top-level keys)
    if not bench_raw:
        bench_raw = y.get("kernel") or y.get("bench") or y.get("microbench") or y.get("workload") or ""

    roi_m = run.get("roi_m") if isinstance(run, dict) else None
    warm_m = run.get("warmup_m") if isinstance(run, dict) else None
    l3_kb = run.get("l3_size_kb") if isinstance(run, dict) else y.get("l3_size_kb", None)
    size_mb = int(l3_kb) // 1024 if isinstance(l3_kb, (int, float)) else None

    tech = run.get("tech", "") if isinstance(run, dict) else y.get("tech", "")
    variant = run.get("variant") if isinstance(run, dict) else y.get("variant")
    if not variant: variant = os.path.basename(run_dir)

    bench = str(bench_raw).replace('.', '_')

    if isinstance(roi_m, (int, float)) and isinstance(warm_m, (int, float)):
        bench_folder = f"{bench}_roi{int(roi_m)}M_warm{int(warm_m)}M"
    elif isinstance(roi_m, (int, float)):
        bench_folder = f"{bench}_roi{int(roi_m)}M"
    else:
        bench_folder = bench

    variant_path = f"sz{size_mb}M/{variant}" if size_mb is not None else str(variant)

    return dict(
        bench_folder=bench_folder,
        bench=bench,
        variant_path=variant_path,
        variant=str(variant),
        tech=str(tech),
        size_mb=size_mb if size_mb is not None else "",
        ROI_M=(int(roi_m) if isinstance(roi_m, (int, float)) else ""),
        WARMUP_M=(int(warm_m) if isinstance(warm_m, (int, float)) else ""),
        sram_ways=knobs.get("sram_ways", "") if isinstance(knobs, dict) else "",
        fill_to=knobs.get("fill_to", "") if isinstance(knobs, dict) else "",
        migration_enabled=isinstance(mig, dict) and bool(mig.get("enabled", False)),
        mig_promote_hits=(mig.get("promote_after_hits", "") if isinstance(mig, dict) else ""),
        mig_cooldown_hits=(mig.get("cooldown_hits", "") if isinstance(mig, dict) else ""),
    )

def load_yaml_props(run_dir: str) -> Dict:
    ypath = os.path.join(run_dir, "run.yaml")
    props = {"bench": "", "variant": "", "size_mb": "", "tech": "", "_yaml": None}
    if not os.path.isfile(ypath):
        props["variant"] = os.path.basename(run_dir)
        return props
    y = load_run_yaml(ypath)
    props["_yaml"] = y
    props.update(from_yaml_fields(y, run_dir))
    return props

# ==========================================
# 2) Log parsing (LC/LeakDVFS)
# ==========================================
LC_INIT_RE = re.compile(r"\[LC\]\s+Initialized:\s+cap=([0-9.]+)W\s+target=([0-9.]+)W.*?base_f=([0-9.]+)GHz", re.IGNORECASE)

LC_DVFS_RE = re.compile(
    r"\[LC\]\s+DVFS Change(?:\s+\[[^\]]+\])?:\s+"
    r"P_est=([0-9.]+)W.*?"
    r"Target=([0-9.]+)W.*?"
    r"(?:u_sum|sum_util)=([0-9.]+).*?"
    r"boosted=([0-9]+)/([0-9]+).*?"
    r"f\[min/avg/max\]=\[[0-9.]+/([0-9.]+)/[0-9.]+\]\s+GHz",
    re.IGNORECASE
)

def parse_lc_from_sniper_log(log_path: str, run_yaml=None) -> dict:
    out = dict(
        lc_final_freq_ghz="",
        lc_final_p_est_w="",
        lc_target_w="",
        lc_p_error_w="",
        lc_dvfs_changes=0,
        lc_mean_freq_ghz="",
        lc_headroom_w_last="",
        lc_headroom_w_mean="",
        lc_headroom_w_p95="",
        lc_sum_util_last="",
        lc_sum_util_mean="",
        lc_boosted_last="",
        lc_overshoot_w_max="",
    )
    if not os.path.isfile(log_path): return out

    init_cap = init_target = init_f = None
    last_p = last_target = last_f = None
    roi_started = False
    dvfs_changes_roi = 0
    roi_freq_samples = []

    roi_headroom_samples = []
    roi_sum_util_samples = []
    roi_overshoot_max = 0.0
    last_boosted = None
    last_sum_util = None

    def yaml_target_w(y):
        if not isinstance(y, dict): return None
        try:
            cap = y.get("knobs", {}).get("lc", {}).get("power_cap_w", None)
            tf = y.get("knobs", {}).get("lc", {}).get("target_frac", None)
            if cap is None or tf is None: return None
            return float(cap) * float(tf)
        except Exception: return None

    with open(log_path, "r", errors="ignore") as f:
        for line in f:
            if (not roi_started) and ("[SNIPER] Setting instrumentation mode to DETAILED" in line):
                roi_started = True
                if init_f is not None: roi_freq_samples.append(float(init_f))
                continue
            if roi_started and (("[SNIPER] Leaving ROI" in line) or ("[SNIPER] Setting instrumentation mode to FAST_FORWARD" in line)):
                break
            if "[LC]" not in line: continue

            m = LC_INIT_RE.search(line)
            if m:
                init_cap, init_target, init_f = float(m.group(1)), float(m.group(2)), float(m.group(3))
                if roi_started and not roi_freq_samples: roi_freq_samples.append(float(init_f))
                continue

            m = LC_DVFS_RE.search(line)
            if m:
                p = float(m.group(1))
                tgt = float(m.group(2))
                sum_util = float(m.group(3))
                b_num = int(m.group(4))
                b_den = int(m.group(5))
                fr = float(m.group(6))

                last_p, last_target, last_f = p, tgt, fr
                last_sum_util = sum_util
                last_boosted = f"{b_num}/{b_den}"

                if roi_started:
                    dvfs_changes_roi += 1
                    roi_freq_samples.append(fr)

                    headroom = tgt - p
                    roi_headroom_samples.append(headroom)
                    roi_sum_util_samples.append(sum_util)
                    if (p - tgt) > roi_overshoot_max:
                        roi_overshoot_max = (p - tgt)
                continue

    out["lc_dvfs_changes"] = dvfs_changes_roi
    final_f = last_f if last_f is not None else init_f
    if final_f is not None: out["lc_final_freq_ghz"] = final_f
    if last_p is not None: out["lc_final_p_est_w"] = last_p

    if roi_freq_samples: out["lc_mean_freq_ghz"] = sum(roi_freq_samples) / len(roi_freq_samples)
    elif final_f is not None: out["lc_mean_freq_ghz"] = final_f

    target = last_target if last_target is not None else init_target
    if target is None: target = yaml_target_w(run_yaml)
    if target is not None: out["lc_target_w"] = target
    if (target is not None) and (last_p is not None): out["lc_p_error_w"] = target - last_p

    if (last_target is not None) and (last_p is not None):
        out["lc_headroom_w_last"] = (last_target - last_p)
    elif (target is not None) and (last_p is not None):
        out["lc_headroom_w_last"] = (target - last_p)

    if last_sum_util is not None:
        out["lc_sum_util_last"] = last_sum_util
    if last_boosted is not None:
        out["lc_boosted_last"] = last_boosted

    if roi_headroom_samples:
        out["lc_headroom_w_mean"] = sum(roi_headroom_samples) / len(roi_headroom_samples)
        xs = sorted(roi_headroom_samples)
        k = int(0.95 * (len(xs) - 1))
        out["lc_headroom_w_p95"] = xs[k]

    if roi_sum_util_samples:
        out["lc_sum_util_mean"] = sum(roi_sum_util_samples) / len(roi_sum_util_samples)

    out["lc_overshoot_w_max"] = roi_overshoot_max
    return out

def parse_lc_frequency(log_path: str, run_yaml=None) -> dict:
    out = {"lc_active": False, "lc_mean_freq_ghz": None, "lc_final_freq_ghz": None, "lc_dvfs_changes": 0,
           "lc_final_p_est_w": None, "lc_target_w": None, "lc_p_error_w": None}
    if not os.path.isfile(log_path): return out
    lc = parse_lc_from_sniper_log(log_path, run_yaml)
    saw_any = ((lc.get("lc_final_freq_ghz") not in (None, "")) or (lc.get("lc_mean_freq_ghz") not in (None, "")) or (int(lc.get("lc_dvfs_changes") or 0) > 0))
    elsewhere_active = lc.get("lc_final_p_est_w") not in (None, "")
    out["lc_active"] = bool(saw_any or elsewhere_active)
    out.update({k: v if v != "" else None for k, v in lc.items()})
    out["lc_dvfs_changes"] = int(lc.get("lc_dvfs_changes") or 0)
    return out

ROI_SEC_RE = re.compile(r"\[SNIPER\]\s+Leaving ROI after\s+([0-9.]+)\s+seconds", re.IGNORECASE)
def parse_roi_seconds_from_sniper_log(log_path: str) -> Optional[float]:
    if not os.path.isfile(log_path): return None
    with open(log_path, "r", errors="ignore") as f:
        for line in f:
            m = ROI_SEC_RE.search(line)
            if m: return float(m.group(1))
    return None

SIM_SUMMARY_RE = re.compile(r"\[SNIPER\]\s+Simulated\s+([0-9.]+)M instructions,\s+([0-9.]+)M cycles,\s+([0-9.]+)\s+IPC", re.IGNORECASE)
def parse_sim_summary_from_sniper_log(log_path: str):
    if not os.path.isfile(log_path): return (None, None, None)
    with open(log_path, "r", errors="ignore") as f:
        for line in f:
            m = SIM_SUMMARY_RE.search(line)
            if m: return (float(m.group(1))*1e6, float(m.group(2))*1e6, float(m.group(3)))
    return (None, None, None)

CMD_FREQ_RE = re.compile(r"-g\s+perf_model/core/frequency=([0-9.]+)")
def parse_base_freq_from_cmdinfo(cmdinfo_path: str) -> Optional[float]:
    if not os.path.isfile(cmdinfo_path): return None
    try:
        txt = open(cmdinfo_path, "r", errors="ignore").read()
        m = CMD_FREQ_RE.search(txt)
        if m: return float(m.group(1))
    except Exception:
        return None
    return None

# ==========================================
# 3) Database parsing
# ==========================================
U64 = 2**64
def _u64_delta(a: int, b: int) -> int:
    d = b - a
    if d < 0: d += U64
    return d

def load_deltas(db_path: str) -> Tuple[Dict, str, str, str]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    names = {r["nameid"]: (r["objectname"], r["metricname"]) for r in cur.execute("SELECT nameid, objectname, metricname FROM names")}
    prefixes = {r["prefixname"]: r["prefixid"] for r in cur.execute("SELECT prefixid, prefixname FROM prefixes")}

    if "roi-begin" in prefixes and "roi-end" in prefixes:
        pb, pe = prefixes["roi-begin"], prefixes["roi-end"]
        source, begin_name, end_name = "roi", "roi-begin", "roi-end"
    elif "start" in prefixes and "stop" in prefixes:
        pb, pe = prefixes["start"], prefixes["stop"]
        source, begin_name, end_name = "full_sim", "start", "stop"
    else:
        con.close()
        raise RuntimeError("Missing ROI or Start/Stop prefixes in DB")

    vals = defaultdict(lambda: defaultdict(dict))
    for row in cur.execute('SELECT prefixid, nameid, core, value FROM "values" WHERE prefixid IN (?,?)', (pb, pe)):
        nm = names.get(row["nameid"])
        if nm: vals[row["prefixid"]][nm][row["core"]] = row["value"]
    con.close()

    deltas = {}
    for key in set(vals[pb].keys()) | set(vals[pe].keys()):
        cores = set(vals[pb].get(key, {}).keys()) | set(vals[pe].get(key, {}).keys())
        inner = {}
        for c in cores:
            a = int(vals[pb].get(key, {}).get(c, 0))
            b = int(vals[pe].get(key, {}).get(c, 0))
            inner[c] = _u64_delta(a, b)
        deltas[key] = inner
    return deltas, source, begin_name, end_name

def sum_d(D, obj, met): return sum(D.get((obj, met), {}).values())
def max_d(D, obj, met):
    vals = D.get((obj, met), {}).values()
    return max(vals) if vals else 0

def pick_time_fs(D, obj, met, prefer_sum=False):
    s = float(sum_d(D, obj, met))
    m = float(max_d(D, obj, met))
    if prefer_sum:
        return s if s > 0 else m
    if m > 0 and s > 1.5 * m:
        return s
    return m if m > 0 else s

# ==========================================
# 4) Compute metrics (mode-specific)
# ==========================================
def compute_metrics(db_path: str, log_path: str, mode: str) -> Dict:
    D, source, begin_name, end_name = load_deltas(db_path)

    elapsed_fs = 0.0
    roi_time_s = 0.0
    roi_time_source = ""

    if mode == "MULTI":
        elapsed_fs = float(pick_time_fs(D, "performance_model", "elapsed_time", prefer_sum=True))
        if elapsed_fs <= 0:
            elapsed_fs = float(pick_time_fs(D, "thread", "nonidle_elapsed_time", prefer_sum=True))
        if elapsed_fs <= 0:
            elapsed_fs = float(pick_time_fs(D, "thread", "elapsed_time", prefer_sum=True))
        roi_time_s = elapsed_fs / 1e15 if elapsed_fs > 0 else 0.0
        roi_time_source = f"db_elapsed_fs({end_name}-{begin_name})"

    elif mode == "KERNEL":
        elapsed_fs = float(pick_time_fs(D, "thread", "elapsed_time"))
        if elapsed_fs <= 0:
            elapsed_fs = float(pick_time_fs(D, "performance_model", "elapsed_time"))
        log_roi_s = parse_roi_seconds_from_sniper_log(log_path)
        if log_roi_s is not None:
            roi_time_s = log_roi_s
            roi_time_source = "log_leaving_roi_seconds"
        else:
            roi_time_s = elapsed_fs / 1e15 if elapsed_fs > 0 else 0.0
            roi_time_source = "db_elapsed_fs"

    else:  # SINGLE
        elapsed_fs = float(pick_time_fs(D, "thread", "elapsed_time"))
        if elapsed_fs <= 0:
            elapsed_fs = float(pick_time_fs(D, "performance_model", "elapsed_time"))
        roi_time_s = elapsed_fs / 1e15 if elapsed_fs > 0 else 0.0
        roi_time_source = "db_elapsed_fs"

    log_instr, log_cycles, log_ipc = parse_sim_summary_from_sniper_log(log_path)
    cycles = float(log_cycles) if (log_cycles is not None and log_cycles > 0) else 0.0

    instr = float(sum_d(D, "performance_model", "instruction_count"))
    if instr <= 0: instr = float(sum_d(D, "thread", "instruction_count"))
    if instr <= 0: instr = float(sum_d(D, "core", "instructions"))

    if log_ipc is not None: ipc = float(log_ipc)
    elif cycles > 0: ipc = instr / cycles
    else: ipc = 0.0

    tpi_fs = (elapsed_fs / instr) if instr > 0 else 0.0

    # Cache
    r_hits = float(sum_d(D, "L3", "l3_read_hits"))
    w_hits = float(sum_d(D, "L3", "l3_write_hits"))
    misses = float(sum_d(D, "L3", "l3_misses"))
    wbs = float(sum_d(D, "L3", "l3_writebacks"))
    evictions = float(sum_d(D, "L3", "l3_evictions"))
    rhs, rhm = float(sum_d(D, "L3", "l3_read_hits_sram")), float(sum_d(D, "L3", "l3_read_hits_mram"))
    whs, whm = float(sum_d(D, "L3", "l3_write_hits_sram")), float(sum_d(D, "L3", "l3_write_hits_mram"))

    loads, stores = float(sum_d(D, "L3", "loads")), float(sum_d(D, "L3", "stores"))
    load_misses, store_misses = float(sum_d(D, "L3", "load-misses")), float(sum_d(D, "L3", "store-misses"))

    used_loads_stores = False
    accesses_ls = (loads + stores) if (loads or stores) else None
    misses_ls = (load_misses + store_misses) if (load_misses or store_misses) else None

    if (r_hits + w_hits) == 0.0 and accesses_ls is not None and misses_ls is not None:
        total_hits = max(accesses_ls - misses_ls, 0.0)
        frac_loads = (loads / accesses_ls) if accesses_ls > 0 else 0.5
        frac_stores = (stores / accesses_ls) if accesses_ls > 0 else 0.5
        r_hits, w_hits = total_hits * frac_loads, total_hits * frac_stores
        used_loads_stores = True
        if misses == 0.0 or abs(misses - misses_ls) > 0.05 * max(misses_ls, 1.0): misses = misses_ls

    accesses = r_hits + w_hits + misses
    l3_hits = r_hits + w_hits
    l3_counts_src = "db_loads+stores" if used_loads_stores else "db_l3_hits+misses"
    miss_rate_pct = (misses / accesses * 100.0) if accesses > 0 else 0.0
    mpki = (misses / (instr / 1e3)) if instr > 0 else 0.0

    # DRAM
    dram_reqs = float(sum_d(D, "dram", "reads")) + float(sum_d(D, "dram", "writes"))
    dram_avg_lat = (float(sum_d(D, "dram", "total-access-latency")) / dram_reqs) if dram_reqs > 0 else 0.0
    dram_avg_q = (float(sum_d(D, "dram", "total-queueing-delay")) / dram_reqs) if dram_reqs > 0 else 0.0

    mig_lines = float(sum_d(D, "L3", "mram_write_bytes_migrate")) / LINE_SIZE

    # -------- per-core stats for weighted-speedup / ROI-slice --------
    # D[(obj, met)] is a dict {core_id: delta_value}
    per_core_instr = D.get(("performance_model", "instruction_count"), {})
    if not per_core_instr:
        per_core_instr = D.get(("core", "instructions"), {})
    per_core_elapsed = D.get(("performance_model", "elapsed_time"), {})
    if not per_core_elapsed:
        per_core_elapsed = D.get(("thread", "elapsed_time"), {})

    n_cores = max(len(per_core_instr), len(per_core_elapsed), 1)
    per_core = {"sim_n": n_cores}
    for c in sorted(set(per_core_instr.keys()) | set(per_core_elapsed.keys())):
        ci = float(per_core_instr.get(c, 0))
        ce = float(per_core_elapsed.get(c, 0))
        per_core[f"instructions_c{c}"] = ci
        per_core[f"elapsed_time_fs_c{c}"] = ce
        per_core[f"ipc_c{c}"] = (ci / (ce / 1e15 * 2.2e9)) if ce > 0 else 0.0  # approx IPC at base freq
        per_core[f"throughput_ips_c{c}"] = (ci / (ce / 1e15)) if ce > 0 else 0.0  # instr per second

    return {
        "roi_time_s": roi_time_s, "roi_time_source": roi_time_source,
        "cycles": cycles, "instructions": instr, "ipc": ipc, "tpi_fs": tpi_fs,
        "sim_summary_instr": log_instr, "sim_summary_cycles": log_cycles, "sim_summary_ipc": log_ipc,
        "dram_avg_lat": dram_avg_lat, "dram_avg_q": dram_avg_q,
        "mig_lines": mig_lines,
        "policy_P": float(sum_d(D, "L3", "hybrid_promotions")),
        "policy_S": float(sum_d(D, "L3", "hybrid_swaps")),
        "policy_T": float(sum_d(D, "L3", "hybrid_throttle_drops")),
        "accesses": accesses, "misses": misses, "l3_hits": l3_hits, "l3_counts_src": l3_counts_src,
        "miss_rate_pct": miss_rate_pct, "mpki": mpki,
        "read_hits": r_hits, "write_hits": w_hits, "writebacks": wbs, "evictions": evictions,
        "read_hits_sram": rhs, "read_hits_mram": rhm, "write_hits_sram": whs, "write_hits_mram": whm,
        "source": source,
        **per_core,
    }

# ==========================================
# 5) Mode detection for mx2
# ==========================================
def detect_mode_and_campaign(run_yaml: Dict[str, object]) -> Tuple[str, str]:
    run = run_yaml.get("run", {}) if isinstance(run_yaml, dict) else {}
    if isinstance(run, dict):
        if "workload" in run:   # trace replay
            return "MULTI", "traces"
        if "kernel" in run:
            return "KERNEL", "kernel"
        if "microbench" in run:
            return "SINGLE", "microbench"
        if "bench" in run:
            return "SINGLE", "spec"
    # fallback
    return "SINGLE", "unknown"

# ==========================================
# 6) Main
# ==========================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="Root directory to scan (e.g., results_test/spec/<run_id>)")
    ap.add_argument("--out", required=True, help="Output CSV file path")
    args = ap.parse_args()

    # Find run dirs (leaf dirs containing sim.stats.sqlite3)
    run_dirs: List[str] = []
    if os.path.isfile(os.path.join(args.root, "sim.stats.sqlite3")):
        run_dirs.append(args.root)
    else:
        for r, _ds, fs in os.walk(args.root):
            if "sim.stats.sqlite3" in fs:
                run_dirs.append(r)

    print(f"Found {len(run_dirs)} runs in {args.root}")
    rows: List[Dict] = []

    for rd in sorted(run_dirs):
        props = load_yaml_props(rd)
        run_yaml = props.get("_yaml") or {}

        mode, campaign = detect_mode_and_campaign(run_yaml)
        db = os.path.join(rd, "sim.stats.sqlite3")
        log = os.path.join(rd, "sniper.log")
        cmdinfo = os.path.join(rd, "cmd.info")

        lc_info = parse_lc_frequency(log, run_yaml)

        # base freq fallback
        base_f = parse_base_freq_from_cmdinfo(cmdinfo) or 2.66
        eff_f = float(lc_info["lc_mean_freq_ghz"]) if (lc_info["lc_active"] and lc_info["lc_mean_freq_ghz"]) else base_f

        try:
            m = compute_metrics(db, log, mode)
        except Exception:
            continue

        row = props.copy()
        row.pop("_yaml", None)
        row.update(m)
        row.update(lc_info)

        row["run_dir"] = os.path.abspath(rd)
        row["campaign"] = campaign
        row["mode"] = mode
        row["base_freq_ghz"] = round(base_f, 4)
        row["effective_freq_ghz"] = round(eff_f, 4)
        row["is_dvfs_active"] = bool(lc_info.get("lc_active", False))

        rows.append(row)

    if not rows:
        print("No rows generated.")
        return

    fieldnames = set()
    for r in rows: fieldnames.update(r.keys())

    priority = ["run_dir", "campaign", "mode", "bench", "variant", "size_mb", "tech",
                "roi_time_s", "cycles", "instructions", "ipc", "tpi_fs",
                "effective_freq_ghz", "is_dvfs_active"]
    final_fields = [f for f in priority if f in fieldnames] + sorted([f for f in fieldnames if f not in priority])

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=final_fields)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {args.out}")

if __name__ == "__main__":
    main()
