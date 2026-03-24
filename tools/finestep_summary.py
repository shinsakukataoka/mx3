#!/usr/bin/env python3
"""Produce a summary table of finestep MRAM-DVFS vs SRAM-counterfactual results.

For n=1: uses raw execution time (deterministic, no barrier issue).
For n=4/n=8: uses normalized per-core throughput (Independent ROI Slice).

Usage:
    python3 mx2/tools/finestep_summary.py [runs_dir]
    python3 mx2/tools/finestep_summary.py results_test/plm_sweep/finestep/runs
"""
import os, sys, re, sqlite3
from collections import defaultdict


# ── helpers ──────────────────────────────────────────────────────────
def get_time_from_simout(so_path):
    """Extract simulation time from sim.out (handles both formats)."""
    with open(so_path) as f:
        for line in f:
            # Multicore: "global.time = 123456789"
            if line.startswith('global.time ='):
                return int(line.split('=')[1].strip().split(',')[0]) * 1e-15
            # n=1 table: "  Time (ns)  |  191029632 |  ..."
            if 'Time (ns)' in line and 'Idle' not in line:
                parts = line.split('|')
                if len(parts) >= 2:
                    try:
                        return int(parts[1].strip()) * 1e-9
                    except ValueError:
                        pass
    return 0


def get_mean_freq(log_path):
    fc = {}
    with open(log_path) as f:
        for line in f:
            if 'DVFS Change' in line:
                m = re.search(r'f_lookup=(\d+\.\d+)GHz', line)
                if m:
                    fv = float(m.group(1))
                    fc[fv] = fc.get(fv, 0) + 1
    if not fc:
        return 0, 0
    return sum(f * n for f, n in fc.items()) / sum(fc.values()), sum(fc.values())


def get_normalized_time(db_path, n_cores, target_total=1_000_000_000):
    """Compute normalized execution time using independent ROI slice."""
    target = target_total // n_cores
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    roi = {}
    for nid, name in [(19, 'ins'), (554, 'time')]:
        cur.execute(f"""
            SELECT v.core,
                   MAX(CASE WHEN p.prefixname='roi-end' THEN v.value END) -
                   MAX(CASE WHEN p.prefixname='roi-begin' THEN v.value END)
            FROM "values" v JOIN prefixes p ON v.prefixid = p.prefixid
            WHERE v.nameid = {nid} AND v.core < {n_cores}
            GROUP BY v.core ORDER BY v.core
        """)
        roi[name] = {r[0]: r[1] for r in cur.fetchall()}
    conn.close()

    core_times = []
    for c in range(n_cores):
        ins = roi['ins'].get(c)
        t = roi['time'].get(c)
        if ins is None or t is None or ins <= 0 or t <= 0:
            return None
        core_times.append(target / (ins / t) * 1e-15)
    return max(core_times)


def get_p_est(log_path):
    """Average P_est from DVFS Change lines."""
    vals = []
    with open(log_path) as f:
        for line in f:
            if 'DVFS Change' in line:
                m = re.search(r'P_est=(\d+\.\d+)W', line)
                if m:
                    vals.append(float(m.group(1)))
    return sum(vals) / len(vals) if vals else 0


def get_cap_and_leak(ci_path):
    with open(ci_path) as f:
        cmd = f.read()
    cap = leak = 0
    m = re.search(r'power_cap_w=([0-9.]+)', cmd)
    if m: cap = float(m.group(1))
    m = re.search(r'llc_leak_w=([0-9.]+)', cmd)
    if m: leak = float(m.group(1))
    return cap, leak


