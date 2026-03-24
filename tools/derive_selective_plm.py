#!/usr/bin/env python3
"""Derive selective (per-core, k=1) PLM coefficients from existing per-socket calibrations.

When only 1 out of N cores is boosted to frequency f while (N-1) remain at f_base,
the total power delta is approximately 1/N of the all-cores-at-f delta:

    coeff_sel(f) = coeff(f_base) + (1/N) * [coeff(f) - coeff(f_base)]

This script reads an existing PLM calibration file (with any step size) and produces
a new file with selective-aware coefficients.  The output filename appends '_selk1'.

Usage:
    python3 derive_selective_plm.py --n-cores 4 --f-base 2.2 FILE [FILE ...]
    python3 derive_selective_plm.py --n-cores 4 --f-base 2.2 results_test/plm_calibrate/plm_sunnycove_n4_cal_step025.sh
"""
import argparse, re, sys
from pathlib import Path


def parse_cal(path: Path):
    """Return (header_lines, freqs, bs, a_utils, a_ipcs) from a cal.sh."""
    text = path.read_text()
    header = []
    for line in text.splitlines():
        if line.startswith("PLM_"):
            break
        header.append(line)

    def extract(name):
        m = re.search(rf'{name}=\(\s*(.*?)\s*\)', text, re.DOTALL)
        if not m:
            raise ValueError(f"Cannot find {name} in {path}")
        return [float(x) for x in m.group(1).split()]

    freqs = extract("PLM_F")
    bs = extract("PLM_B")
    a_utils = extract("PLM_AUTIL")
    a_ipcs = extract("PLM_AIPC")
    assert len(freqs) == len(bs) == len(a_utils) == len(a_ipcs)
    return header, freqs, bs, a_utils, a_ipcs


def find_base_idx(freqs, f_base, tol=0.001):
    """Find index of f_base in freqs."""
    for i, f in enumerate(freqs):
        if abs(f - f_base) < tol:
            return i
    raise ValueError(f"f_base={f_base} not found in freqs={freqs[:5]}...")


def derive_selective(freqs, vals, base_idx, n_cores):
    """coeff_sel(f) = coeff(f_base) + (1/N) * (coeff(f) - coeff(f_base))"""
    v_base = vals[base_idx]
    return [v_base + (v - v_base) / n_cores for v in vals]


def write_cal(out_path, header, freqs, bs, a_utils, a_ipcs, n_cores, src):
    n = len(freqs)
    with out_path.open("w") as f:
        for h in header:
            f.write(h + "\n")
        f.write(f"# Selective DVFS (k=1) coefficients derived from {src}\n")
        f.write(f"# Formula: coeff(f) = coeff(f_base) + (1/{n_cores}) * delta\n")
        f.write(f"PLM_N={n}\n")

        def fmt_arr(name, vals):
            parts = "  ".join(f"{v:8.4f}" for v in vals)
            f.write(f"{name}=( {parts}  )\n")

        fmt_arr("PLM_F", freqs)
        fmt_arr("PLM_B", bs)
        fmt_arr("PLM_AUTIL", a_utils)
        fmt_arr("PLM_AIPC", a_ipcs)

    print(f"  [OK] {out_path}  ({n} entries, k=1/{n_cores} scaling)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="PLM cal files to process")
    ap.add_argument("--n-cores", type=int, required=True,
                    help="Number of cores (4 or 8)")
    ap.add_argument("--f-base", type=float, default=2.2,
                    help="Base frequency in GHz (default 2.2)")
    ap.add_argument("--suffix", default="_selk1",
                    help="Output suffix (default: _selk1)")
    args = ap.parse_args()

    for fpath in args.files:
        p = Path(fpath)
        if not p.exists():
            print(f"  [SKIP] {p} not found")
            continue

        out = p.with_name(p.stem + args.suffix + ".sh")
        print(f"  Processing: {p.name}  (n_cores={args.n_cores}, f_base={args.f_base})")

        header, freqs, bs, a_utils, a_ipcs = parse_cal(p)
        base_idx = find_base_idx(freqs, args.f_base)

        sel_bs = derive_selective(freqs, bs, base_idx, args.n_cores)
        sel_au = derive_selective(freqs, a_utils, base_idx, args.n_cores)
        sel_ai = derive_selective(freqs, a_ipcs, base_idx, args.n_cores)

        write_cal(out, header, freqs, sel_bs, sel_au, sel_ai,
                  args.n_cores, p.name)

    print(f"\nDone. Selective k=1 coefficients derived for n_cores={args.n_cores}")


if __name__ == "__main__":
    main()
