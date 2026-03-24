#!/usr/bin/env bash
# plm_sweep.sh — PLM-based simulation sweep.
#
# Three modes:
#   --mode main         MRAM + LeakDVFS across n=1/4/8, 20 calibrated workloads, 3 cache sizes
#   --mode comparison   Static lift (f*) vs baseline for n=1 workloads
#   --mode sensitivity  Read-latency and leakage-gap sensitivity for n=1
#
# Results written to: results_test/plm_sweep/
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
MX="$REPO_ROOT/mx2/bin/mx"
SITE_YAML="$REPO_ROOT/mx2/config/site.yaml"
PARAMS_YAML="$REPO_ROOT/mx2/config/params.yaml"
OUT_BASE="$HOME/COSC_498/miniMXE/results_test"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
MODE=""
# Per-core-count PLM base (CORES substituted at emit_job time)
PLM_CFG_BASE="${PLM_CFG_BASE:-$OUT_BASE/plm_calibrate/plm_sunnycove}"
# Legacy combined model (kept for reference)
PLM_CFG_SH="${PLM_CFG_SH:-$OUT_BASE/plm_calibrate/plm_sunnycove_n1n4n8_cal.sh}"

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --plm-cfg) PLM_CFG_SH="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 --mode main|comparison|sensitivity [--plm-cfg PATH]"
      exit 0 ;;
    *) echo "[ERR] Unknown arg: $1"; exit 1 ;;
  esac
done

[[ -z "$MODE" ]] && { echo "[ERR] --mode required (main|comparison|sensitivity)"; exit 1; }

# ---------------------------------------------------------------------------
# Sunnycove configuration
# ---------------------------------------------------------------------------
UARCH=sunnycove
BASE_FREQ_GHZ=2.2
LC_FMIN_GHZ=2.2
LC_STEP_GHZ=0.10
PLM_CAL_SUFFIX=""
ROI_M=1000
WARMUP_M=200
DIR_ENTRIES=4194304
BASE_PERIODIC_INS=2000000
FAIL_ON_SIFT_ASSERT=1

# ---------------------------------------------------------------------------
# Read paths from site.yaml
# ---------------------------------------------------------------------------
read_yaml_key() {
  grep -E "^${1}:" "$2" | head -1 | sed 's/^[^:]*:[[:space:]]*//'
}
SNIPER_HOME="$(read_yaml_key SNIPER_HOME "$SITE_YAML")"
TRACE_ROOT="$(read_yaml_key TRACE_ROOT "$SITE_YAML")"
CONDA_LIB="$(read_yaml_key CONDA_LIB "$SITE_YAML")"
CONDA_PY="$(read_yaml_key CONDA_PY "$SITE_YAML")"
GCC_DIR="$(read_yaml_key GCC_DIR "$SITE_YAML")"
SPEC_ROOT="$(read_yaml_key SPEC_ROOT "$SITE_YAML")"

