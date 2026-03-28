#!/usr/bin/env python3
"""
gen_plm_cap_w.py — Compute per-workload PLM power caps from oracle data.

For each (workload, capacity), computes:
    cap_w = P_nocache_oracle(f_base) + LLC_leak_sram(capacity)

Prints YAML fragment suitable for pasting into params.yaml under plm_cap_w.

Usage:
    python3 mx3/tools/gen_plm_cap_w.py \
        --oracle-csv repro/calibration/plm_calib_sunnycove/runs/oracle_points.csv \
        --sram-device mx3/config/devices/sunnycove/sram14.yaml \
        --base-freq 2.2
"""
import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path


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


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--oracle-csv", required=True)
    ap.add_argument("--sram-device", required=True)
    ap.add_argument("--base-freq", type=float, default=2.2)
    ap.add_argument("--capacities", default="16,32,128",
                    help="Comma-separated LLC capacities in MB")
    args = ap.parse_args()

    capacities = [int(x) for x in args.capacities.split(",")]

    # Load sram leakage
    sram = cheap_yaml_load(Path(args.sram_device).read_text())
    leak_w = {}
    for cap in capacities:
        entry = sram.get(str(cap)) or sram.get(cap)
        if entry and isinstance(entry, dict) and "leak_mw" in entry:
            leak_w[cap] = entry["leak_mw"] / 1000.0
        else:
            print(f"[WARN] No leak_mw for {cap}MB in {args.sram_device}", file=sys.stderr)
            leak_w[cap] = 0.0

    # Load oracle CSV, filter to base_freq
    # Key: (ncores, bench, capacity) -> P_nocache (y_PminusLLC)
    data = defaultdict(dict)  # {ncores: {(bench, cap): P_nocache}}
    with open(args.oracle_csv) as f:
        for row in csv.DictReader(f):
            f_ghz = float(row["f_ghz"])
            if abs(f_ghz - args.base_freq) > 0.05:
                continue
            m = re.search(r"/n(\d+)/", row["run_dir"])
            nc = int(m.group(1)) if m else 1
            bench = row["bench"]
            cap = int(row["size_mb"])
            if cap not in capacities:
                continue
            p_nocache = float(row["y_PminusLLC"])
            data[nc][(bench, cap)] = p_nocache

    # Print YAML
    print("    plm_cap_w:")
    for nc in sorted(data.keys()):
        print(f"      n{nc}:")
        # Group by bench
        benches = sorted(set(b for b, _ in data[nc].keys()))
        for bench in benches:
            needs_quote = "+" in bench
            bkey = f'"{bench}"' if needs_quote else bench
            print(f"        {bkey}:")
            for cap in capacities:
                p = data[nc].get((bench, cap))
                if p is not None:
                    cap_w = p + leak_w[cap]
                    print(f"          {cap}: {cap_w:.2f}")
    print()

    # Also print condensed summary
    print("# Summary: cap_w range per core count", file=sys.stderr)
    for nc in sorted(data.keys()):
        vals = [data[nc][(b, c)] + leak_w[c] for (b, c) in data[nc] if c in leak_w]
        if vals:
            print(f"#   n{nc}: {min(vals):.1f} – {max(vals):.1f} W", file=sys.stderr)


if __name__ == "__main__":
    main()