# ── main ─────────────────────────────────────────────────────────────
def main():
    runs_base = sys.argv[1] if len(sys.argv) > 1 else \
        "results_test/plm_sweep/finestep/runs"

    if not os.path.isdir(runs_base):
        print(f"[ERR] Not found: {runs_base}")
        sys.exit(1)

    # Scan all runs
    data = {}
    for root, dirs, files in os.walk(runs_base):
        if 'sim.out' not in files:
            continue
        rel = os.path.relpath(root, runs_base).split('/')
        if len(rel) < 4:
            continue
        wl, nc_str, sz, var = rel[0], rel[1], rel[2], rel[3]
        n_cores = int(nc_str.replace('n', ''))
        is_sram = var.startswith('sram_')

        so = os.path.join(root, 'sim.out')
        log = os.path.join(root, 'sniper.log')
        ci = os.path.join(root, 'cmd.info')
        db = os.path.join(root, 'sim.stats.sqlite3')

        raw_t = get_time_from_simout(so)
        mf, nchg = get_mean_freq(log) if os.path.exists(log) else (0, 0)

        # Normalized time for multicore
        norm_t = raw_t
        if n_cores > 1 and os.path.exists(db):
            try:
                nt = get_normalized_time(db, n_cores)
                if nt is not None:
                    norm_t = nt
            except Exception:
                pass

        cap, leak = get_cap_and_leak(ci) if os.path.exists(ci) else (0, 0)
        p_est = get_p_est(log) if os.path.exists(log) else 0

        key = (wl, nc_str, sz)
        tag = 'sram' if is_sram else 'mram'
        data.setdefault(key, {})[tag] = {
            'raw': raw_t, 'norm': norm_t, 'mf': mf, 'chg': nchg,
            'cap': cap, 'leak': leak, 'p_est': p_est,
        }

    pairs = {k: v for k, v in data.items() if 'mram' in v and 'sram' in v}
    total_runs = sum(len(v) for v in data.values())
    print(f"Total completed runs: {total_runs}/120  |  Complete pairs: {len(pairs)}/60\n")

    # ── per core-count section ───────────────────────────────────────
    grand_wins = grand_total = 0
    for nc_label in ['n1', 'n4', 'n8']:
        subset = {k: v for k, v in pairs.items() if k[1] == nc_label}
        if not subset:
            continue
        is_mc = nc_label != 'n1'
        metric = 'norm' if is_mc else 'raw'
        metric_name = "normalized" if is_mc else "raw"

        n_cores = int(nc_label.replace('n', ''))
        hdr = f"  {nc_label.upper()} — {len(subset)} pairs  [{metric_name} time]"
        print("=" * 90)
        print(hdr)
        print("=" * 90)
        print(f"{'Workload':30s} {'Sz':>6s} | {'f_M':>6s} {'f_S':>6s}"
              f" {'Δf':>7s} | {'t_M(s)':>8s} {'t_S(s)':>8s} {'spdup':>7s}")
        print("-" * 90)

        cap_stats = defaultdict(list)
        for cap_label in ['l3_16MB', 'l3_32MB', 'l3_128MB']:
            cap_sub = {k: v for k, v in subset.items() if k[2] == cap_label}
            if not cap_sub:
                continue
            for key in sorted(cap_sub, key=lambda x: x[0]):
                m, s = cap_sub[key]['mram'], cap_sub[key]['sram']
                t_m = m[metric]
                t_s = s[metric]
                df = m['mf'] - s['mf']
                sp = t_s / t_m if t_m > 0 else 0
                ok = "✅" if sp >= 1.0 else "❌"
                wl_short = key[0][:30]
                cap_stats[cap_label].append(sp)
                print(f"{wl_short:30s} {cap_label.replace('l3_',''):>6s}"
                      f" | {m['mf']:6.3f} {s['mf']:6.3f} {df:+7.4f}"
                      f" | {t_m:8.4f} {t_s:8.4f} {sp:6.4f}{ok}")

            sps = cap_stats[cap_label]
            wins = sum(1 for x in sps if x >= 1.0)
            mean_sp = sum(sps) / len(sps) if sps else 0
            grand_wins += wins
            grand_total += len(sps)
            print(f"  >> {cap_label}: {wins}/{len(sps)} wins  "
                  f"mean={mean_sp:.4f} ({(mean_sp-1)*100:+.2f}%)")
            print()

        # Overall for this core count
        all_sp = []
        for v in subset.values():
            t_m = v['mram'][metric]
            t_s = v['sram'][metric]
            if t_m > 0:
                all_sp.append(t_s / t_m)
        if all_sp:
            w = sum(1 for x in all_sp if x >= 1.0)
            mn = sum(all_sp) / len(all_sp)
            print(f"  >> {nc_label} OVERALL: {w}/{len(all_sp)} wins  "
                  f"mean={mn:.4f} ({(mn-1)*100:+.2f}%)")
        print()

    print(f"{'='*90}")
    print(f"  GRAND TOTAL: {grand_wins}/{grand_total} wins")
    print(f"{'='*90}")


if __name__ == "__main__":
    main()
