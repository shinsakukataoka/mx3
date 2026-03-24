#!/usr/bin/env python3
"""
linear_decision_agreement.py — Validate the simple linear power model by
computing DVFS decision agreement (boost/hold/down) against McPAT oracle
ground truth.

Linear model:  P_nocache = static_w + dyn_w_per_ghz × f × U_sum
               P_total   = P_nocache + llc_leak_w

Oracle truth:  P_total = P_total_W (from calibration runs via McPAT)

For each oracle calibration point, we classify both the linear model and
the oracle into {boost, hold, down} decisions and report agreement rates.

This parallels the PLM decision agreement analysis in PLM.md §14 and
plm_error_cancel.py, but is much simpler since x_fU is already in the CSV.
"""

import csv
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import yaml

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REPO_ROOT  = Path(__file__).resolve().parent.parent.parent
PARAMS     = REPO_ROOT / "mx2" / "config" / "params.yaml"
CALIB_BASE = REPO_ROOT / "results_test" / "plm_calibrate"
OUT_DIR    = CALIB_BASE
UARCH      = "sunnycove"
HYSTERESIS = 0.10  # W — governor deadband

# LLC leakage per capacity (from device YAMLs, mW → W)
LLC_LEAK_MRAM = {16: 0.0538, 32: 0.102434, 128: 0.134785}

# Clean workload lists for n=4 and n=8
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


def decision(p_est, p_cap, h):
    """Governor DVFS decision."""
    if p_est < p_cap - h:
        return "boost"
    if p_est > p_cap + h:
        return "down"
    return "hold"


def load_params():
    """Load linear model coefficients, linear caps, and oracle per-workload caps."""
    with open(PARAMS) as f:
        cfg = yaml.safe_load(f)

    power = cfg["uarch"][UARCH]["power"]
    static_w = power["p_static_w"]
    dyn_w    = power["k_dyn_w_per_ghz_util"]

    # Linear model caps (cap_w): single cap per (ncores, capacity)
    cap_w_raw = cfg["uarch"][UARCH]["cap_w"]
    linear_caps = {}  # {(ncores, cap_mb): cap_w}
    # single → n=1
    for size_mb, cap in cap_w_raw["single"].items():
        linear_caps[(1, int(size_mb))] = float(cap)
    # multicore → n4, n8
    for nkey, sizedict in cap_w_raw["multicore"].items():
        nc = int(nkey[1:])
        for size_mb, cap in sizedict.items():
            linear_caps[(nc, int(size_mb))] = float(cap)

    # Oracle per-workload caps (plm_cap_w)
    oracle_caps = {}
    plm_cap = cfg["uarch"][UARCH]["plm_cap_w"]
    for nkey, benchdict in plm_cap.items():  # n1, n4, n8
        nc = int(nkey[1:])
        oracle_caps[nc] = {}
        for bench, sizedict in benchdict.items():
            oracle_caps[nc][bench] = {int(k): float(v) for k, v in sizedict.items()}

    return static_w, dyn_w, linear_caps, oracle_caps


def load_oracle(ncores, cap_mb):
    """Load oracle CSV, filtered to clean workloads for n≥4."""
    csv_path = (CALIB_BASE /
                f"plm_calib_sunnycove_n{ncores}_{cap_mb}M" /
                "runs" / "oracle_points.csv")
    if not csv_path.exists():
        print(f"  [WARN] Missing: {csv_path}", file=sys.stderr)
        return []

    clean_set = None
    if ncores == 4:
        clean_set = N4_CLEAN
    elif ncores == 8:
        clean_set = N8_CLEAN

    records = []
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            bench = row["bench"]
            if clean_set and bench not in clean_set:
                continue
            records.append({
                "bench":     bench,
                "f_ghz":     float(row["f_ghz"]),
                "U_sum":     float(row["U_sum"]),
                "x_fU":      float(row["x_fU"]),
                "P_total_W": float(row["P_total_W"]),
                "P_llc_W":   float(row["P_llc_leak_W"]),
            })
    return records


