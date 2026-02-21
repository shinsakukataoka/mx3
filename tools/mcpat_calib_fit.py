#!/usr/bin/env python3
import argparse, csv, os, subprocess, sys
from pathlib import Path
from typing import Dict, List, Tuple

def parse_mcpat_table(path: Path) -> Tuple[float, float, float]:
    """Parse Sniper mcpat.py stdout table (mcpat_table.txt)."""
    p_total = None
    p_cache = None
    with path.open("r", errors="ignore") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "total":
                p_total = float(parts[1])
            elif len(parts) >= 2 and parts[0] == "cache":
                p_cache = float(parts[1])
    if p_total is None or p_cache is None:
        raise RuntimeError(f"Missing total/cache in {path}")
    return p_total, p_cache, (p_total - p_cache)

def run_mcpat(sniper_home: Path, run_dir: Path) -> bool:
    """Run Sniper's mcpat.py and save stdout table to mcpat_table.txt."""
    tool = sniper_home / "tools" / "mcpat.py"
    if not tool.exists():
        raise RuntimeError(f"Missing {tool}")
    cmd = [sys.executable, str(tool), "-d", str(run_dir), "-t", "total", "-o", "mcpat_total"]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
    if r.returncode != 0:
        return False
    (run_dir / "mcpat_table.txt").write_text(r.stdout)
    return True

def read_csv_rows(csv_path: Path) -> List[Dict[str, str]]:
    with csv_path.open(newline="") as f:
        r = csv.DictReader(f)
        return list(r)

def write_csv(out_path: Path, rows: List[Dict[str, str]]) -> None:
    base = list(rows[0].keys())
    extra = []
    for row in rows:
        for k in row.keys():
            if k not in base and k not in extra:
                extra.append(k)
    fieldnames = base + extra
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row)

def filtered_points(rows: List[Dict[str, str]], exclude: set, u_max: float, y_domain: str) -> List[Tuple[str, float, float]]:
    pts = []
    for row in rows:
        bench = row.get("bench", "")
        if bench in exclude:
            continue
        try:
            U = float(row["U_sum"])
        except Exception:
            continue
        if U > u_max:
            continue
        try:
            x = float(row["x_fU"])
        except Exception:
            continue

        if y_domain == "nocache":
            y_str = row.get("P_nocache_W", "")
        else:
            y_str = row.get("P_total_W", "")
        if not y_str:
            continue
        y = float(y_str)
        pts.append((bench, x, y))
    return pts

def fit_pooled(points: List[Tuple[str, float, float]]) -> Dict[str, float]:
    xs = [x for _, x, _ in points]
    ys = [y for _, _, y in points]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    varx = sum((x - mx) ** 2 for x in xs)
    cov  = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    m = cov / varx
    b = my - m * mx
    yhat = [b + m * x for x in xs]
    abs_err = [abs(y - yh) for y, yh in zip(ys, yhat)]
    abs_err.sort()
    mae = sum(abs_err) / n
    p95 = abs_err[int(0.95 * (n - 1))]
    ss_res = sum((y - yh) ** 2 for y, yh in zip(ys, yhat))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return {"b": b, "m": m, "r2": r2, "mae": mae, "p95": p95, "n": float(n)}

def fit_fixed_effect(points: List[Tuple[str, float, float]]) -> Dict[str, float]:
    by = {}
    for b, x, y in points:
        by.setdefault(b, []).append((x, y))

    xs_d, ys_d = [], []
    xbar, ybar = {}, {}
    for b, pts in by.items():
        xb = sum(x for x, _ in pts) / len(pts)
        yb = sum(y for _, y in pts) / len(pts)
        xbar[b] = xb
        ybar[b] = yb
        for x, y in pts:
            xs_d.append(x - xb)
            ys_d.append(y - yb)

    num = sum(x * y for x, y in zip(xs_d, ys_d))
    den = sum(x * x for x in xs_d)
    m = num / den

    betas = {b: (ybar[b] - m * xbar[b]) for b in by.keys()}
    b_avg = sum(betas.values()) / len(betas)

    resid = []
    for b, pts in by.items():
        bb = betas[b]
        for x, y in pts:
            resid.append(y - (bb + m * x))

    abs_err = [abs(e) for e in resid]
    abs_err.sort()
    n = len(abs_err)
    mae = sum(abs_err) / n
    p95 = abs_err[int(0.95 * (n - 1))]

    yhat_d = [m * x for x in xs_d]
    my = sum(ys_d) / len(ys_d)
    ss_res = sum((y - yh) ** 2 for y, yh in zip(ys_d, yhat_d))
    ss_tot = sum((y - my) ** 2 for y in ys_d)
    r2_within = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")

    return {"b": b_avg, "m": m, "r2": r2_within, "mae": mae, "p95": p95, "n": float(n)}