# ---------------------------------------------------------------------------
# Static and dynamic power params (for LC label)
# ---------------------------------------------------------------------------
_p_static=$(python3 -c "
import yaml
p = yaml.safe_load(open('$PARAMS_YAML'))
print(f\"{p['uarch']['sunnycove']['power']['p_static_w']:.2f}\")
" 2>/dev/null)

_k_dyn=$(python3 -c "
import yaml
p = yaml.safe_load(open('$PARAMS_YAML'))
print(f\"{p['uarch']['sunnycove']['power']['k_dyn_w_per_ghz_util']:.2f}\")
" 2>/dev/null)

_stat_lbl="${_p_static//./$'p'}"
_dyn_lbl="${_k_dyn//./$'p'}"
_step_lbl="${LC_STEP_GHZ//./$'p'}"

# ---------------------------------------------------------------------------
# Helper: get PLM cap for a workload from params.yaml
# ---------------------------------------------------------------------------
get_plm_cap() {
  local _wl="$1" _cores="$2" _l3="$3"
  local _core_key="n${_cores}"
  python3 -c "
import yaml, sys
p = yaml.safe_load(open('$PARAMS_YAML'))
sc = p['uarch']['sunnycove']
try:
    cap = sc['plm_cap_w']['${_core_key}']['${_wl}'][${_l3}]
    if cap and float(cap) > 0:
        print(f'{cap:.2f}')
        sys.exit(0)
except (KeyError, TypeError):
    pass
try:
    cap = sc['cap_w']['multicore']['${_core_key}'][${_l3}]
    print(f'{cap:.2f}')
except (KeyError, TypeError):
    cap = sc['cap_w']['single'][${_l3}]
    print(f'{cap:.2f}')
" 2>/dev/null
}

# Build LC_BASE label for a given cap value
make_lc_base() {
  local _cap="$1"
  local _cap_lbl="${_cap//./$'p'}"
  echo "lc_c${_cap_lbl}_s${_stat_lbl}_d${_dyn_lbl}_tf1_h0p10_f4_st${_step_lbl}_pi${BASE_PERIODIC_INS}"
}

# ---------------------------------------------------------------------------
# Workload lists — pulled from mx's canonical lists
# ---------------------------------------------------------------------------
MX_BIN="$REPO_ROOT/mx2/bin/mx"

get_workloads() {
  local cores="$1"
  if [[ "$cores" -eq 1 ]]; then
    python3 -c "
import re
with open('${MX_BIN}') as f: src = f.read()
ns = {}; exec('from typing import List', ns)
m = re.search(r'(def default_spec_benches\(.*?\n(?:    .*\n)*)', src)
exec(m.group(1), ns)
for w in ns['default_spec_benches'](): print(w)
"
  else
    python3 -c "
import re
with open('${MX_BIN}') as f: src = f.read()
ns = {}; exec('from typing import List', ns)
m = re.search(r'(def default_trace_workloads\(.*?\n(?:    .*\n)*)', src)
exec(m.group(1), ns)
for w in ns['default_trace_workloads'](${cores}): print(w)
"
  fi
}

# Filter to only calibrated workloads (those with PLM caps in params.yaml)
get_calibrated_workloads() {
  local cores="$1" l3="$2"
  local _core_key="n${cores}"
  python3 -c "
import yaml
p = yaml.safe_load(open('$PARAMS_YAML'))
wls = p.get('uarch',{}).get('sunnycove',{}).get('plm_cap_w',{}).get('${_core_key}',{})
for wl_key in sorted(wls.keys()):
    if isinstance(wls[wl_key], dict) and ${l3} in wls[wl_key]:
        print(wl_key)
" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Compute f* for static lift comparison
# ---------------------------------------------------------------------------
compute_f_star() {
  python3 - "$PLM_CFG_SH" "$PARAMS_YAML" << 'PYEOF'
import csv, re, sys, yaml

cal_file = sys.argv[1]
params_file = sys.argv[2]

# Load PLM coefficients
with open(cal_file) as f: src = f.read()
def parse_arr(name):
    m = re.search(rf'{name}=\(\s*(.*?)\)', src, re.DOTALL)
    return [float(x) for x in m.group(1).split()]
plm_f  = parse_arr('PLM_F')
plm_b  = parse_arr('PLM_B')
plm_au = parse_arr('PLM_AUTIL')
plm_ai = parse_arr('PLM_AIPC')

# Load device leakage
with open("/home/skataoka26/COSC_498/miniMXE/mx2/config/devices/sram14.yaml") as f:
    sram = yaml.safe_load(f)
with open("/home/skataoka26/COSC_498/miniMXE/mx2/config/devices/mram14.yaml") as f:
    mram = yaml.safe_load(f)

# Load n=1 oracle data
oracle = "/home/skataoka26/COSC_498/miniMXE/results_test/plm_calibrate/plm_calib_sunnycove_n1_32M/runs/oracle_points.csv"
by_bench = {}
with open(oracle) as f:
    for r in csv.DictReader(f):
        bench = r['bench']
        f_ghz = float(r['f_ghz'])
        p_nc = float(r['P_total_W']) - float(r['P_llc_leak_W'])
        if bench not in by_bench: by_bench[bench] = {}
        by_bench[bench][f_ghz] = p_nc

# Compute f* per cache size; report the most conservative
for l3_mb in [16, 32, 128]:
    sram_leak = sram[l3_mb]['leak_mw'] / 1000.0
    mram_leak = mram[l3_mb]['leak_mw'] / 1000.0
    
    f_stars = []
    for bench in sorted(by_bench.keys()):
        pts = by_bench[bench]
        base_pnc = pts.get(2.2)
        if base_pnc is None: continue
        p_cap = base_pnc + sram_leak
        threshold = p_cap - mram_leak
        
        f_star_b = 2.0
        for f in sorted(pts.keys()):
            if pts[f] <= threshold + 0.01:
                f_star_b = f
        f_stars.append(f_star_b)
    
    fstar = min(f_stars) if f_stars else 2.2
    print(f"{l3_mb}:{fstar:.1f}")
PYEOF
}

# ---------------------------------------------------------------------------
# Set up run directory
# ---------------------------------------------------------------------------
RUN_DIR="$OUT_BASE/plm_sweep/${MODE}"
RUNS_ROOT="$RUN_DIR/runs"
mkdir -p "$RUNS_ROOT" "$RUN_DIR/slurm"

# Write env.sh
cat > "$RUN_DIR/env.sh" <<ENVSH
#!/usr/bin/env bash
set -euo pipefail
export SNIPER_HOME='${SNIPER_HOME}'
export TRACE_ROOT='${TRACE_ROOT}'
export CONDA_LIB='${CONDA_LIB}'
export CONDA_PY='${CONDA_PY}'
export GCC_DIR='${GCC_DIR}'
export SPEC_ROOT='${SPEC_ROOT}'
export REPO_ROOT='${REPO_ROOT}'
ENVSH

# ---------------------------------------------------------------------------
# Generate jobs.txt
# ---------------------------------------------------------------------------
JOBS_FILE="$RUN_DIR/jobs.txt"
: > "$JOBS_FILE"
JOB_COUNT=0

emit_job() {
  local VARIANT="$1" TECH="$2" WORKLOAD="$3" CORES="$4" L3_MB="$5"
  local FREQ="${6:-${BASE_FREQ_GHZ}}"
  local CAMP="${7:-}"
  [[ -z "$CAMP" ]] && { if [[ "$CORES" -eq 1 ]]; then CAMP="spec"; else CAMP="traces"; fi; }
  local OUTDIR="${RUNS_ROOT}/${WORKLOAD}/n${CORES}/l3_${L3_MB}MB/${VARIANT}_${TECH}"
  local _line="CAMPAIGN=${CAMP}"
  _line+=" OUTDIR=${OUTDIR}"
  _line+=" JOB_OUTDIR=${OUTDIR}"
  _line+=" SNIPER_CONFIG=${UARCH}"
  _line+=" TECH=${TECH}"
  _line+=" WORKLOAD=${WORKLOAD}"
  _line+=" L3_MB=${L3_MB}"
  _line+=" VARIANT=${VARIANT}"
  _line+=" ROI_M=${ROI_M}"
  _line+=" WARMUP_M=${WARMUP_M}"
  _line+=" SIM_N=${CORES}"
  _line+=" BASE_FREQ_GHZ=${FREQ}"
  _line+=" BASE_PERIODIC_INS=${BASE_PERIODIC_INS}"
  _line+=" LC_FMIN_GHZ=${LC_FMIN_GHZ}"
  _line+=" DIR_ENTRIES=${DIR_ENTRIES}"
  _line+=" FAIL_ON_SIFT_ASSERT=${FAIL_ON_SIFT_ASSERT}"
  # Resolve PLM calibration file per core count and cache size
  # n=1 → n1n4 combined model (stable, low bias)
  # n=4 → n4 per-core model
  # n=8 → n8 per-core model
  local _plm_cfg
  local _size_suffix=""; [[ "$L3_MB" != "32" ]] && _size_suffix="_${L3_MB}M"
  case "$CORES" in
    1) _plm_cfg="${PLM_CFG_BASE}_n1n4_cal${_size_suffix}${PLM_CAL_SUFFIX}.sh" ;;
    *) _plm_cfg="${PLM_CFG_BASE}_n${CORES}_cal${_size_suffix}${PLM_CAL_SUFFIX}.sh" ;;
  esac
  _line+=" PLM_CFG_SH=${_plm_cfg}"
  # Pass LLC leak override if set (used by counterfactual mode)
  [[ -n "${LLC_LEAK_OVERRIDE:-}" ]] && _line+=" LLC_LEAK_OVERRIDE=${LLC_LEAK_OVERRIDE}"
  # Pass selective DVFS flags if set
  [[ -n "${LC_SELECTIVE:-}" ]] && _line+=" LC_SELECTIVE=${LC_SELECTIVE}"
  [[ -n "${LC_TOPK:-}" ]] && _line+=" LC_TOPK=${LC_TOPK}"
  # run_spec.sh expects BENCH; run_traces.sh expects WORKLOAD
  if [[ "$CAMP" == "spec" ]]; then
    _line+=" BENCH=${WORKLOAD}"
  fi
  echo "$_line" >> "$JOBS_FILE"
  (( JOB_COUNT++ )) || true
}

# =====================================================================
case "$MODE" in
# =====================================================================
  main)
    # MRAM + LeakDVFS for 20 calibrated workloads × 3 cache sizes
    for CORES in 1 4 8; do
      for L3_MB in 16 32 128; do
        mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
        for WORKLOAD in "${WLOADS[@]}"; do
          _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
          LC_BASE=$(make_lc_base "$_cap")
          emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
        done
      done
    done
    ;;

# =====================================================================
  comparison)
    # Static lift comparison: 128MB only
    L3_MB=128

    # --- n=1: all 10 workloads at conservative f*=2.3 ---
    mapfile -t WLOADS < <(get_calibrated_workloads 1 "$L3_MB")
    for WORKLOAD in "${WLOADS[@]}"; do
      emit_job "static_lift_f2p2" "mram14" "$WORKLOAD" 1 "$L3_MB" "2.2"
      emit_job "static_lift_f2p3" "mram14" "$WORKLOAD" 1 "$L3_MB" "2.3"
    done

    # --- n=4: per-workload f* (only workloads with headroom) ---
    # gccx4: f*=3.3  mcf+perl+exc+foto: f*=3.7  mcfx2+gccx2: f*=3.4
    declare -A N4_FSTAR=(
      ["502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r"]="3.3"
      ["505.mcf_r+500.perlbench_r+648.exchange2_s+649.fotonik3d_s"]="3.7"
      ["505.mcf_r+505.mcf_r+502.gcc_r+502.gcc_r"]="3.4"
    )
    for WORKLOAD in "${!N4_FSTAR[@]}"; do
      _fs="${N4_FSTAR[$WORKLOAD]}"
      _fs_lbl="${_fs//./$'p'}"
      emit_job "static_lift_f2p2" "mram14" "$WORKLOAD" 4 "$L3_MB" "2.2"
      emit_job "static_lift_f${_fs_lbl}" "mram14" "$WORKLOAD" 4 "$L3_MB" "$_fs"
    done
    ;;

# =====================================================================
  sensitivity)
    # Read-latency and leakage-gap sensitivity for n=1, n=4, n=8
    SENS_DEVICES=(
      "mram14_read2x"
      "mram14_read3x"
      "mram14_read4x"
      "mram14_read5x"
      "mram14_leak_b0p25"
      "mram14_leak_b0p5"
      "mram14_leak_b0p75"
    )
    for CORES in 1 4 8; do
      for L3_MB in 16 32 128; do
        mapfile -t SENS_WORKLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
        for DEV in "${SENS_DEVICES[@]}"; do
          for WORKLOAD in "${SENS_WORKLOADS[@]}"; do
            _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
            LC_BASE=$(make_lc_base "$_cap")
            emit_job "${LC_BASE}" "$DEV" "$WORKLOAD" "$CORES" "$L3_MB"
          done
        done
      done
    done
    ;;

