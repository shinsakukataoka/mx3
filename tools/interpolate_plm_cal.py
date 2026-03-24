#!/usr/bin/env python3
"""Interpolate PLM calibration .sh files from 0.1 GHz to finer step sizes.

Reads an existing plm_*_cal.sh, linearly interpolates (b, a_util, a_ipc)
between each adjacent pair of calibrated frequencies, and writes a new
file with the finer grid.  The output filename appends '_step{STEP}' before
'.sh', e.g.  plm_sunnycove_n1n4_cal.sh  →  plm_sunnycove_n1n4_cal_step025.sh

Usage:
    python3 interpolate_plm_cal.py --step 0.025 FILE [FILE ...]
    python3 interpolate_plm_cal.py --step 0.025 --glob 'results_test/plm_calibrate/plm_sunnycove_n*_cal*.sh'
"""
import argparse, glob, re, sys
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
    assert len(freqs) == len(bs) == len(a_utils) == len(a_ipcs), \
        f"Length mismatch in {path}"
    return header, freqs, bs, a_utils, a_ipcs


def interpolate(freqs, vals, step):
    """Linearly interpolate vals at every 'step' between freqs[0] and freqs[-1]."""
    new_f, new_v = [], []
    f = freqs[0]
    while f <= freqs[-1] + 1e-9:
        # find surrounding pair
        idx = 0
        while idx < len(freqs) - 2 and freqs[idx + 1] < f - 1e-9:
            idx += 1
        f0, f1 = freqs[idx], freqs[idx + 1]
        t = (f - f0) / (f1 - f0) if abs(f1 - f0) > 1e-12 else 0.0
        v = vals[idx] + t * (vals[idx + 1] - vals[idx])
        new_f.append(round(f, 4))
        new_v.append(v)
        f = round(f + step, 4)
    return new_f, new_v


def write_cal(out_path: Path, header, freqs, bs, a_utils, a_ipcs, step, src):
    n = len(freqs)
    with out_path.open("w") as f:
        for h in header:
            f.write(h + "\n")
        f.write(f"# Interpolated from {src} to step={step} GHz "
                f"({n} frequency entries)\n")
        f.write(f"PLM_N={n}\n")

        def fmt_arr(name, vals):
            parts = "  ".join(f"{v:8.4f}" for v in vals)
            f.write(f"{name}=( {parts}  )\n")

        fmt_arr("PLM_F", freqs)
        fmt_arr("PLM_B", bs)
        fmt_arr("PLM_AUTIL", a_utils)
        fmt_arr("PLM_AIPC", a_ipcs)

    print(f"  [OK] {out_path}  ({n} entries)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="Cal .sh files to interpolate")
    ap.add_argument("--glob", default="", help="Glob pattern for cal files")
    ap.add_argument("--step", type=float, default=0.025,
                    help="New frequency step in GHz (default 0.025)")
    ap.add_argument("--suffix", default="",
                    help="Override output suffix (default: _step{STEP*1000:.0f})")
    args = ap.parse_args()

    files = list(args.files)
    if args.glob:
        files += sorted(glob.glob(args.glob))
    if not files:
        ap.error("No input files")

    suffix = args.suffix or f"_step{int(args.step * 1000):03d}"

    for fpath in files:
        p = Path(fpath)
        if not p.exists():
            print(f"  [SKIP] {p} not found")
            continue
        # Output name: insert suffix before .sh
        out = p.with_name(p.stem + suffix + ".sh")
        print(f"  Processing: {p.name}")

        header, freqs, bs, a_utils, a_ipcs = parse_cal(p)

        new_f, new_b = interpolate(freqs, bs, args.step)
        _, new_au = interpolate(freqs, a_utils, args.step)
        _, new_ai = interpolate(freqs, a_ipcs, args.step)

        write_cal(out, header, new_f, new_b, new_au, new_ai, args.step, p.name)

    print(f"\nDone. New step = {args.step} GHz, suffix = '{suffix}'")


if __name__ == "__main__":
    main()
