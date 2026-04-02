#!/usr/bin/env python3
"""
build_agg_dataset.py — Master extractor for miniMXE DVFS/HCA aggregated datasets.

This script scans the experiment roots under
    /home/skataoka26/COSC_498/miniMXE/repro
and writes a multi-level aggregated dataset under
    /home/skataoka26/COSC_498/miniMXE/repro/agg
using absolute paths only.

Outputs:
  - runs.csv          : one row per run with wide run-level metrics
  - per_core.csv      : one row per core per run
  - per_interval.csv  : one row per [LC] interval per run from sniper.log
  - comparisons.csv   : one row per derived comparison metric
  - sweeps.csv        : one row per sweep point with baseline/cf context
  - master.json       : full nested archive

Usage:
    python3 /home/skataoka26/COSC_498/miniMXE/repro/agg/build_agg_dataset.py
    python3 /home/skataoka26/COSC_498/miniMXE/repro/agg/build_agg_dataset.py \
        --meta /home/skataoka26/COSC_498/miniMXE/repro/agg/meta.yaml
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml


DEFAULT_META_PATH = "/home/skataoka26/COSC_498/miniMXE/mx3/config/meta.yaml"


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def safe_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


def safe_int(x: Any) -> Optional[int]:
    if x is None:
        return None
    try:
        return int(x)
    except Exception:
        return None


def stdev(vals: List[float]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    mu = sum(vals) / len(vals)
    return math.sqrt(sum((v - mu) ** 2 for v in vals) / len(vals))


def ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return a / b


def pct_from_ratio(r: Optional[float]) -> Optional[float]:
    if r is None:
        return None
    return (r - 1.0) * 100.0


def fs_to_s(v: Optional[float]) -> Optional[float]:
    if v is None:
        return None
    return v * 1e-15


def sort_key_run(r: Dict[str, Any]) -> Tuple[Any, ...]:
    return (
        safe_int(r.get("n_cores")) or 0,
        safe_int(r.get("size_mb")) or 0,
        r.get("workload") or "",
        r.get("source") or "",
        r.get("variant_dir") or "",
    )


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------
def _get_stat(db_path: str, obj: str, metric: str,
              prefix: str = "roi-end", core: int = 0) -> Optional[float]:
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


def _get_stat_multi(db_path: str, obj: str, metric: str,
                    prefix: str, n_cores: int) -> List[Tuple[int, float]]:
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
        return [(int(core), float(val)) for core, val in rows]
    except Exception:
        return []


def get_delta(db_path: str, obj: str, metric: str, core: int = 0) -> Optional[float]:
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


# ---------------------------------------------------------------------------
# sim.out parsing / time extraction
# ---------------------------------------------------------------------------
def _parse_sim_out_times(path: str) -> Optional[List[float]]:
    begins: List[float] = []
    ends: List[float] = []
    elapsed: List[float] = []
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
        return None

    if begins and ends and len(begins) == len(ends):
        return [(e - b) * 1e-15 for b, e in zip(begins, ends)]
    if elapsed:
        return [e * 1e-15 for e in elapsed]
    return None


def get_times_from_dir(run_dir: str) -> Optional[List[Optional[float]]]:
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
    result: List[Optional[float]] = []
    for core in range(n_cores):
        t = get_delta(db_path, "thread", "elapsed_time", core)
        if t is None or t <= 0:
            t = get_delta(db_path, "performance_model", "elapsed_time", core)
        result.append(t * 1e-15 if t is not None and t > 0 else None)

    if any(v is not None and v > 0 for v in result):
        return result
    return None


# ---------------------------------------------------------------------------
# sniper.log parsing
# ---------------------------------------------------------------------------
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


def parse_sniper_log(log_path: str) -> Dict[str, Any]:
    intervals: List[Dict[str, Any]] = []
    final: Optional[Dict[str, Any]] = None

    if not os.path.exists(log_path):
        return {"intervals": intervals, "final": final}

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

    return {"intervals": intervals, "final": final}


# ---------------------------------------------------------------------------
# Workload helpers
# ---------------------------------------------------------------------------
def normalize_workload_token(token: str) -> str:
    token = token.strip()
    token = token.split("_roi")[0]
    if re.match(r"^\d+_[A-Za-z0-9]", token):
        token = token.replace("_", ".", 1)
    return token


def shorten_bench(bench: str) -> str:
    m = re.match(r"\d+\.(\w+?)(?:_[rs])?$", bench)
    return m.group(1) if m else bench


def shorten_workload(wl: str) -> str:
    if "+" not in wl:
        return shorten_bench(wl)

    parts = wl.split("+")
    counts: List[Tuple[str, int]] = []
    prev = None
    cnt = 0
    for p in parts:
        s = shorten_bench(p)
        if s == prev:
            cnt += 1
        else:
            if prev is not None:
                counts.append((prev, cnt))
            prev = s
            cnt = 1
    if prev is not None:
        counts.append((prev, cnt))
    return "+".join(f"{name}×{c}" if c > 1 else name for name, c in counts)


# ---------------------------------------------------------------------------
# Meta-driven rule matching
# ---------------------------------------------------------------------------
def load_meta(path: str) -> Dict[str, Any]:
    with open(path) as f:
        meta = yaml.safe_load(f)
    return meta


def path_priority(meta: Dict[str, Any], source: str) -> int:
    return int(meta.get("source_priorities", {}).get(source, 999))


def match_string_rule(text: str, rule: Dict[str, Any]) -> bool:
    text = text or ""
    prefixes = rule.get("prefixes", [])
    suffixes = rule.get("suffixes", [])
    substrings = rule.get("substrings", [])
    regexes = rule.get("regexes", [])
    excludes_prefixes = rule.get("excludes_prefixes", [])
    excludes_substrings = rule.get("excludes_substrings", [])
    excludes_regexes = rule.get("excludes_regexes", [])

    for p in excludes_prefixes:
        if text.startswith(p):
            return False
    for s in excludes_substrings:
        if s in text:
            return False
    for rx in excludes_regexes:
        if re.search(rx, text):
            return False

    if prefixes and not any(text.startswith(p) for p in prefixes):
        return False
    if suffixes and not any(text.endswith(s) for s in suffixes):
        return False
    if substrings and not all(s in text for s in substrings):
        return False
    if regexes and not all(re.search(rx, text) for rx in regexes):
        return False
    return True


def classify_variant(meta: Dict[str, Any], source: str, variant_dir: str) -> Dict[str, Any]:
    for rule in meta.get("variant_rules", []):
        sources = rule.get("sources", [])
        if sources and source not in sources:
            continue
        if not match_string_rule(variant_dir, rule.get("match", {})):
            continue
        out = {
            "rule_id": rule["id"],
            "config_label": rule["config_label"],
            "config_group": rule.get("config_group"),
            "technology": rule.get("technology"),
            "stage": rule.get("stage"),
        }
        out.update(rule.get("extra", {}))
        return out
    return {
        "rule_id": "unclassified",
        "config_label": "UNCLASSIFIED",
        "config_group": "other",
        "technology": None,
        "stage": source,
    }


# ---------------------------------------------------------------------------
# Path parsing helpers
# ---------------------------------------------------------------------------
def parse_standard_run_context(run_path: str) -> Tuple[Optional[str], Optional[str], Optional[int], str]:
    parts = Path(run_path).parts
    variant_dir = parts[-1]

    n_idx = None
    for i, p in enumerate(parts):
        if re.fullmatch(r"n\d+", p):
            n_idx = i
            break
    if n_idx is None or n_idx == 0 or n_idx + 1 >= len(parts):
        return None, None, None, variant_dir

    workload = parts[n_idx - 1]
    n_tag = parts[n_idx]
    l3_part = parts[n_idx + 1]
    m = re.fullmatch(r"l3_(\d+)MB", l3_part)
    size_mb = int(m.group(1)) if m else None
    return workload, n_tag, size_mb, variant_dir


def parse_hca_run_context(run_path: str) -> Tuple[Optional[str], str, Optional[int], str]:
    parts = Path(run_path).parts
    variant_dir = parts[-1]

    workload = None
    size_mb = None

    for p in parts:
        if p.startswith("sz") and p.endswith("M"):
            try:
                size_mb = int(p[2:-1])
            except Exception:
                pass
        if "roi" in p and (p[0].isdigit() or p.startswith("0")):
            workload = normalize_workload_token(p)
        elif re.match(r"^\d+[._]\w", p):
            workload = normalize_workload_token(p)

    return workload, "n1", size_mb, variant_dir


def parse_run_context(source: str, run_path: str) -> Tuple[Optional[str], Optional[str], Optional[int], str]:
    if source == "hca":
        return parse_hca_run_context(run_path)
    return parse_standard_run_context(run_path)


# ---------------------------------------------------------------------------
# Sweep / cap parsing helpers
# ---------------------------------------------------------------------------
def parse_cap_from_variant_dir(variant_dir: str) -> Optional[float]:
    m = re.search(r"(?:^|_)c(\d+p\d+)(?:_|$)", variant_dir)
    if not m:
        return None
    return float(m.group(1).replace("p", "."))


def parse_read_latency_factor(variant_dir: str) -> Optional[int]:
    m = re.search(r"_rdx(\d+)$", variant_dir)
    return int(m.group(1)) if m else None


def parse_leakage_gap_fraction(variant_dir: str) -> Optional[float]:
    m = re.search(r"_lk(\d+(?:\.\d+)?)$", variant_dir)
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Completion checks and discovery
# ---------------------------------------------------------------------------
def has_completed_results(run_dir: str) -> bool:
    if not os.path.isdir(run_dir):
        return False
    if os.path.exists(os.path.join(run_dir, "sim.out")):
        return True
    status_file = os.path.join(run_dir, "mx3_status.yaml")
    if os.path.exists(status_file):
        try:
            with open(status_file) as f:
                status = yaml.safe_load(f) or {}
            return status.get("status") == "done"
        except Exception:
            return False
    return False


def discover_standard_runs(source: str, root: str) -> List[Dict[str, Any]]:
    runs: List[Dict[str, Any]] = []
    if not os.path.isdir(root):
        return runs

    for workload in sorted(os.listdir(root)):
        wl_path = os.path.join(root, workload)
        if not os.path.isdir(wl_path):
            continue
        for n_tag in sorted(os.listdir(wl_path)):
            n_path = os.path.join(wl_path, n_tag)
            if not os.path.isdir(n_path) or not re.fullmatch(r"n\d+", n_tag):
                continue
            for l3_dir in sorted(os.listdir(n_path)):
                l3_path = os.path.join(n_path, l3_dir)
                if not os.path.isdir(l3_path) or not re.fullmatch(r"l3_\d+MB", l3_dir):
                    continue
                for variant_dir in sorted(os.listdir(l3_path)):
                    run_path = os.path.join(l3_path, variant_dir)
                    if not os.path.isdir(run_path):
                        continue
                    if not has_completed_results(run_path):
                        continue
                    runs.append({
                        "source": source,
                        "run_path": run_path,
                    })
    return runs


def discover_hca_runs(root: str) -> List[Dict[str, Any]]:
    runs: List[Dict[str, Any]] = []
    if not os.path.isdir(root):
        return runs

    for campaign in [
        "1_baselines",
        "2_cross_node/mram14",
        "2_cross_node/mram32",
        "3_static_hca",
    ]:
        runs_dir = os.path.join(root, campaign, "runs")
        if not os.path.isdir(runs_dir):
            continue
        for cur_root, _, files in os.walk(runs_dir):
            if "sim.out" not in files and "sim.stats.sqlite3" not in files:
                continue
            if not has_completed_results(cur_root):
                continue
            runs.append({
                "source": "hca",
                "campaign": campaign,
                "run_path": cur_root,
            })
    return runs


def discover_all_runs(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    roots = meta.get("roots", {})
    all_runs: List[Dict[str, Any]] = []

    for source in [
        "fixed_dvfs",
        "smart_ttl",
        "main_dvfs",
        "counterfactual",
        "baseline_run",
        "fixed_read_latency",
        "fixed_leakage_gap",
        "fixed_cap_mae",
    ]:
        all_runs.extend(discover_standard_runs(source, roots.get(source, "")))

    all_runs.extend(discover_hca_runs(roots.get("hca", "")))
    return all_runs


# ---------------------------------------------------------------------------
# Oracle lookup / device params
# ---------------------------------------------------------------------------
def build_oracle_lookup(meta: Dict[str, Any]) -> Dict[Tuple[str, int, int], float]:
    csv_path = meta.get("roots", {}).get("oracle_csv")
    result: Dict[Tuple[str, int, int], float] = {}
    if not csv_path or not os.path.exists(csv_path):
        return result

    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            run_dir = row.get("run_dir", "")
            bench = row.get("bench", "")
            n_m = re.search(r"/n(\d+)/", run_dir)
            if not n_m:
                continue
            n_cores = int(n_m.group(1))
            try:
                size_mb = int(float(row.get("size_mb", "")))
                p_noncache = float(row.get("y_PminusLLC", ""))
            except Exception:
                continue
            result[(bench, n_cores, size_mb)] = p_noncache
    return result


def get_device_params(meta: Dict[str, Any], technology: Optional[str], size_mb: Optional[int]) -> Optional[Dict[str, float]]:
    if technology is None or size_mb is None:
        return None
    key = f"{technology}:{size_mb}"
    params = meta.get("device_params", {}).get(key)
    if not params:
        return None
    return {
        "leak_mw": float(params["leak_mw"]),
        "r_pj": float(params["r_pj"]),
        "w_pj": float(params["w_pj"]),
    }


def load_params_caps(meta: Dict[str, Any]) -> Dict[Tuple[str, str, int], float]:
    params_yaml = meta.get("roots", {}).get("params_yaml")
    out: Dict[Tuple[str, str, int], float] = {}
    if not params_yaml or not os.path.exists(params_yaml):
        return out

    with open(params_yaml) as f:
        params = yaml.safe_load(f) or {}
    plm_caps = params.get("uarch", {}).get("sunnycove", {}).get("plm_cap_w", {})
    for n_tag, wl_caps in plm_caps.items():
        if not isinstance(wl_caps, dict):
            continue
        for workload, caps_by_size in wl_caps.items():
            if not isinstance(caps_by_size, dict):
                continue
            for size_mb, cap in caps_by_size.items():
                try:
                    out[(workload, n_tag, int(size_mb))] = float(cap)
                except Exception:
                    continue
    return out


# ---------------------------------------------------------------------------
# Metrics extraction
# ---------------------------------------------------------------------------
PER_CORE_METRICS = [
    ("instruction_count", "performance_model", "instruction_count"),
    ("nonidle_elapsed_time_fs", "thread", "nonidle_elapsed_time"),
    ("idle_elapsed_time_fs", "performance_model", "idle_elapsed_time"),
    ("cpiBase", "rob_timer", "cpiBase"),
    ("cpiBranchPredictor", "rob_timer", "cpiBranchPredictor"),
    ("cpiDataCacheL1", "rob_timer", "cpiDataCacheL1"),
    ("cpiDataCacheL2", "rob_timer", "cpiDataCacheL2"),
    ("cpiDataCacheL3", "rob_timer", "cpiDataCacheL3"),
    ("cpiDataCachedram", "rob_timer", "cpiDataCachedram"),
    ("cpiRSFull", "rob_timer", "cpiRSFull"),
    ("cpiSerialization", "rob_timer", "cpiSerialization"),
    ("cpiSyncDvfsTransition", "performance_model", "cpiSyncDvfsTransition"),
    ("outstandingLongLatencyCycles", "rob_timer", "outstandingLongLatencyCycles"),
    ("l3_loads", "L3", "loads"),
    ("l3_stores", "L3", "stores"),
    ("l3_load_misses", "L3", "load-misses"),
    ("l3_store_misses", "L3", "store-misses"),
    ("l3_read_hits", "L3", "l3_read_hits"),
    ("l3_write_hits", "L3", "l3_write_hits"),
    ("l3_read_hits_sram", "L3", "l3_read_hits_sram"),
    ("l3_read_hits_mram", "L3", "l3_read_hits_mram"),
    ("l3_write_hits_sram", "L3", "l3_write_hits_sram"),
    ("l3_write_hits_mram", "L3", "l3_write_hits_mram"),
    ("mram_write_bytes", "L3", "mram_write_bytes"),
    ("hybrid_promotions", "L3", "hybrid_promotions"),
    ("dram_reads", "dram", "reads"),
    ("dram_writes", "dram", "writes"),
    ("uops_total", "rob_timer", "uops_total"),
    ("llc_dyn_energy_pJ_raw", "L3", "llc_dyn_energy_pJ"),
    ("llc_leakage_energy_raw", "L3", "llc_leakage_energy_pJ"),
]


def extract_per_core_rows(run: Dict[str, Any], meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    run_dir = run["run_path"]
    db_path = os.path.join(run_dir, "sim.stats.sqlite3")
    times = get_times_from_dir(run_dir) or []
    n_cores = len(times) if times else (get_num_cores(db_path) if os.path.exists(db_path) else 1)
    base_freq_ghz = float(meta.get("metrics", {}).get("base_freq_ghz", 2.2))

    rows: List[Dict[str, Any]] = []
    for core in range(n_cores):
        elapsed_s = times[core] if core < len(times) else None
        row: Dict[str, Any] = {
            "run_id": run["run_id"],
            "core": core,
            "elapsed_s": elapsed_s,
        }

        if os.path.exists(db_path):
            thread_elapsed_fs = get_delta(db_path, "thread", "elapsed_time", core)
            perf_elapsed_fs = get_delta(db_path, "performance_model", "elapsed_time", core)
            row["thread_elapsed_time_fs"] = thread_elapsed_fs
            row["perf_elapsed_time_fs"] = perf_elapsed_fs

            for key, obj, metric in PER_CORE_METRICS:
                if key == "cpiDataCachedram":
                    row[key] = get_delta(db_path, obj, metric, core)
                    alt = get_delta(db_path, "rob_timer", "cpiDataCachedram-cache", core)
                    if alt is None:
                        alt = get_delta(db_path, "rob_timer", "cpiDataCachedram_cache", core)
                    row["cpiDataCachedram_cache"] = alt
                else:
                    row[key] = get_delta(db_path, obj, metric, core)
        else:
            row["thread_elapsed_time_fs"] = None
            row["perf_elapsed_time_fs"] = None
            row["cpiDataCachedram_cache"] = None
            for key, _, _ in PER_CORE_METRICS:
                row.setdefault(key, None)

        instr = safe_float(row.get("instruction_count")) or 0.0
        nonidle_fs = safe_float(row.get("nonidle_elapsed_time_fs"))
        elapsed_s_local = row.get("elapsed_s")
        elapsed_fs = safe_float(row.get("thread_elapsed_time_fs")) or safe_float(row.get("perf_elapsed_time_fs"))

        if elapsed_s_local and elapsed_s_local > 0:
            row["throughput_inst_ns"] = instr / (elapsed_s_local * 1e9)
            cycles_base_elapsed = elapsed_s_local * base_freq_ghz * 1e9
            row["ipc_base_elapsed_proxy"] = instr / cycles_base_elapsed if cycles_base_elapsed > 0 else None
        else:
            row["throughput_inst_ns"] = None
            row["ipc_base_elapsed_proxy"] = None

        if nonidle_fs and nonidle_fs > 0:
            row["utilization"] = (nonidle_fs / elapsed_fs) if elapsed_fs and elapsed_fs > 0 else None
            cycles_base_nonidle = nonidle_fs * base_freq_ghz * 1e-6
            row["ipc_base_nonidle_proxy"] = instr / cycles_base_nonidle if cycles_base_nonidle > 0 else None
            mem_cpi = (
                (safe_float(row.get("cpiDataCacheL3")) or 0.0) +
                (safe_float(row.get("cpiDataCachedram")) or 0.0) +
                (safe_float(row.get("cpiDataCachedram_cache")) or 0.0)
            )
            row["mem_cpi_frac"] = mem_cpi / nonidle_fs
            ll = safe_float(row.get("outstandingLongLatencyCycles")) or 0.0
            row["long_lat_frac"] = ll / nonidle_fs
        else:
            row["utilization"] = None
            row["ipc_base_nonidle_proxy"] = None
            row["mem_cpi_frac"] = None
            row["long_lat_frac"] = None

        l3_loads = safe_float(row.get("l3_loads")) or 0.0
        l3_read_hits = safe_float(row.get("l3_read_hits"))
        if l3_read_hits is None:
            l3_read_hits = (safe_float(row.get("l3_read_hits_sram")) or 0.0) + (safe_float(row.get("l3_read_hits_mram")) or 0.0)
        l3_write_hits = safe_float(row.get("l3_write_hits"))
        if l3_write_hits is None:
            l3_write_hits = (safe_float(row.get("l3_write_hits_sram")) or 0.0) + (safe_float(row.get("l3_write_hits_mram")) or 0.0)
        l3_misses = safe_float(row.get("l3_load_misses")) or 0.0

        row["l3_read_hits_total"] = l3_read_hits
        row["l3_write_hits_total"] = l3_write_hits
        row["l3_miss_rate_from_loads"] = (l3_misses / l3_loads) if l3_loads > 0 else None
        row["l3_miss_rate_from_hits_plus_misses"] = (l3_misses / (l3_read_hits + l3_misses)) if (l3_read_hits + l3_misses) > 0 else None
        row["write_pct_of_hits"] = (l3_write_hits / (l3_read_hits + l3_write_hits)) if (l3_read_hits + l3_write_hits) > 0 else None
        row["l3_mpki"] = (l3_misses / instr * 1000.0) if instr > 0 else None
        dram_total = (safe_float(row.get("dram_reads")) or 0.0) + (safe_float(row.get("dram_writes")) or 0.0)
        row["dram_total"] = dram_total
        row["dram_mpki"] = (dram_total / instr * 1000.0) if instr > 0 else None

        llc_dyn_raw = safe_float(row.get("llc_dyn_energy_pJ_raw"))
        llc_leak_raw = safe_float(row.get("llc_leakage_energy_raw"))
        row["llc_dyn_energy_j_db"] = llc_dyn_raw * 1e-12 if llc_dyn_raw is not None else None
        row["llc_leak_energy_j_db"] = llc_leak_raw * 1e-21 if llc_leak_raw is not None else None
        row["llc_total_energy_j_db"] = (
            row["llc_dyn_energy_j_db"] + row["llc_leak_energy_j_db"]
            if row["llc_dyn_energy_j_db"] is not None and row["llc_leak_energy_j_db"] is not None
            else None
        )

        rows.append(row)

    return rows


def summarise_intervals(run_id: str, intervals: List[Dict[str, Any]], final: Optional[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    out_rows: List[Dict[str, Any]] = []
    summary: Dict[str, Any] = {
        "n_intervals": len(intervals),
        "n_transitions": None,
        "transition_rate": None,
        "cap_w": None,
        "avg_power_est_w": None,
        "avg_cap_slack_w": None,
        "avg_f_lookup_ghz": None,
        "avg_f_max_ghz": None,
        "max_f_max_ghz": None,
        "pct_over_cap": None,
        "pct_near_cap_hyst_0p10": None,
        "pct_room_gt_hyst_0p10": None,
        "freq_residency_json": None,
        "final_base_f_ghz": None,
        "final_f_min_ghz": None,
        "final_f_avg_ghz": None,
        "final_f_max_ghz": None,
        "final_llc_leak_w": None,
        "final_selective": None,
        "final_k": None,
        "final_power_model": None,
    }

    for idx, iv in enumerate(intervals):
        row = {"run_id": run_id, "interval_idx": idx}
        row.update(iv)
        out_rows.append(row)

    if intervals:
        summary["cap_w"] = intervals[0]["Target"]
        summary["avg_power_est_w"] = sum(iv["P_est"] for iv in intervals) / len(intervals)
        summary["avg_cap_slack_w"] = sum(iv["Target"] - iv["P_est"] for iv in intervals) / len(intervals)
        summary["avg_f_lookup_ghz"] = sum(iv["f_lookup"] for iv in intervals) / len(intervals)
        summary["avg_f_max_ghz"] = sum(iv["f_max"] for iv in intervals) / len(intervals)
        summary["max_f_max_ghz"] = max(iv["f_max"] for iv in intervals)
        summary["n_transitions"] = sum(
            1 for i in range(1, len(intervals))
            if abs(intervals[i]["f_max"] - intervals[i - 1]["f_max"]) > 1e-3
        )
        summary["transition_rate"] = summary["n_transitions"] / len(intervals)

        hyst = 0.10
        n = len(intervals)
        n_over = sum(1 for iv in intervals if iv["P_est"] > iv["Target"])
        n_band = sum(1 for iv in intervals if 0 <= (iv["Target"] - iv["P_est"]) <= hyst)
        n_room = sum(1 for iv in intervals if (iv["Target"] - iv["P_est"]) > hyst)
        summary["pct_over_cap"] = n_over / n
        summary["pct_near_cap_hyst_0p10"] = n_band / n
        summary["pct_room_gt_hyst_0p10"] = n_room / n

        freq_counts = Counter(round(iv["f_max"], 1) for iv in intervals)
        freq_residency = {
            f"{freq:.1f}": count / n
            for freq, count in sorted(freq_counts.items())
        }
        summary["freq_residency_json"] = json_dumps(freq_residency)

    if final:
        summary["final_base_f_ghz"] = final.get("base_f")
        summary["final_f_min_ghz"] = final.get("f_min")
        summary["final_f_avg_ghz"] = final.get("f_avg")
        summary["final_f_max_ghz"] = final.get("f_max")
        summary["final_llc_leak_w"] = final.get("llc_leak")
        summary["final_selective"] = final.get("selective")
        summary["final_k"] = final.get("k")
        summary["final_power_model"] = final.get("power_model")

    return out_rows, summary


def build_run_record(run: Dict[str, Any], meta: Dict[str, Any],
                     oracle_lookup: Dict[Tuple[str, int, int], float],
                     params_caps: Dict[Tuple[str, str, int], float]) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    workload, n_tag, size_mb, variant_dir = parse_run_context(run["source"], run["run_path"])
    if workload is None or n_tag is None or size_mb is None:
        return None, [], []

    workload = normalize_workload_token(workload)
    n_cores = int(n_tag[1:])
    class_info = classify_variant(meta, run["source"], variant_dir)
    base_freq_ghz = float(meta.get("metrics", {}).get("base_freq_ghz", 2.2))
    package_static_w = float(meta.get("metrics", {}).get("package_static_w", 20.08))

    run_id = f"{workload}__{n_tag}__l3_{size_mb}MB__{variant_dir}"
    run["run_id"] = run_id

    per_core_rows = extract_per_core_rows(run, meta)
    if not per_core_rows:
        return None, [], []

    log_info = parse_sniper_log(os.path.join(run["run_path"], "sniper.log"))
    per_interval_rows, interval_summary = summarise_intervals(run_id, log_info["intervals"], log_info["final"])

    times = [r.get("elapsed_s") for r in per_core_rows if r.get("elapsed_s") is not None and r.get("elapsed_s") > 0]
    makespan_s = max(times) if times else None
    avg_elapsed_s = (sum(times) / len(times)) if times else None

    agg: Dict[str, Any] = {
        "run_id": run_id,
        "source": run["source"],
        "source_priority": path_priority(meta, run["source"]),
        "campaign": run.get("campaign"),
        "run_path": run["run_path"],
        "workload": workload,
        "workload_short": shorten_workload(workload),
        "n_tag": n_tag,
        "n_cores": n_cores,
        "size_mb": size_mb,
        "variant_dir": variant_dir,
        "rule_id": class_info["rule_id"],
        "config_label": class_info["config_label"],
        "config_group": class_info.get("config_group"),
        "technology": class_info.get("technology"),
        "stage": class_info.get("stage"),
        "cap_w_from_variant": parse_cap_from_variant_dir(variant_dir),
        "read_latency_factor": parse_read_latency_factor(variant_dir),
        "leakage_gap_fraction": parse_leakage_gap_fraction(variant_dir),
        "makespan_s": makespan_s,
        "avg_elapsed_s": avg_elapsed_s,
        "times_s_json": json_dumps(times),
    }

    run_cmp = {k: v for k, v in agg.items()}

    sums: Dict[str, float] = defaultdict(float)
    count_nonnull: Dict[str, int] = defaultdict(int)
    utils: List[float] = []
    ipc_proxies: List[float] = []
    instrs_m: List[float] = []

    for row in per_core_rows:
        for key, val in row.items():
            if key in {"run_id", "core"}:
                continue
            if isinstance(val, (int, float)) and val is not None:
                sums[key] += float(val)
                count_nonnull[key] += 1
        if row.get("utilization") is not None:
            utils.append(row["utilization"])
        if row.get("ipc_base_nonidle_proxy") is not None:
            ipc_proxies.append(row["ipc_base_nonidle_proxy"])
        elif row.get("ipc_base_elapsed_proxy") is not None:
            ipc_proxies.append(row["ipc_base_elapsed_proxy"])
        instr = row.get("instruction_count")
        if instr is not None:
            instrs_m.append(instr / 1e6)

    total_instr = sums.get("instruction_count", 0.0)
    l3_loads = sums.get("l3_loads", 0.0)
    l3_stores = sums.get("l3_stores", 0.0)
    l3_load_misses = sums.get("l3_load_misses", 0.0)
    l3_store_misses = sums.get("l3_store_misses", 0.0)
    l3_read_hits_total = sums.get("l3_read_hits_total", 0.0)
    l3_write_hits_total = sums.get("l3_write_hits_total", 0.0)
    dram_total = sums.get("dram_total", 0.0)

    agg.update({
        "total_instructions": total_instr,
        "throughput_inst_ns": (total_instr / (makespan_s * 1e9)) if makespan_s and makespan_s > 0 else None,
        "avg_ipc_base_proxy": (sum(ipc_proxies) / len(ipc_proxies)) if ipc_proxies else None,
        "avg_utilization": (sum(utils) / len(utils)) if utils else None,
        "std_utilization": stdev(utils),
        "util_top1_minus_top2": (sorted(utils, reverse=True)[0] - sorted(utils, reverse=True)[1]) if len(utils) >= 2 else None,
        "std_ipc_base_proxy": stdev(ipc_proxies),
        "instr_m_per_core_json": json_dumps([round(v, 6) for v in instrs_m]),
        "l3_loads": l3_loads,
        "l3_stores": l3_stores,
        "l3_load_misses": l3_load_misses,
        "l3_store_misses": l3_store_misses,
        "l3_total_accesses": l3_loads + l3_stores,
        "l3_read_hits": l3_read_hits_total,
        "l3_write_hits": l3_write_hits_total,
        "l3_read_hits_sram": sums.get("l3_read_hits_sram"),
        "l3_read_hits_mram": sums.get("l3_read_hits_mram"),
        "l3_write_hits_sram": sums.get("l3_write_hits_sram"),
        "l3_write_hits_mram": sums.get("l3_write_hits_mram"),
        "l3_miss_rate_from_loads": (l3_load_misses / l3_loads) if l3_loads > 0 else None,
        "l3_miss_rate_from_hits_plus_misses": (l3_load_misses / (l3_read_hits_total + l3_load_misses)) if (l3_read_hits_total + l3_load_misses) > 0 else None,
        "write_pct_of_hits": (l3_write_hits_total / (l3_read_hits_total + l3_write_hits_total)) if (l3_read_hits_total + l3_write_hits_total) > 0 else None,
        "l3_mpki": (l3_load_misses / total_instr * 1000.0) if total_instr > 0 else None,
        "dram_reads": sums.get("dram_reads"),
        "dram_writes": sums.get("dram_writes"),
        "dram_total": dram_total,
        "dram_mpki": (dram_total / total_instr * 1000.0) if total_instr > 0 else None,
        "long_lat_cycles": sums.get("outstandingLongLatencyCycles"),
        "long_lat_frac": (sums.get("outstandingLongLatencyCycles", 0.0) / sums.get("nonidle_elapsed_time_fs", 0.0)) if sums.get("nonidle_elapsed_time_fs", 0.0) > 0 else None,
        "mem_cpi_frac": (
            (
                sums.get("cpiDataCacheL3", 0.0) +
                sums.get("cpiDataCachedram", 0.0) +
                sums.get("cpiDataCachedram_cache", 0.0)
            ) / sums.get("nonidle_elapsed_time_fs", 0.0)
        ) if sums.get("nonidle_elapsed_time_fs", 0.0) > 0 else None,
        "dvfs_transition_ms": (sums.get("cpiSyncDvfsTransition", 0.0) * 1e-12) if sums.get("cpiSyncDvfsTransition") is not None else None,
        "mram_write_bytes": sums.get("mram_write_bytes"),
        "hybrid_promotions": sums.get("hybrid_promotions"),
        "llc_dyn_energy_j_db": sums.get("llc_dyn_energy_j_db"),
        "llc_leak_energy_j_db": sums.get("llc_leak_energy_j_db"),
        "llc_total_energy_j_db": (
            (sums.get("llc_dyn_energy_j_db") or 0.0) + (sums.get("llc_leak_energy_j_db") or 0.0)
            if count_nonnull.get("llc_dyn_energy_j_db") or count_nonnull.get("llc_leak_energy_j_db")
            else None
        ),
    })

    if agg.get("llc_total_energy_j_db") is not None and makespan_s and makespan_s > 0:
        agg["pkg_energy_est_j"] = package_static_w * makespan_s + agg["llc_total_energy_j_db"]
        agg["avg_pkg_power_est_w"] = agg["pkg_energy_est_j"] / makespan_s
        agg["edp_est_j_s"] = agg["pkg_energy_est_j"] * makespan_s
    else:
        agg["pkg_energy_est_j"] = None
        agg["avg_pkg_power_est_w"] = None
        agg["edp_est_j_s"] = None

    device = get_device_params(meta, agg.get("technology"), size_mb)
    agg["device_params_json"] = json_dumps(device) if device else None
    if device and makespan_s is not None:
        if agg.get("technology") in {"mram14", "sram14", "sram7"}:
            agg["llc_dyn_energy_model_j"] = (
                ((agg.get("l3_read_hits") or 0.0) * device["r_pj"] + (agg.get("l3_write_hits") or 0.0) * device["w_pj"]) * 1e-12
            )
            agg["llc_leak_energy_model_j"] = device["leak_mw"] * 1e-3 * makespan_s
            agg["llc_total_energy_model_j"] = agg["llc_dyn_energy_model_j"] + agg["llc_leak_energy_model_j"]
        else:
            agg["llc_dyn_energy_model_j"] = None
            agg["llc_leak_energy_model_j"] = None
            agg["llc_total_energy_model_j"] = None
    else:
        agg["llc_dyn_energy_model_j"] = None
        agg["llc_leak_energy_model_j"] = None
        agg["llc_total_energy_model_j"] = None

    oracle_key = (workload, n_cores, size_mb)
    p_noncache = oracle_lookup.get(oracle_key)
    agg["oracle_noncache_power_w"] = p_noncache
    if p_noncache is not None and makespan_s is not None:
        agg["oracle_noncache_dyn_power_w"] = max(0.0, p_noncache - package_static_w)
        agg["energy_nc_static_j"] = package_static_w * makespan_s
        agg["energy_nc_dyn_oracle_j"] = agg["oracle_noncache_dyn_power_w"] * makespan_s
        if agg.get("llc_total_energy_model_j") is not None:
            agg["energy_total_oracle_j"] = agg["llc_total_energy_model_j"] + agg["energy_nc_static_j"] + agg["energy_nc_dyn_oracle_j"]
        else:
            agg["energy_total_oracle_j"] = None
    else:
        agg["oracle_noncache_dyn_power_w"] = None
        agg["energy_nc_static_j"] = None
        agg["energy_nc_dyn_oracle_j"] = None
        agg["energy_total_oracle_j"] = None

    agg.update(interval_summary)

    nominal_cap = params_caps.get((workload, n_tag, size_mb))
    agg["nominal_cap_w_from_params"] = nominal_cap
    if agg["cap_w_from_variant"] is not None and nominal_cap is not None:
        agg["cap_delta_w_vs_nominal"] = agg["cap_w_from_variant"] - nominal_cap
    else:
        agg["cap_delta_w_vs_nominal"] = None

    for row in per_core_rows:
        row.update({
            "workload": workload,
            "n_tag": n_tag,
            "n_cores": n_cores,
            "size_mb": size_mb,
            "config_label": agg["config_label"],
            "source": agg["source"],
            "variant_dir": variant_dir,
        })

    for row in per_interval_rows:
        row.update({
            "workload": workload,
            "n_tag": n_tag,
            "n_cores": n_cores,
            "size_mb": size_mb,
            "config_label": agg["config_label"],
            "source": agg["source"],
            "variant_dir": variant_dir,
        })

    return agg, per_core_rows, per_interval_rows


# ---------------------------------------------------------------------------
# Canonical pairing helpers
# ---------------------------------------------------------------------------
def build_group_index(runs: List[Dict[str, Any]]) -> Dict[Tuple[str, str, int], List[Dict[str, Any]]]:
    groups: Dict[Tuple[str, str, int], List[Dict[str, Any]]] = defaultdict(list)
    for r in runs:
        groups[(r["workload"], r["n_tag"], r["size_mb"])].append(r)
    return groups


def pick_best_run(candidates: List[Dict[str, Any]],
                  preferred_sources: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    preferred_sources = preferred_sources or []
    pref_rank = {src: i for i, src in enumerate(preferred_sources)}

    def key(r: Dict[str, Any]) -> Tuple[int, int, str]:
        return (
            pref_rank.get(r["source"], 999),
            safe_int(r.get("source_priority")) or 999,
            r.get("run_path", ""),
        )

    return sorted(candidates, key=key)[0]


def find_runs_by_label(group_runs: List[Dict[str, Any]], label: str) -> List[Dict[str, Any]]:
    return [r for r in group_runs if r.get("config_label") == label]


# ---------------------------------------------------------------------------
# Comparison builders
# ---------------------------------------------------------------------------
def make_comparison_row(rule_id: str, metric_name: str,
                        subject: Dict[str, Any], baseline: Optional[Dict[str, Any]],
                        value: Optional[float], unit: str,
                        extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    row = {
        "rule_id": rule_id,
        "metric_name": metric_name,
        "metric_value": value,
        "unit": unit,
        "workload": subject["workload"],
        "workload_short": subject["workload_short"],
        "n_tag": subject["n_tag"],
        "n_cores": subject["n_cores"],
        "size_mb": subject["size_mb"],
        "subject_run_id": subject["run_id"],
        "subject_config_label": subject["config_label"],
        "subject_source": subject["source"],
        "baseline_run_id": baseline["run_id"] if baseline else None,
        "baseline_config_label": baseline["config_label"] if baseline else None,
        "baseline_source": baseline["source"] if baseline else None,
    }
    if extra:
        row.update(extra)
    return row


def compute_ws_over_n(baseline: Dict[str, Any], subject: Dict[str, Any]) -> Tuple[Optional[float], Optional[List[float]]]:
    bt = json.loads(baseline.get("times_s_json") or "[]")
    st = json.loads(subject.get("times_s_json") or "[]")
    pairs = [(b, s) for b, s in zip(bt, st) if b is not None and s is not None and s > 0]
    if not pairs:
        return None, None
    terms = [b / s for b, s in pairs]
    return sum(terms) / len(terms), terms


def build_core_comparisons(meta: Dict[str, Any], runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups = build_group_index(runs)
    out: List[Dict[str, Any]] = []
    baseline_preferences = meta.get("baseline_preferences", {})

    for rule in meta.get("comparison_rules", []):
        rule_id = rule["id"]
        subject_labels = set(rule.get("subject_labels", []))
        baseline_labels = set(rule.get("baseline_labels", []))
        formula = rule["formula"]
        only_n_tags = set(rule.get("n_tags", [])) if rule.get("n_tags") else None
        preferred_sources = baseline_preferences.get(rule.get("baseline_pick_key") or "", [])

        for key, group_runs in groups.items():
            workload, n_tag, size_mb = key
            if only_n_tags and n_tag not in only_n_tags:
                continue

            subjects = [r for r in group_runs if r.get("config_label") in subject_labels]
            baselines = [r for r in group_runs if r.get("config_label") in baseline_labels]
            baseline = pick_best_run(baselines, preferred_sources)
            if not baseline:
                continue

            for subject in sorted(subjects, key=sort_key_run):
                if formula == "makespan_speedup":
                    speedup = ratio(baseline.get("makespan_s"), subject.get("makespan_s"))
                    out.append(make_comparison_row(rule_id, "speedup_ratio", subject, baseline, speedup, "ratio"))
                    out.append(make_comparison_row(rule_id, "speedup_pct", subject, baseline, pct_from_ratio(speedup), "pct"))
                elif formula == "ws_over_n":
                    ws, terms = compute_ws_over_n(baseline, subject)
                    extra = {"ws_terms_json": json_dumps(terms) if terms else None}
                    out.append(make_comparison_row(rule_id, "ws_over_n_ratio", subject, baseline, ws, "ratio", extra))
                    out.append(make_comparison_row(rule_id, "ws_over_n_pct", subject, baseline, pct_from_ratio(ws), "pct", extra))
                elif formula == "runtime_speedup_and_normalized_metrics":
                    spd = ratio(baseline.get("makespan_s"), subject.get("makespan_s"))
                    out.append(make_comparison_row(rule_id, "runtime_speedup_ratio", subject, baseline, spd, "ratio"))
                    out.append(make_comparison_row(rule_id, "runtime_speedup_pct", subject, baseline, pct_from_ratio(spd), "pct"))
                    miss_ratio = ratio(subject.get("l3_load_misses"), baseline.get("l3_load_misses"))
                    out.append(make_comparison_row(rule_id, "l3_load_misses_ratio_to_baseline", subject, baseline, miss_ratio, "ratio"))
                    mram_wr_ratio = ratio(subject.get("mram_write_bytes"), baseline.get("mram_write_bytes"))
                    out.append(make_comparison_row(rule_id, "mram_write_bytes_ratio_to_baseline", subject, baseline, mram_wr_ratio, "ratio"))
                    out.append(make_comparison_row(rule_id, "hybrid_promotions", subject, baseline, subject.get("hybrid_promotions"), "count"))
                elif formula == "restricted_fill_bundle":
                    spd = ratio(baseline.get("makespan_s"), subject.get("makespan_s"))
                    out.append(make_comparison_row(rule_id, "runtime_speedup_ratio", subject, baseline, spd, "ratio"))
                    out.append(make_comparison_row(rule_id, "runtime_speedup_pct", subject, baseline, pct_from_ratio(spd), "pct"))
                    miss_ratio = ratio(subject.get("l3_load_misses"), baseline.get("l3_load_misses"))
                    out.append(make_comparison_row(rule_id, "l3_load_misses_ratio_to_baseline", subject, baseline, miss_ratio, "ratio"))
                elif formula == "delta_vs_baseline":
                    field = rule["field"]
                    delta = None
                    if subject.get(field) is not None and baseline.get(field) is not None:
                        delta = subject[field] - baseline[field]
                    out.append(make_comparison_row(rule_id, f"delta_{field}", subject, baseline, delta, rule.get("unit", "scalar")))
                else:
                    raise ValueError(f"Unknown comparison formula: {formula}")

    # Gap rows that need two baselines / two subject sets
    for key, group_runs in groups.items():
        sram7 = pick_best_run(find_runs_by_label(group_runs, "SRAM7"), baseline_preferences.get("SRAM7", []))
        main_dvfs = pick_best_run(find_runs_by_label(group_runs, "MainDVFS"), baseline_preferences.get("MainDVFS", []))
        cf = pick_best_run(find_runs_by_label(group_runs, "Counterfactual"), baseline_preferences.get("Counterfactual", []))
        if sram7 and main_dvfs and cf and main_dvfs.get("makespan_s") and cf.get("makespan_s"):
            spd_d = ratio(sram7.get("makespan_s"), main_dvfs.get("makespan_s"))
            spd_c = ratio(sram7.get("makespan_s"), cf.get("makespan_s"))
            gap_pp = None
            if spd_d is not None and spd_c is not None:
                gap_pp = (spd_d - spd_c) * 100.0
            out.append(make_comparison_row(
                "main_dvfs_gap_vs_cf",
                "gap_pp",
                main_dvfs,
                cf,
                gap_pp,
                "pp",
                {"reference_run_id": sram7["run_id"], "reference_config_label": "SRAM7"},
            ))

        mram = pick_best_run(find_runs_by_label(group_runs, "smartDVFS+TTL"), baseline_preferences.get("smartDVFS+TTL", []))
        cf_smart = pick_best_run(find_runs_by_label(group_runs, "smartCounterfactual"), baseline_preferences.get("smartCounterfactual", []))
        if mram and cf_smart:
            for field, unit in [
                ("avg_f_max_ghz", "GHz"),
                ("avg_cap_slack_w", "W"),
                ("avg_power_est_w", "W"),
                ("pct_over_cap", "frac"),
            ]:
                if mram.get(field) is not None and cf_smart.get(field) is not None:
                    delta = mram[field] - cf_smart[field]
                    out.append(make_comparison_row(
                        "stage6_mram_vs_cf",
                        f"delta_{field}",
                        mram,
                        cf_smart,
                        delta,
                        unit,
                    ))

    return out


# ---------------------------------------------------------------------------
# Sweep table builder
# ---------------------------------------------------------------------------
def build_sweeps(meta: Dict[str, Any], runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups = build_group_index(runs)
    out: List[Dict[str, Any]] = []
    baseline_preferences = meta.get("baseline_preferences", {})
    mae_by_n = {k: float(v) for k, v in meta.get("metrics", {}).get("mae_w_by_n", {}).items()}

    for (workload, n_tag, size_mb), group_runs in groups.items():
        sram7 = pick_best_run(find_runs_by_label(group_runs, "SRAM7"), baseline_preferences.get("SRAM7", []))
        cf = pick_best_run(find_runs_by_label(group_runs, "Counterfactual"), baseline_preferences.get("Counterfactual", []))
        if cf is None:
            cf = pick_best_run(find_runs_by_label(group_runs, "counterfactual_fixed"), baseline_preferences.get("counterfactual_fixed", []))
        if cf is None:
            cf = pick_best_run(find_runs_by_label(group_runs, "smartCounterfactual"), baseline_preferences.get("smartCounterfactual", []))

        cf_speedup_pct = None
        if sram7 and cf:
            cf_speedup_pct = pct_from_ratio(ratio(sram7.get("makespan_s"), cf.get("makespan_s")))

        for run in group_runs:
            sweep_type = None
            param_name = None
            param_value = None

            if run.get("read_latency_factor") is not None:
                sweep_type = "read_latency"
                param_name = "read_latency_factor"
                param_value = run["read_latency_factor"]
            elif run.get("leakage_gap_fraction") is not None:
                sweep_type = "leakage_gap"
                param_name = "leakage_gap_fraction"
                param_value = run["leakage_gap_fraction"]
            elif run.get("source") == "fixed_cap_mae":
                sweep_type = "cap_mae"
                param_name = "cap_delta_w_vs_nominal"
                param_value = run.get("cap_delta_w_vs_nominal")
            else:
                continue

            baseline_speedup_pct = None
            if sram7:
                baseline_speedup_pct = pct_from_ratio(ratio(sram7.get("makespan_s"), run.get("makespan_s")))

            nominal_cap = run.get("nominal_cap_w_from_params")
            cap_delta = run.get("cap_delta_w_vs_nominal")
            mae_w = mae_by_n.get(n_tag)
            sign = None
            if cap_delta is not None:
                if cap_delta > 0:
                    sign = "+"
                elif cap_delta < 0:
                    sign = "-"
                else:
                    sign = "0"

            out.append({
                "sweep_type": sweep_type,
                "param_name": param_name,
                "param_value": param_value,
                "workload": workload,
                "workload_short": run["workload_short"],
                "n_tag": n_tag,
                "n_cores": run["n_cores"],
                "size_mb": size_mb,
                "subject_run_id": run["run_id"],
                "subject_config_label": run["config_label"],
                "subject_source": run["source"],
                "subject_variant_dir": run["variant_dir"],
                "subject_makespan_s": run.get("makespan_s"),
                "baseline_run_id": sram7["run_id"] if sram7 else None,
                "baseline_makespan_s": sram7.get("makespan_s") if sram7 else None,
                "subject_speedup_vs_sram7_pct": baseline_speedup_pct,
                "cf_run_id": cf["run_id"] if cf else None,
                "cf_speedup_vs_sram7_pct": cf_speedup_pct,
                "nominal_cap_w": nominal_cap,
                "subject_cap_w": run.get("cap_w_from_variant"),
                "cap_delta_w": cap_delta,
                "mae_w_for_n": mae_w,
                "cap_delta_sign": sign,
                "read_latency_factor": run.get("read_latency_factor"),
                "leakage_gap_fraction": run.get("leakage_gap_fraction"),
            })

    out.sort(key=lambda r: (r["sweep_type"], r["n_cores"], r["size_mb"], r["workload"], str(r["param_value"])))
    return out


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------
def write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    ensure_dir(os.path.dirname(path))
    if not rows:
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([])
        return

    fieldnames: List[str] = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def build_master_json(meta: Dict[str, Any], runs: List[Dict[str, Any]],
                      per_core: List[Dict[str, Any]],
                      per_interval: List[Dict[str, Any]],
                      comparisons: List[Dict[str, Any]],
                      sweeps: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_run_core: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_run_interval: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_subject_cmp: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for row in per_core:
        by_run_core[row["run_id"]].append(row)
    for row in per_interval:
        by_run_interval[row["run_id"]].append(row)
    for row in comparisons:
        by_subject_cmp[row["subject_run_id"]].append(row)

    nested_runs = []
    for run in sorted(runs, key=sort_key_run):
        entry = dict(run)
        entry["per_core"] = by_run_core.get(run["run_id"], [])
        entry["per_interval"] = by_run_interval.get(run["run_id"], [])
        entry["comparisons"] = by_subject_cmp.get(run["run_id"], [])
        nested_runs.append(entry)

    return {
        "generated_at_utc": now_utc_iso(),
        "meta_path": meta.get("meta_path"),
        "base_dir": meta.get("base_dir"),
        "output_dir": meta.get("output_dir"),
        "runs": nested_runs,
        "sweeps": sweeps,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--meta", default=DEFAULT_META_PATH,
                    help=f"Absolute path to meta.yaml (default: {DEFAULT_META_PATH})")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    meta = load_meta(args.meta)
    meta["meta_path"] = os.path.abspath(args.meta)

    base_dir = meta.get("base_dir")
    output_dir = meta.get("output_dir")
    if not base_dir or not output_dir:
        print("ERROR: meta.yaml must define base_dir and output_dir", file=sys.stderr)
        return 1

    ensure_dir(output_dir)

    oracle_lookup = build_oracle_lookup(meta)
    params_caps = load_params_caps(meta)
    discovered = discover_all_runs(meta)

    runs: List[Dict[str, Any]] = []
    per_core: List[Dict[str, Any]] = []
    per_interval: List[Dict[str, Any]] = []

    for run_stub in discovered:
        run_row, core_rows, interval_rows = build_run_record(run_stub, meta, oracle_lookup, params_caps)
        if not run_row:
            continue
        runs.append(run_row)
        per_core.extend(core_rows)
        per_interval.extend(interval_rows)

    runs.sort(key=sort_key_run)
    per_core.sort(key=lambda r: (r["n_cores"], r["size_mb"], r["workload"], r["run_id"], r["core"]))
    per_interval.sort(key=lambda r: (r["n_cores"], r["size_mb"], r["workload"], r["run_id"], r["interval_idx"]))

    comparisons = build_core_comparisons(meta, runs)
    comparisons.sort(key=lambda r: (r["rule_id"], r["n_cores"], r["size_mb"], r["workload"], r["metric_name"], r["subject_run_id"]))

    sweeps = build_sweeps(meta, runs)

    outputs = meta.get("outputs", {})
    write_csv(outputs["runs_csv"], runs)
    write_csv(outputs["per_core_csv"], per_core)
    write_csv(outputs["per_interval_csv"], per_interval)
    write_csv(outputs["comparisons_csv"], comparisons)
    write_csv(outputs["sweeps_csv"], sweeps)

    master = build_master_json(meta, runs, per_core, per_interval, comparisons, sweeps)
    with open(outputs["master_json"], "w") as f:
        json.dump(master, f, indent=2, sort_keys=False)

    summary = {
        "generated_at_utc": now_utc_iso(),
        "meta_path": meta["meta_path"],
        "output_dir": output_dir,
        "n_runs": len(runs),
        "n_per_core_rows": len(per_core),
        "n_per_interval_rows": len(per_interval),
        "n_comparisons": len(comparisons),
        "n_sweeps": len(sweeps),
        "outputs": outputs,
    }
    with open(os.path.join(output_dir, "manifest.json"), "w") as f:
        json.dump(summary, f, indent=2, sort_keys=False)

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