# =====================================================================
  counterfactual)
    # MRAM LLC simulation but governor uses SRAM leakage → no headroom.
    # Isolates whether the SRAM→MRAM leakage savings actually help DVFS.
    # All cache sizes × all core counts.
    # ORDERING: 128MB n=1 (10 jobs, idx 1-10), 128MB n=4 (5 jobs, idx 11-15),
    #           then 16/32MB n=1, 16/32MB n=4, all n=8 sizes.
    #           Submit new jobs with --array=16-60 to skip existing 128MB results.
    #
    # SRAM LLC leakage per cache size (from sram14.yaml):
    #   16MB: 0.170900 W   32MB: 0.330430 W   128MB: 0.899078 W
    get_sram_leak() {
      case "$1" in
        16)  echo "0.170900" ;;
        32)  echo "0.330430" ;;
        128) echo "0.899078" ;;
      esac
    }

    # --- Phase 1: 128MB n=1+n=4 (preserve existing indices 1-15) ---
    export LLC_LEAK_OVERRIDE=0.899078
    for CORES in 1 4; do
      mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" 128)
      for WORKLOAD in "${WLOADS[@]}"; do
        _cap=$(get_plm_cap "$WORKLOAD" "$CORES" 128)
        LC_BASE=$(make_lc_base "$_cap")
        emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" 128
      done
    done
    unset LLC_LEAK_OVERRIDE

    # --- Phase 2: 16/32MB n=1+n=4 (new jobs, idx 16+) ---
    for L3_MB in 16 32; do
      export LLC_LEAK_OVERRIDE=$(get_sram_leak "$L3_MB")
      for CORES in 1 4; do
        mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
        for WORKLOAD in "${WLOADS[@]}"; do
          _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
          LC_BASE=$(make_lc_base "$_cap")
          emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
        done
      done
      unset LLC_LEAK_OVERRIDE
    done

    # --- Phase 3: n=8 all cache sizes (new jobs) ---
    for L3_MB in 16 32 128; do
      export LLC_LEAK_OVERRIDE=$(get_sram_leak "$L3_MB")
      mapfile -t WLOADS < <(get_calibrated_workloads 8 "$L3_MB")
      for WORKLOAD in "${WLOADS[@]}"; do
        _cap=$(get_plm_cap "$WORKLOAD" 8 "$L3_MB")
        LC_BASE=$(make_lc_base "$_cap")
        emit_job "${LC_BASE}" "mram14" "$WORKLOAD" 8 "$L3_MB"
      done
      unset LLC_LEAK_OVERRIDE
    done
    ;;