def process_root(root: Path, sniper_home: Path, args):
    in_csv = root / args.oracle
    if not in_csv.exists():
        raise SystemExit(f"Missing {in_csv}")

    rows = read_csv_rows(in_csv)
    if not rows:
        raise SystemExit(f"No rows found in {in_csv}")

    # enrich rows with P_cache_W / P_nocache_W from mcpat_table.txt
    for row in rows:
        run_dir = Path(row["run_dir"])
        table = run_dir / "mcpat_table.txt"
        if not table.exists():
            if args.run_mcpat_missing:
                ok = run_mcpat(sniper_home, run_dir)
                if not ok:
                    row["P_cache_W"] = ""
                    row["P_nocache_W"] = ""
                    continue
            else:
                row["P_cache_W"] = ""
                row["P_nocache_W"] = ""
                continue

        p_total, p_cache, p_nocache = parse_mcpat_table(table)
        row["P_cache_W"] = f"{p_cache:.6f}"
        row["P_nocache_W"] = f"{p_nocache:.6f}"
        # also normalize total in case extractor used a different precision
        row["P_total_W"] = f"{p_total:.6f}"

    out_csv = root / args.out
    write_csv(out_csv, rows)

    exclude = set(x.strip() for x in args.exclude.split(",") if x.strip())
    pts = filtered_points(rows, exclude, args.u_max, args.y_domain)
    if len(pts) < 3:
        raise SystemExit(f"Not enough points after filtering in {root} (have {len(pts)})")

    pooled = fit_pooled(pts)
    fe = fit_fixed_effect(pts)
    return out_csv, pooled, fe

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", nargs="+", required=True,
                    help="One or more calibration roots containing oracle_points.csv")
    ap.add_argument("--sniper-home", default=os.environ.get("SNIPER_HOME", ""),
                    help="Sniper home (or set SNIPER_HOME)")
    ap.add_argument("--oracle", default="oracle_points.csv",
                    help="Input oracle CSV name (default: oracle_points.csv)")
    ap.add_argument("--out", default="oracle_points_plus.csv",
                    help="Output CSV name (default: oracle_points_plus.csv)")
    ap.add_argument("--run-mcpat-missing", action="store_true",
                    help="Run mcpat.py if mcpat_table.txt missing")
    ap.add_argument("--exclude", default="",
                    help="Comma-separated benches to exclude (default none)")
    ap.add_argument("--u-max", type=float, default=1.05,
                    help="Drop points with U_sum > u-max (default 1.05)")
    ap.add_argument("--y-domain", choices=["total", "nocache"], default="nocache",
                    help="Fit y = P_total (total) or y = P_total - P_cache (nocache). Default: nocache")
    ap.add_argument("--fit", choices=["fixed_effect", "pooled", "both"], default="fixed_effect",
                    help="Which regression to print (default: fixed_effect)")
    args = ap.parse_args()

    sniper_home = Path(args.sniper_home).resolve() if args.sniper_home else None
    if not sniper_home or not (sniper_home / "tools" / "mcpat.py").exists():
        raise SystemExit("Set --sniper-home or export SNIPER_HOME so tools/mcpat.py exists")

    fe_bs, fe_ms = [], []

    for r in args.root:
        root = Path(r).resolve()
        out_csv, pooled, fe = process_root(root, sniper_home, args)
        print(f"[OK] wrote {out_csv}")
        print(f"Root: {root}")

        if args.fit in ("pooled", "both"):
            print("  Pooled OLS: y = b + m x")
            print(f"    points: {int(pooled['n'])}   R^2: {pooled['r2']:.6f}   MAE: {pooled['mae']:.6f} W   p95: {pooled['p95']:.6f} W")
            print(f"    b = {pooled['b']:.6f} W")
            print(f"    m = {pooled['m']:.6f} W/(GHz*util)")

        if args.fit in ("fixed_effect", "both"):
            print("  Fixed-effect: y = beta_b + m x")
            print(f"    points: {int(fe['n'])}   within-R^2: {fe['r2']:.6f}   MAE: {fe['mae']:.6f} W   p95: {fe['p95']:.6f} W")
            print(f"    b_avg (P_static-like) = {fe['b']:.6f} W")
            print(f"    m (k_dyn-like)        = {fe['m']:.6f} W/(GHz*util)")
            fe_bs.append(fe["b"])
            fe_ms.append(fe["m"])

        print()

    if len(fe_bs) >= 2:
        b_global = sum(fe_bs) / len(fe_bs)
        m_global = sum(fe_ms) / len(fe_ms)
        print("Recommended global parameters (avg over roots, fixed-effect):")
        print(f"  P_static = {b_global:.6f} W")
        print(f"  k_dyn    = {m_global:.6f} W/(GHz*util)")

if __name__ == "__main__":
    main()