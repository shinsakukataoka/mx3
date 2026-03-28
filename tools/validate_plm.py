#!/usr/bin/env python3
"""
validate_plm.py — Unified PLM validation: bias, MAE, MAPE, and DVFS
decision agreement vs McPAT oracle.

Usage:
    python3 mx3/tools/validate_plm.py \
        --calib-dir repro/calibration/plm_calib_sunnycove \
        --models-dir repro/calibration/models \
        --sniper-home ~/src/sniper \
        --sram-device mx3/config/devices/sunnycove/sram14.yaml \
        --mram-device mx3/config/devices/sunnycove/mram14.yaml

For each model .sh file in --models-dir, evaluates against oracle data
from --calib-dir and reports:
  - Bias (mean signed error), MAE, MAPE
  - DVFS decision agreement (boost/hold/down) vs McPAT oracle
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# YAML loader (lightweight, no dependency)
# ---------------------------------------------------------------------------
def cheap_yaml_load(text: str) -> dict:
    root: dict = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        m = re.match(r"^\s*([A-Za-z0-9_.-]+):\s*(.*)$", raw)
        if not m:
            continue
        key, rest = m.group(1), m.group(2)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else root
        if rest == "" or rest in ("|", ">"):
            newd: dict = {}
            parent[key] = newd
            stack.append((indent, newd))
        else:
            try:
                parent[key] = float(rest)
            except ValueError:
                parent[key] = rest.strip().strip('"').strip("'")
    return root


# ---------------------------------------------------------------------------
# PLM loader
# ---------------------------------------------------------------------------
def parse_plm_sh(path: Path) -> Dict[float, Tuple[float, float, float]]:
    """Parse a PLM cal.sh file into {f_ghz: (b_f, a_util, a_ipc)}."""
    text = path.read_text()
    def _arr(name):
        m = re.search(rf"{name}=\(\s*(.*?)\)", text, re.DOTALL)
        if not m:
            raise RuntimeError(f"Cannot find {name} in {path}")
        return [float(x) for x in m.group(1).split()]
    fs = _arr("PLM_F"); bs = _arr("PLM_B")
    a_us = _arr("PLM_AUTIL"); a_is = _arr("PLM_AIPC")
    return {round(f, 2): (b, au, ai) for f, b, au, ai in zip(fs, bs, a_us, a_is)}


def eval_plm(plm: dict, f_ghz: float, u_sum: float, ipc: float) -> float:
    f_key = round(f_ghz, 2)
    if f_key not in plm:
        f_key = min(plm.keys(), key=lambda x: abs(x - f_ghz))
    b, a_u, a_i = plm[f_key]
    return b + a_u * u_sum + a_i * u_sum * ipc


# ---------------------------------------------------------------------------
# IPC extraction via sniper_lib
# ---------------------------------------------------------------------------
def extract_ipc(run_dir: Path, sniper_home: Path) -> float:
    script = f"""\