# =====================================================================
  tuning)
    # DVFS parameter tuning sweep: h × I cross-product (Δf fixed at 0.10)
    # n=1 and n=4, 32MB
    L3_MB=32

    TUNE_HYS=( 0.05 0.10 0.20 0.30 0.40 )
    TUNE_PI=( 1000000 2000000 3000000 4000000 )

    for CORES in 1 4; do
      readarray -t TUNE_WORKLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
      for WORKLOAD in "${TUNE_WORKLOADS[@]}"; do
        _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
        local_cap_lbl="${_cap//./$'p'}"

        for _h in "${TUNE_HYS[@]}"; do
          for _pi in "${TUNE_PI[@]}"; do
            local_h_lbl="${_h//./$'p'}"
            VARIANT="lc_c${local_cap_lbl}_s${_stat_lbl}_d${_dyn_lbl}_tf1_h${local_h_lbl}_f4_st${_step_lbl}_pi${_pi}"
            emit_job "$VARIANT" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
          done
        done
      done
    done
    ;;

# =====================================================================
  cap_sensitivity)
    # Same workloads as main, but with P_cap ± MAE to bound model error impact.
    # Produces error bars on speedup.
    # MAE per (cores, cache) from PLM validation:
    #   n=1: n1+n4 model MAE on n=1 data ≈ 1.56W (all sizes, conservative)
    #   n=4/n=8: per-core model MAE from per_core_plm_validation.csv
    get_mae() {
      local _nc="$1" _l3="$2"
      case "${_nc}_${_l3}" in
        1_16)  echo "1.56" ;;
        1_32)  echo "1.56" ;;
        1_128) echo "1.56" ;;
        4_16)  echo "1.41" ;;
        4_32)  echo "0.61" ;;
        4_128) echo "0.48" ;;
        8_16)  echo "0.88" ;;
        8_32)  echo "1.01" ;;
        8_128) echo "0.93" ;;
        *)     echo "1.50" ;;
      esac
    }

    for CORES in 1 4 8; do
      for L3_MB in 16 32 128; do
        mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
        _mae=$(get_mae "$CORES" "$L3_MB")
        for WORKLOAD in "${WLOADS[@]}"; do
          _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
          for _dir in plus minus; do
            if [[ "$_dir" == "plus" ]]; then
              _adj=$(python3 -c "print(f'{${_cap} + ${_mae}:.2f}')")
            else
              _adj=$(python3 -c "print(f'{max(${_cap} - ${_mae}, 1.0):.2f}')")
            fi
            LC_BASE=$(make_lc_base "$_adj")
            emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
          done
        done
      done
    done
    ;;

