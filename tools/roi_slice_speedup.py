#!/usr/bin/env python3
"""Independent ROI Slice speedup calculation for multicore runs.

Problem: Sniper's stop-by-icount distributes 1B instructions across cores,
but the per-core split changes with DVFS decisions, making global.time
unreliable for A-vs-B comparisons.

Solution: For each core, compute throughput = delta_instructions / delta_time
from the ROI region. Then normalize to a fixed instruction target (e.g.,
250M per core for n=4). The multicore execution time is
max(target_instructions / throughput_i) across all cores.

Usage:
    python3 roi_slice_speedup.py results_test/plm_sweep/finestep/runs
"""
import os
import sys
import re
import sqlite3
from collections import defaultdict


def extract_per_core_roi(db_path, n_cores):
    """Extract per-core instruction count and elapsed time deltas from SQLite."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    result = {}
    for nid, name in [(19, 'instructions'), (554, 'elapsed_time')]:
        cur.execute(f"""
            SELECT v.core,
                   MAX(CASE WHEN p.prefixname='roi-end' THEN v.value END) -
                   MAX(CASE WHEN p.prefixname='roi-begin' THEN v.value END) as delta
            FROM "values" v
            JOIN prefixes p ON v.prefixid = p.prefixid
            WHERE v.nameid = {nid} AND v.core < {n_cores}
            GROUP BY v.core ORDER BY v.core
        """)
        result[name] = {r[0]: r[1] for r in cur.fetchall()}

    conn.close()
    return result


def compute_normalized_time(roi_data, n_cores, target_ins_per_core):
    """Compute normalized multicore time using independent ROI slices.

    For each core: time_normalized = target_ins / throughput
    where throughput = roi_instructions / roi_elapsed_time
    Multicore time = max across all cores.
    """
    core_times = []
    for c in range(n_cores):
        ins = roi_data['instructions'].get(c)
        t = roi_data['elapsed_time'].get(c)
        if ins is None or t is None or ins <= 0 or t <= 0:
            return None, None
        throughput = ins / t  # instructions per femtosecond
        norm_time = target_ins_per_core / throughput  # femtoseconds
        core_times.append(norm_time)

    # Multicore completion = slowest core
    return max(core_times), core_times


def extract_mean_freq(log_path):
    """Extract mean DVFS frequency from sniper.log."""
    freq_counts = {}
    total = 0
    with open(log_path) as f:
        for line in f:
            if 'DVFS Change' in line:
                total += 1
                m = re.search(r'f_lookup=(\d+\.\d+)GHz', line)
                if m:
                    fv = float(m.group(1))
                    freq_counts[fv] = freq_counts.get(fv, 0) + 1
    if not freq_counts:
        return 0, 0
    mean_f = sum(f * n for f, n in freq_counts.items()) / sum(freq_counts.values())
    return mean_f, total


def get_n_cores(run_dir):
    """Infer core count from the nN directory name."""
    for part in run_dir.split('/'):
        m = re.match(r'n(\d+)', part)
        if m:
            return int(m.group(1))
    return 1


def main():
    runs_base = sys.argv[1] if len(sys.argv) > 1 else \
        "results_test/plm_sweep/finestep/runs"
    target_total = 1_000_000_000  # 1B total instructions

    # Walk all run directories
    all_runs = {}
    for root, dirs, files in os.walk(runs_base):
        if 'sim.stats.sqlite3' not in files:
            continue

        # Parse path: runs/WORKLOAD/nN/l3_XXXMB/VARIANT/
        rel = os.path.relpath(root, runs_base)
        parts = rel.split('/')
        if len(parts) < 4:
            continue

        wl, nc_str, sz, variant = parts[0], parts[1], parts[2], parts[3]
        is_sram = variant.startswith('sram_')
        n_cores = int(nc_str.replace('n', ''))

        if n_cores == 1:
            continue  # n=1 doesn't need this fix

        db = os.path.join(root, 'sim.stats.sqlite3')
        log = os.path.join(root, 'sniper.log')

        try:
            roi = extract_per_core_roi(db, n_cores)
        except Exception as e:
            continue

        target_per_core = target_total // n_cores
        norm_time, core_times = compute_normalized_time(roi, n_cores, target_per_core)
        if norm_time is None:
            continue

        mean_f, n_changes = extract_mean_freq(log) if os.path.exists(log) else (0, 0)

        # Also get raw global.time from sim.out
        so = os.path.join(root, 'sim.out')
        raw_time = 0
        if os.path.exists(so):
            with open(so) as f:
                for line in f:
                    if line.startswith('global.time ='):
                        raw_time = int(line.split('=')[1].strip().split(',')[0]) * 1e-15

        key = (wl, nc_str, sz)
        entry = {
            'sram': is_sram,
            'norm_time': norm_time * 1e-15,  # fs → seconds
            'raw_time': raw_time,
            'mean_f': mean_f,
            'n_changes': n_changes,
            'core_times': [t * 1e-15 for t in core_times],
            'roi_ins': [roi['instructions'].get(c, 0) for c in range(n_cores)],
        }
        all_runs.setdefault(key, {})[('sram' if is_sram else 'mram')] = entry

    # Print results
    complete = {k: v for k, v in all_runs.items() if 'mram' in v and 'sram' in v}

    if not complete:
        print("No complete pairs found yet.")
        return

    print(f"{'Workload':22s} {'NC':>3s} {'Sz':>8s} | {'f_M':>5s} {'f_S':>5s} {'Δf':>6s} |"
          f" {'raw_M':>7s} {'raw_S':>7s} {'raw_sp':>7s} |"
          f" {'norm_M':>7s} {'norm_S':>7s} {'n_spdup':>7s}")
    print("-" * 115)

    wins_raw = wins_norm = total = 0
    for key in sorted(complete, key=lambda x: (x[1], x[2], x[0])):
        m, s = complete[key]['mram'], complete[key]['sram']
        wl_short = key[0][:22]

        df = m['mean_f'] - s['mean_f']
        raw_sp = s['raw_time'] / m['raw_time'] if m['raw_time'] > 0 else 0
        norm_sp = s['norm_time'] / m['norm_time'] if m['norm_time'] > 0 else 0

        total += 1
        if raw_sp >= 1.0: wins_raw += 1
        if norm_sp >= 1.0: wins_norm += 1

        rok = "✅" if raw_sp >= 1.0 else "❌"
        nok = "✅" if norm_sp >= 1.0 else "❌"

        print(f"{wl_short:22s} {key[1]:>3s} {key[2]:>8s} | "
              f"{m['mean_f']:5.3f} {s['mean_f']:5.3f} {df:+6.3f} | "
              f"{m['raw_time']:7.4f} {s['raw_time']:7.4f} {raw_sp:6.4f}{rok} | "
              f"{m['norm_time']:7.4f} {s['norm_time']:7.4f} {norm_sp:6.4f}{nok}")

    print(f"\n  Complete pairs: {total}")
    print(f"  Raw speedup wins:        {wins_raw}/{total}")
    print(f"  Normalized speedup wins:  {wins_norm}/{total}")


if __name__ == "__main__":
    main()