def main():
    static_w, dyn_w, linear_caps, oracle_caps = load_params()
    print(f"Linear model: P_nocache = {static_w} + {dyn_w} × f × U_sum")
    print(f"Hysteresis: {HYSTERESIS} W\n")

    all_detail = []

    for ncores in [1, 4, 8]:
        for cap_mb in [16, 32, 128]:
            records = load_oracle(ncores, cap_mb)
            if not records:
                print(f"  [SKIP] n={ncores}, {cap_mb}MB: no oracle data")
                continue

            llc_mram = LLC_LEAK_MRAM[cap_mb]
            bench_oracle_caps = oracle_caps.get(ncores, {})
            p_cap_linear = linear_caps.get((ncores, cap_mb))
            if p_cap_linear is None:
                print(f"  [SKIP] n={ncores}, {cap_mb}MB: no linear cap")
                continue

            n_skip = 0
            for r in records:
                bench = r["bench"]
                # Get per-workload oracle cap
                if bench not in bench_oracle_caps or cap_mb not in bench_oracle_caps[bench]:
                    n_skip += 1
                    continue
                p_cap_oracle = bench_oracle_caps[bench][cap_mb]

                # Linear model prediction
                p_nocache_linear = static_w + dyn_w * r["x_fU"]
                p_total_linear = p_nocache_linear + llc_mram

                # Oracle truth
                p_total_oracle = r["P_total_W"]

                # Decisions — each within its own framework
                dec_linear = decision(p_total_linear, p_cap_linear, HYSTERESIS)
                dec_oracle = decision(p_total_oracle, p_cap_oracle, HYSTERESIS)

                all_detail.append({
                    "workload":       bench,
                    "ncores":         ncores,
                    "capacity_mb":    cap_mb,
                    "f_ghz":          r["f_ghz"],
                    "U_sum":          r["U_sum"],
                    "x_fU":           r["x_fU"],
                    "p_cap_linear_w": p_cap_linear,
                    "p_cap_oracle_w": p_cap_oracle,
                    "p_oracle_w":     p_total_oracle,
                    "p_linear_w":     p_total_linear,
                    "abs_err_w":      p_total_linear - p_total_oracle,
                    "dec_oracle":     dec_oracle,
                    "dec_linear":     dec_linear,
                    "dec_agree":      dec_oracle == dec_linear,
                })

            if n_skip:
                print(f"  [INFO] n={ncores}, {cap_mb}MB: {n_skip} rows skipped (no cap)")

            n_pts = sum(1 for d in all_detail
                        if d["ncores"] == ncores and d["capacity_mb"] == cap_mb)
            print(f"  n={ncores}, {cap_mb}MB: {n_pts} points loaded")

    if not all_detail:
        print("[ERR] No data!", file=sys.stderr)
        sys.exit(1)

    # --- Write detail CSV ---
    detail_cols = [
        "workload", "ncores", "capacity_mb", "f_ghz", "U_sum", "x_fU",
        "p_cap_linear_w", "p_cap_oracle_w", "p_oracle_w", "p_linear_w", "abs_err_w",
        "dec_oracle", "dec_linear", "dec_agree",
    ]
    detail_path = OUT_DIR / "linear_decision_detail.csv"
    with open(detail_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=detail_cols)
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

    # --- Decision agreement summary ---
    def compute_summary(rows, group_keys):
        groups = defaultdict(list)
        for r in rows:
            key = tuple(r[k] for k in group_keys)
            groups[key].append(r)

        summaries = []
        for key, grp in sorted(groups.items()):
            d = dict(zip(group_keys, key))
            n = len(grp)
            d["n_points"] = n

            # Overall agreement
            agree = sum(1 for r in grp if r["dec_agree"])
            d["dec_agree_pct"] = agree / n * 100

            # MAE
            errs = np.array([abs(r["abs_err_w"]) for r in grp])
            d["mae_w"] = float(np.mean(errs))

            # Breakdown by oracle action
            for action in ["boost", "hold", "down"]:
                subset = [r for r in grp if r["dec_oracle"] == action]
                d[f"oracle_{action}_n"] = len(subset)
                if subset:
                    correct = sum(1 for r in subset if r["dec_linear"] == action)
                    d[f"oracle_{action}_agree_pct"] = correct / len(subset) * 100
                else:
                    d[f"oracle_{action}_agree_pct"] = ""

            summaries.append(d)
        return summaries

    by_cap_nc = compute_summary(all_detail, ["capacity_mb", "ncores"])

    # Write summary CSV
    summary_cols = [
        "capacity_mb", "ncores", "n_points", "mae_w", "dec_agree_pct",
        "oracle_boost_n", "oracle_boost_agree_pct",
        "oracle_hold_n", "oracle_hold_agree_pct",
        "oracle_down_n", "oracle_down_agree_pct",
    ]
    summary_path = OUT_DIR / "linear_decision_agreement_summary.csv"
    with open(summary_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=summary_cols, extrasaction="ignore")
        w.writeheader()
        for r in by_cap_nc:
            out = {}
            for c in summary_cols:
                v = r.get(c, "")
                if isinstance(v, float):
                    out[c] = f"{v:.4f}"
                else:
                    out[c] = v
            w.writerow(out)
    print(f"  Wrote {summary_path} ({len(by_cap_nc)} rows)")

    # --- Print summary table (PLM.md §14 style) ---
    print(f"\n{'=' * 100}")
    print("  LINEAR MODEL DECISION AGREEMENT vs ORACLE")
    print(f"  P_nocache = {static_w} + {dyn_w} × f × U_sum")
    print(f"{'=' * 100}")
    print(f"  {'Config':<15} {'n':>5}  {'MAE(W)':>7}  "
          f"{'Overall':>8}  "
          f"{'boost':>6} {'bst%':>5}  "
          f"{'hold':>6} {'hld%':>5}  "
          f"{'down':>6} {'dwn%':>5}")
    print("-" * 100)

    for r in by_cap_nc:
        ba = r["oracle_boost_agree_pct"]
        ha = r["oracle_hold_agree_pct"]
        da = r["oracle_down_agree_pct"]
        ba_s = f"{ba:.0f}%" if isinstance(ba, (int, float)) and ba != "" else "--"
        ha_s = f"{ha:.0f}%" if isinstance(ha, (int, float)) and ha != "" else "--"
        da_s = f"{da:.0f}%" if isinstance(da, (int, float)) and da != "" else "--"
        print(f"  {r['capacity_mb']:>3}MB n={r['ncores']:<2}    "
              f"{r['n_points']:>5}  {r['mae_w']:>7.3f}  "
              f"{r['dec_agree_pct']:>7.1f}%  "
              f"{r['oracle_boost_n']:>6} {ba_s:>5}  "
              f"{r['oracle_hold_n']:>6} {ha_s:>5}  "
              f"{r['oracle_down_n']:>6} {da_s:>5}")
    print()

    # --- Also print comparison-friendly format for PLM.md ---
    print("Decision Agreement Table (for PLM.md):")
    print()
    print("| Config | Overall | Boost (n) | Down (n) |")
    print("| :--- | :--- | :--- | :--- |")
    for r in by_cap_nc:
        ba = r["oracle_boost_agree_pct"]
        da = r["oracle_down_agree_pct"]
        ba_s = f"{ba:.0f}%" if isinstance(ba, (int, float)) and ba != "" else "--"
        da_s = f"{da:.0f}%" if isinstance(da, (int, float)) and da != "" else "--"
        print(f"| {r['capacity_mb']}MB n={r['ncores']} | "
              f"{r['dec_agree_pct']:.1f}% | "
              f"{ba_s} ({r['oracle_boost_n']} pts) | "
              f"{da_s} ({r['oracle_down_n']} pts) |")
    print()


if __name__ == "__main__":
    main()