# =====================================================================
  add_TDP)
    # Fixed platform-wide TDP cap study.
    # Cap = max(per-workload caps) per (n, cache_size) from params.yaml.
    # Three variants: our DVFS, sram_lc counterfactual, static lift at f*.
    # 32/128MB only × 20 calibrated workloads × 3 variants = 120 jobs.
    #
    # Fixed TDP caps (precomputed from params.yaml):
    get_fixed_tdp() {
      case "${1}_${2}" in
        1_32)   echo "45.57" ;;
        1_128)  echo "74.12" ;;
        4_32)   echo "83.94" ;;
        4_128)  echo "112.20" ;;
        8_32)   echo "162.79" ;;
        8_128)  echo "183.27" ;;
      esac
    }
    # f* under fixed TDP (conservative, min across calibrated workloads):
    get_fixed_fstar() {
      case "${1}_${2}" in
        1_32)   echo "2.2" ;;
        1_128)  echo "2.4" ;;
        4_32)   echo "3.5" ;;
        4_128)  echo "3.6" ;;
        8_32)   echo "3.8" ;;
        8_128)  echo "3.8" ;;
      esac
    }
    # SRAM LLC leakage per cache size (for sram_lc counterfactual):
    get_sram_leak_tdp() {
      case "$1" in
        32)  echo "0.330430" ;;
        128) echo "0.899078" ;;
      esac
    }

    for CORES in 1 4 8; do
      for L3_MB in 32 128; do
        _fixed_cap=$(get_fixed_tdp "$CORES" "$L3_MB")
        _fstar=$(get_fixed_fstar "$CORES" "$L3_MB")
        _fstar_lbl="${_fstar//./$'p'}"
        _sram_leak=$(get_sram_leak_tdp "$L3_MB")

        mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
        for WORKLOAD in "${WLOADS[@]}"; do
          # Variant 1: MRAM + LeakDVFS (our mechanism) under fixed TDP
          LC_BASE=$(make_lc_base "$_fixed_cap")
          emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"

          # Variant 2: sram_lc — MRAM LLC but governor sees SRAM leakage
          # Use "sram_" prefix to get a distinct OUTDIR from variant 1
          export LLC_LEAK_OVERRIDE="$_sram_leak"
          emit_job "sram_${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
          unset LLC_LEAK_OVERRIDE

          # Variant 3: static lift at f* (fixed frequency, no DVFS)
          emit_job "static_lift_f${_fstar_lbl}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB" "$_fstar"
        done
      done
    done
    ;;