import os, sys
sys.path.insert(0, {str(sniper_home / "tools")!r})
import sniper_lib
r = sniper_lib.get_results(resultsdir={str(run_dir)!r}, partial=None)
res = r["results"]
cfg = r.get("config", {{}})
instr         = res.get("performance_model.instruction_count", [])
global_time   = float(res.get("global.time", 0))
f_ghz         = float(cfg.get("perf_model/core/frequency", 2.0))
n_cores_sim   = len(instr)
total_ins     = sum(float(x) for x in instr)
total_cycles  = global_time * f_ghz * 1e-6
ipc = total_ins / (n_cores_sim * total_cycles) if total_cycles > 0 else 0.0
print(f"{{ipc:.10f}}")
"""
    r = subprocess.run([sys.executable, "-c", script],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:200])
    return float(r.stdout.strip())


# ---------------------------------------------------------------------------
# Oracle data loader
# ---------------------------------------------------------------------------
def load_oracle_csv(csv_path: Path) -> List[Dict]:
    """Load master oracle_points.csv, infer ncores from run_dir path."""
    rows = []
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            run_dir = row["run_dir"]
            # Infer ncores from /n1/, /n4/, /n8/ in path
            m = re.search(r"/n(\d+)/", run_dir)
            ncores = int(m.group(1)) if m else 1
            rows.append({
                "run_dir": run_dir,
                "bench": row["bench"],
                "size_mb": int(row["size_mb"]),
                "f_ghz": float(row["f_ghz"]),
                "U_sum": float(row["U_sum"]),
                "P_total_W": float(row["P_total_W"]),
                "P_llc_leak_W": float(row["P_llc_leak_W"]),
                "y_PminusLLC": float(row["y_PminusLLC"]),
                "ncores": ncores,
            })
    return rows


# ---------------------------------------------------------------------------
# Parse model filename to extract combo and capacity
# ---------------------------------------------------------------------------
def parse_model_name(filename: str) -> Optional[Tuple[str, int]]:
    """Parse 'plm_sunnycove_n1n4_cal_32M.sh' → ('n1n4', 32)."""
    m = re.match(r"plm_\w+_(n\w+)_cal_(\d+)M\.sh$", filename)
    if not m:
        return None
    return m.group(1), int(m.group(2))


def combo_ncores(combo: str) -> List[int]:
    """'n1n4n8' → [1, 4, 8], 'n4' → [4]."""
    return [int(x) for x in re.findall(r"(\d+)", combo)]


# ---------------------------------------------------------------------------
# DVFS decision
# ---------------------------------------------------------------------------
HYSTERESIS = 0.10  # W

def dvfs_decision(p_est: float, p_cap: float) -> str:
    if p_est < p_cap - HYSTERESIS:
        return "boost"
    if p_est > p_cap + HYSTERESIS:
        return "down"
    return "hold"


# ---------------------------------------------------------------------------
# Clean workload filters
# ---------------------------------------------------------------------------
N4_CLEAN = {
    "502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r",
    "505.mcf_r+500.perlbench_r+648.exchange2_s+649.fotonik3d_s",
    "505.mcf_r+505.mcf_r+502.gcc_r+502.gcc_r",
    "505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r",
    "523.xalancbmk_r+523.xalancbmk_r+502.gcc_r+502.gcc_r",
}
N8_CLEAN = {
    "502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r",
    "505.mcf_r+505.mcf_r+500.perlbench_r+500.perlbench_r+648.exchange2_s+648.exchange2_s+649.fotonik3d_s+649.fotonik3d_s",
    "505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r",
    "505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r",
    "523.xalancbmk_r+523.xalancbmk_r+523.xalancbmk_r+523.xalancbmk_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r",
}


def is_clean(bench: str, ncores: int) -> bool:
    if ncores == 1:
        return True
    if ncores == 4:
        return bench in N4_CLEAN
    if ncores == 8:
        return bench in N8_CLEAN
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
BASE_FREQ = 2.2  # GHz — baseline for P_cap computation


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--calib-dir", required=True,
                    help="Calibration run directory (contains runs/oracle_points.csv)")
    ap.add_argument("--models-dir", required=True,
                    help="Directory containing plm_*_cal_*M.sh model files")
    ap.add_argument("--sniper-home", default=os.environ.get("SNIPER_HOME", ""),
                    help="Sniper installation directory")
    ap.add_argument("--sram-device", required=True,
                    help="Path to SRAM device YAML (for LLC leakage in P_cap)")
    ap.add_argument("--mram-device", default="",
                    help="Path to MRAM device YAML (for LLC leakage in P_total_est)")
    ap.add_argument("--base-freq", type=float, default=BASE_FREQ,
                    help=f"Baseline frequency for P_cap (default: {BASE_FREQ})")
    args = ap.parse_args()

    calib_dir = Path(args.calib_dir).resolve()
    models_dir = Path(args.models_dir).resolve()
    sniper_home = Path(args.sniper_home).resolve() if args.sniper_home else None

    master_csv = calib_dir / "runs" / "oracle_points.csv"
    if not master_csv.exists():
        raise SystemExit(f"[ERR] Oracle CSV not found: {master_csv}")

    # Load device leakage values
    sram_data = cheap_yaml_load(Path(args.sram_device).read_text())
    llc_leak_sram = {}  # {capacity_mb: leak_W}
    for cap_str, entry in sram_data.items():
        if isinstance(entry, dict) and "leak_mw" in entry:
            llc_leak_sram[int(cap_str)] = entry["leak_mw"] / 1000.0

    llc_leak_mram = {}
    if args.mram_device:
        mram_data = cheap_yaml_load(Path(args.mram_device).read_text())
        for cap_str, entry in mram_data.items():
            if isinstance(entry, dict) and "leak_mw" in entry:
                llc_leak_mram[int(cap_str)] = entry["leak_mw"] / 1000.0

    # Load all oracle data
    print("[1/3] Loading oracle data ...")
    all_oracle = load_oracle_csv(master_csv)
    print(f"  {len(all_oracle)} total oracle points")

    # Index by (ncores, capacity, bench, f_ghz) for fast lookup
    oracle_by_key: Dict[Tuple, Dict] = {}
    for r in all_oracle:
        key = (r["ncores"], r["size_mb"], r["bench"], r["f_ghz"])
        oracle_by_key[key] = r

    # Find all model files
    model_files = sorted(models_dir.glob("plm_*_cal_*M.sh"))
    if not model_files:
        raise SystemExit(f"[ERR] No model files found in {models_dir}")
    print(f"  {len(model_files)} model files found")

    # Extract IPC cache (expensive — avoid re-computing for each model)
    print("\n[2/3] Extracting IPC from simulation outputs ...")
    ipc_cache: Dict[str, float] = {}
    n_ipc_ok = 0
    n_ipc_fail = 0
    for r in all_oracle:
        rd = r["run_dir"]
        if rd in ipc_cache:
            continue
        try:
            ipc_cache[rd] = extract_ipc(Path(rd), sniper_home)
            n_ipc_ok += 1
        except Exception as e:
            n_ipc_fail += 1
    print(f"  IPC extracted: {n_ipc_ok} ok, {n_ipc_fail} failed")

    # Evaluate each model
    print("\n[3/3] Evaluating models ...")
    all_detail = []
    model_summaries = []

    for model_path in model_files:
        parsed = parse_model_name(model_path.name)
        if parsed is None:
            print(f"  [SKIP] Cannot parse: {model_path.name}")
            continue

        combo, cap_mb = parsed
        target_ncores = combo_ncores(combo)
        plm = parse_plm_sh(model_path)

        # P_cap: for each (ncores, bench), we use oracle power at base_freq + sram leakage
        # Collect relevant oracle points
        points_for_model = []
        for r in all_oracle:
            if r["ncores"] not in target_ncores:
                continue
            if r["size_mb"] != cap_mb:
                continue
            if not is_clean(r["bench"], r["ncores"]):
                continue
            if r["run_dir"] not in ipc_cache:
                continue
            points_for_model.append(r)

        if not points_for_model:
            print(f"  {combo} {cap_mb}MB: no matching oracle points")
            continue

        # Build per-(ncores, bench) baseline power at base_freq for P_cap
        base_power: Dict[Tuple[int, str], float] = {}
        for r in points_for_model:
            if abs(r["f_ghz"] - args.base_freq) < 0.05:
                base_power[(r["ncores"], r["bench"])] = r["y_PminusLLC"]

        sram_leak = llc_leak_sram.get(cap_mb, 0.0)
        mram_leak = llc_leak_mram.get(cap_mb, 0.0)

        errors = []
        decisions_oracle = []
        decisions_plm = []

        for r in points_for_model:
            ipc = ipc_cache.get(r["run_dir"])
            if ipc is None:
                continue

            p_nocache_actual = r["y_PminusLLC"]
            p_nocache_pred = eval_plm(plm, r["f_ghz"], r["U_sum"], ipc)

            err = p_nocache_pred - p_nocache_actual
            pct = 100.0 * err / p_nocache_actual if p_nocache_actual != 0 else math.nan

            # P_cap = P_nocache_oracle(base_freq) + sram_leakage
            bp = base_power.get((r["ncores"], r["bench"]))
            if bp is not None:
                p_cap = bp + sram_leak

                # DVFS decisions
                p_total_oracle = p_nocache_actual + mram_leak
                p_total_plm = p_nocache_pred + mram_leak

                dec_oracle = dvfs_decision(p_total_oracle, p_cap)
                dec_plm = dvfs_decision(p_total_plm, p_cap)
                dec_agree = dec_oracle == dec_plm
            else:
                p_cap = None
                dec_oracle = dec_plm = ""
                dec_agree = None

            row = {
                "model": f"{combo}_{cap_mb}M",
                "combo": combo,
                "capacity_mb": cap_mb,
                "ncores": r["ncores"],
                "workload": r["bench"],
                "f_ghz": r["f_ghz"],
                "U_sum": r["U_sum"],
                "IPC": ipc,
                "p_actual_w": p_nocache_actual,
                "p_predicted_w": p_nocache_pred,
                "err_w": err,
                "err_pct": pct,
                "dec_oracle": dec_oracle,
                "dec_plm": dec_plm,
                "dec_agree": dec_agree,
            }

            all_detail.append(row)
            errors.append({"err": err, "pct": pct})
            if dec_agree is not None:
                decisions_oracle.append(dec_oracle)
                decisions_plm.append(dec_plm)

        # Model-level summary
        if errors:
            errs = [e["err"] for e in errors]
            pcts = [abs(e["pct"]) for e in errors if not math.isnan(e["pct"])]
            bias = sum(errs) / len(errs)
            mae = sum(abs(e) for e in errs) / len(errs)
            mape = sum(pcts) / len(pcts) if pcts else math.nan

            n_agree = sum(1 for o, p in zip(decisions_oracle, decisions_plm) if o == p)
            n_dec = len(decisions_oracle)
            dec_pct = 100.0 * n_agree / n_dec if n_dec > 0 else math.nan

            # Per-action breakdown
            action_stats = {}
            for action in ["boost", "hold", "down"]:
                subset = [(o, p) for o, p in zip(decisions_oracle, decisions_plm) if o == action]
                n_action = len(subset)
                n_correct = sum(1 for o, p in subset if o == p)
                action_stats[action] = {
                    "n": n_action,
                    "agree_pct": 100.0 * n_correct / n_action if n_action > 0 else math.nan,
                }

            summary = {
                "model": f"{combo}_{cap_mb}M",
                "combo": combo,
                "capacity_mb": cap_mb,
                "n_points": len(errors),
                "bias_w": bias,
                "mae_w": mae,
                "mape_pct": mape,
                "dec_agree_pct": dec_pct,
                "n_decisions": n_dec,
                **{f"{a}_n": action_stats[a]["n"] for a in ["boost", "hold", "down"]},
                **{f"{a}_agree_pct": action_stats[a]["agree_pct"] for a in ["boost", "hold", "down"]},
            }
            model_summaries.append(summary)

    # ---------------------------------------------------------------------------
    # Output
    # ---------------------------------------------------------------------------
    out_dir = models_dir

    # 1. Detail CSV
    detail_path = out_dir / "validation_detail.csv"
    detail_cols = ["model", "combo", "capacity_mb", "ncores", "workload", "f_ghz",
                   "U_sum", "IPC", "p_actual_w", "p_predicted_w", "err_w", "err_pct",
                   "dec_oracle", "dec_plm", "dec_agree"]
    with open(detail_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=detail_cols, extrasaction="ignore")
        w.writeheader()
        for r in all_detail:
            out = {}
            for c in detail_cols:
                v = r.get(c, "")
                if isinstance(v, float):
                    out[c] = f"{v:.6f}"
                elif isinstance(v, bool):
                    out[c] = str(v)
                else:
                    out[c] = v
            w.writerow(out)
    print(f"\n  Wrote {detail_path} ({len(all_detail)} rows)")

    # 2. Summary CSV
    summary_path = out_dir / "validation_summary.csv"
    summary_cols = ["model", "combo", "capacity_mb", "n_points",
                    "bias_w", "mae_w", "mape_pct", "dec_agree_pct", "n_decisions",
                    "boost_n", "boost_agree_pct", "hold_n", "hold_agree_pct",
                    "down_n", "down_agree_pct"]
    with open(summary_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=summary_cols, extrasaction="ignore")
        w.writeheader()
        for r in model_summaries:
            out = {}
            for c in summary_cols:
                v = r.get(c, "")
                if isinstance(v, float):
                    out[c] = f"{v:.4f}" if not math.isnan(v) else "N/A"
                else:
                    out[c] = v
            w.writerow(out)
    print(f"  Wrote {summary_path} ({len(model_summaries)} rows)")

    # 3. Print summary table + write to .txt
    lines = []
    def pr(s=""):
        print(s)
        lines.append(s)

    pr()
    pr("=" * 110)
    pr("  PLM VALIDATION SUMMARY")
    pr("=" * 110)
    pr(f"  {'Model':<15} {'n':>5}  {'Bias(W)':>8}  {'MAE(W)':>7}  {'MAPE':>7}  "
       f"{'Dec%':>6}  {'boost':>6} {'bst%':>5}  {'hold':>6} {'hld%':>5}  {'down':>6} {'dwn%':>5}")
    pr("-" * 110)

    for r in model_summaries:
        mape_s = f"{r['mape_pct']:.1f}%" if not math.isnan(r['mape_pct']) else "N/A"
        dec_s = f"{r['dec_agree_pct']:.1f}%" if not math.isnan(r['dec_agree_pct']) else "N/A"
        ba = r['boost_agree_pct']
        ha = r['hold_agree_pct']
        da = r['down_agree_pct']
        ba_s = f"{ba:.0f}%" if not math.isnan(ba) else "--"
        ha_s = f"{ha:.0f}%" if not math.isnan(ha) else "--"
        da_s = f"{da:.0f}%" if not math.isnan(da) else "--"
        pr(f"  {r['model']:<15} {r['n_points']:>5}  {r['bias_w']:>+8.3f}  {r['mae_w']:>7.3f}  {mape_s:>7}  "
           f"{dec_s:>6}  {r['boost_n']:>6} {ba_s:>5}  {r['hold_n']:>6} {ha_s:>5}  {r['down_n']:>6} {da_s:>5}")

    pr("=" * 110)
    pr()

    summary_txt = out_dir / "validation_summary.txt"
    with open(summary_txt, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  Wrote {summary_txt}")


if __name__ == "__main__":
    main()