# =====================================================================
  finestep)
    # MRAM DVFS vs SRAM-counterfactual DVFS with 25 MHz step granularity.
    # 20 workloads × 3 capacities × 2 variants = 120 jobs.
    # Uses interpolated PLM calibration files (_step025).

    # Override step size and PLM cal suffix to use _step025 variants
    LC_STEP_GHZ=0.025
    _step_lbl="${LC_STEP_GHZ//./$'p'}"
    PLM_CAL_SUFFIX="_step025"

    # SRAM LLC leakage per cache size (from sram14.yaml)
    get_sram_leak_fs() {
      case "$1" in
        16)  echo "0.170900" ;;
        32)  echo "0.330430" ;;
        128) echo "0.899078" ;;
      esac
    }

    for CORES in 1 4 8; do
      for L3_MB in 16 32 128; do
        mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
        _sram_leak=$(get_sram_leak_fs "$L3_MB")
        for WORKLOAD in "${WLOADS[@]}"; do
          _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
          LC_BASE=$(make_lc_base "$_cap")

          # Variant 1: MRAM DVFS (standard leakage)
          emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"

          # Variant 2: SRAM counterfactual (governor sees SRAM leakage)
          export LLC_LEAK_OVERRIDE="$_sram_leak"
          emit_job "sram_${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
          unset LLC_LEAK_OVERRIDE
        done
      done
    done
    ;;

# =====================================================================
  finestep_selective)
    # Per-core DVFS (selective k=1) with 25 MHz steps.
    # Uses analytically derived PLM coefficients: 1/N power scaling.
    # Only multicore (n=4, n=8) — n=1 is already per-core by definition.

    LC_STEP_GHZ=0.025
    _step_lbl="${LC_STEP_GHZ//./$'p'}"
    PLM_CAL_SUFFIX="_step025_selk1"
    export LC_SELECTIVE=1
    export LC_TOPK=1

    get_sram_leak_fss() {
      case "$1" in
        16)  echo "0.170900" ;;
        32)  echo "0.330430" ;;
        128) echo "0.899078" ;;
      esac
    }

    for CORES in 4 8; do
      for L3_MB in 16 32 128; do
        mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
        _sram_leak=$(get_sram_leak_fss "$L3_MB")
        for WORKLOAD in "${WLOADS[@]}"; do
          _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
          LC_BASE=$(make_lc_base "$_cap")

          # Variant 1: MRAM DVFS (selective k=1)
          emit_job "sel_${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"

          # Variant 2: SRAM counterfactual (selective k=1)
          export LLC_LEAK_OVERRIDE="$_sram_leak"
          emit_job "sram_sel_${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
          unset LLC_LEAK_OVERRIDE
        done
      done
    done
    unset LC_SELECTIVE LC_TOPK
    ;;

# =====================================================================
  *)
    echo "[ERR] Unknown mode: $MODE (supported: main, comparison, sensitivity, counterfactual, tuning, cap_sensitivity, add_TDP, finestep, finestep_selective)"
    exit 1 ;;
esac

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "=============================================="
echo " PLM Sweep — mode=${MODE}"
echo " Uarch:       ${UARCH}   Base freq: ${BASE_FREQ_GHZ}GHz"
echo " Power model: PLM (${PLM_CFG_SH##*/})"
echo " DVFS step:   ${LC_STEP_GHZ}GHz   f_min: ${LC_FMIN_GHZ}GHz"
echo " Total jobs:  ${JOB_COUNT}"
echo " Run dir:     ${RUN_DIR}"
echo "=============================================="
echo
echo "[OK] planned ${JOB_COUNT} jobs -> ${RUN_DIR}"
echo
echo "Next:"
echo "  $MX submit ${RUN_DIR} --sbatch=\"-w\" --sbatch=\"node017\""