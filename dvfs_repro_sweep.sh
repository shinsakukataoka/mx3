#!/usr/bin/env bash
# dvfs_repro_sweep.sh — DVFS reproduction study.
#
# Stage 1:  Main DVFS        — 3 caps × 20 wl × 2 variants      = 120 jobs
# Stage 1b: Counterfactual   — 3 caps × 20 wl (sram-DVFS)        =  60 jobs
# Stage 2:  Read latency     — 4 mults × (10 n1 + 5 n4 + 5 n8)  =  80 jobs  [mc first]
# Stage 3:  Leakage gap      — 3 fracs × (10 n1 + 5 n4 + 5 n8)  =  60 jobs  [mc first]
# Stage 4:  Cap ± MAE        — 2 dirs  × 10 wl × 128MB           =  20 jobs
# Stage 5:  SmartDVFS        — multicore sticky-util ranking
# Stage 6:  SmartDVFS+TTL    — multicore sticky + bounded TTL
# Stage 7:  Fixed DVFS       — 3 caps × 20 wl × 2 variants      = 120 jobs
#
# Usage:
#   bash mx3/dvfs_repro_sweep.sh          # plan all
#   mx3/bin/mx submit repro/dvfs/<stage>  # submit
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
MX="$REPO_ROOT/mx3/bin/mx"
SITE_YAML="$REPO_ROOT/mx3/config/site.yaml"
PARAMS_YAML="$REPO_ROOT/mx3/config/params.yaml"
OUT_BASE="$REPO_ROOT/repro"
DEV_DIR="$REPO_ROOT/mx3/config/devices/sunnycove"

# Model dir (from fit_all_plm.sh output)
PLM_MODELS="$REPO_ROOT/repro/calibration/models"

# ---------------------------------------------------------------------------
# Sunnycove defaults
# ---------------------------------------------------------------------------
UARCH=sunnycove
BASE_FREQ_GHZ=2.2
LC_FMIN_GHZ=2.2
LC_STEP_GHZ=0.10
ROI_M=1000
WARMUP_M=200
DIR_ENTRIES=4194304
BASE_PERIODIC_INS=2000000
FAIL_ON_SIFT_ASSERT=1

# ---------------------------------------------------------------------------
# Read paths from site.yaml
# ---------------------------------------------------------------------------
read_yaml_key() { grep -E "^${1}:" "$2" | head -1 | sed 's/^[^:]*:[[:space:]]*//'; }
SNIPER_HOME="$(read_yaml_key SNIPER_HOME "$SITE_YAML")"
TRACE_ROOT="$(read_yaml_key TRACE_ROOT "$SITE_YAML")"
CONDA_LIB="$(read_yaml_key CONDA_LIB "$SITE_YAML")"
CONDA_PY="$(read_yaml_key CONDA_PY "$SITE_YAML")"
GCC_DIR="$(read_yaml_key GCC_DIR "$SITE_YAML")"
SPEC_ROOT="$(read_yaml_key SPEC_ROOT "$SITE_YAML")"

# ---------------------------------------------------------------------------
# Power params (for LC label)
# ---------------------------------------------------------------------------
_p_static=$(python3 -c "
import yaml
p = yaml.safe_load(open('$PARAMS_YAML'))
print(f\"{p['uarch']['sunnycove']['power']['p_static_w']:.2f}\")
")
_k_dyn=$(python3 -c "
import yaml
p = yaml.safe_load(open('$PARAMS_YAML'))
print(f\"{p['uarch']['sunnycove']['power']['k_dyn_w_per_ghz_util']:.2f}\")
")
_stat_lbl="${_p_static//./$'p'}"
_dyn_lbl="${_k_dyn//./$'p'}"
_step_lbl="${LC_STEP_GHZ//./$'p'}"

# ---------------------------------------------------------------------------
# Helpers
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

make_lc_base() {
  local _cap="$1"
  local _cap_lbl="${_cap//./$'p'}"
  echo "lc_c${_cap_lbl}_s${_stat_lbl}_d${_dyn_lbl}_tf1_h0p10_f4_st${_step_lbl}_pi${BASE_PERIODIC_INS}"
}

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

# SRAM LLC leakage per capacity (from sram14.yaml, in Watts)
get_sram_leak_w() {
  python3 -c "
import yaml
d = yaml.safe_load(open('$DEV_DIR/sram14.yaml'))
print(f\"{d[${1}]['leak_mw'] / 1000.0:.6f}\")
"
}

# ---------------------------------------------------------------------------
# Write env.sh for a run directory
# ---------------------------------------------------------------------------
write_env() {
  local _dir="$1"
  cat > "$_dir/env.sh" <<ENVSH
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
}

# ---------------------------------------------------------------------------
# Emit a job line
# ---------------------------------------------------------------------------
emit_job() {
  local VARIANT="$1" TECH="$2" WORKLOAD="$3" CORES="$4" L3_MB="$5"
  local CAMP; if [[ "$CORES" -eq 1 ]]; then CAMP="spec"; else CAMP="traces"; fi
  local _sweep_sfx=""
  [[ -n "${_MRAM_RD_MULT:-}"       ]] && _sweep_sfx+="_rdx${_MRAM_RD_MULT}"
  [[ -n "${_MRAM_LEAK_GAP_FRAC:-}" ]] && _sweep_sfx+="_lk${_MRAM_LEAK_GAP_FRAC}"
  [[ -n "${_FREQ_TAG:-}"            ]] && _sweep_sfx+="_${_FREQ_TAG}"
  local OUTDIR="${RUNS_ROOT}/${WORKLOAD}/n${CORES}/l3_${L3_MB}MB/${VARIANT}_${TECH}${_sweep_sfx}"


  local _line="CAMPAIGN=${CAMP}"
  _line+=" OUTDIR=${OUTDIR} JOB_OUTDIR=${OUTDIR}"
  _line+=" SNIPER_CONFIG=${UARCH} TECH=${TECH}"
  _line+=" WORKLOAD=${WORKLOAD} L3_MB=${L3_MB} VARIANT=${VARIANT}"
  _line+=" ROI_M=${ROI_M} WARMUP_M=${WARMUP_M}"
  _line+=" SIM_N=${CORES} BASE_FREQ_GHZ=${BASE_FREQ_GHZ}"
  _line+=" BASE_PERIODIC_INS=${BASE_PERIODIC_INS}"
  _line+=" LC_FMIN_GHZ=${LC_FMIN_GHZ} DIR_ENTRIES=${DIR_ENTRIES}"
  _line+=" FAIL_ON_SIFT_ASSERT=${FAIL_ON_SIFT_ASSERT}"
  _line+=" DEVICES_DIR=${DEV_DIR}"

  # PLM model selection
  local _plm_cfg="${PLM_MODELS}/plm_${UARCH}_n${CORES}_cal_${L3_MB}M"

  # Default: selective DVFS for multicore, unless explicitly disabled
  local _use_selective="${USE_SELECTIVE_DVFS:-1}"
  if [[ "$CORES" -gt 1 && "$_use_selective" == "1" ]]; then
    _plm_cfg+="_selk1"
    _line+=" LC_SELECTIVE=1 LC_TOPK=1"
  fi

  _plm_cfg+=".sh"
  _line+=" PLM_CFG_SH=${_plm_cfg}"

  # SPEC expects BENCH env var
  [[ "$CAMP" == "spec" ]] && _line+=" BENCH=${WORKLOAD}"

  # Optional overrides
  [[ -n "${_MRAM_RD_MULT:-}" ]]       && _line+=" MRAM_RD_MULT=${_MRAM_RD_MULT}"
  [[ -n "${_MRAM_LEAK_GAP_FRAC:-}" ]] && _line+=" MRAM_LEAK_GAP_FRAC=${_MRAM_LEAK_GAP_FRAC}"
  [[ -n "${_LLC_LEAK_OVERRIDE:-}" ]]   && _line+=" LLC_LEAK_OVERRIDE=${_LLC_LEAK_OVERRIDE}"
  [[ -n "${_LC_RANK_MODE:-}" ]]         && _line+=" LC_RANK_MODE=${_LC_RANK_MODE}"
  [[ -n "${_LC_STICKY_MARGIN:-}" ]]      && _line+=" LC_STICKY_MARGIN=${_LC_STICKY_MARGIN}"
  [[ -n "${_LC_STICKY_TTL:-}" ]]         && _line+=" LC_STICKY_TTL=${_LC_STICKY_TTL}"

  echo "$_line" >> "$JOBS_FILE"
  (( JOB_COUNT++ )) || true
}

# ---------------------------------------------------------------------------
# Setup a stage
# ---------------------------------------------------------------------------
setup_stage() {
  local stage="$1"
  RUN_DIR="$OUT_BASE/dvfs/$stage"
  RUNS_ROOT="$RUN_DIR/runs"
  JOBS_FILE="$RUN_DIR/jobs.txt"
  mkdir -p "$RUNS_ROOT" "$RUN_DIR/slurm"
  write_env "$RUN_DIR"
  : > "$JOBS_FILE"
  JOB_COUNT=0
}

TOTAL_JOBS=0

# ===============================================================
# STAGE 1: Main DVFS (120 jobs)
#   3 caps × 20 wl × 2 variants (MRAM+LeakDVFS, SRAM baseline)
#   n=4/n=8 use selective DVFS (k=1)
# ===============================================================
echo ""
echo "=============================="
echo " Stage 1: Main DVFS"
echo "=============================="

setup_stage "1_main_dvfs"

for CORES in 1 4 8; do
  for L3_MB in 16 32 128; do
    mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
    for WORKLOAD in "${WLOADS[@]}"; do
      _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
      LC_BASE=$(make_lc_base "$_cap")

      # Variant 1: MRAM + LeakDVFS
      emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"

      # Variant 2: SRAM baseline (fixed frequency, no DVFS)
      emit_job "baseline_sram_only" "sram14" "$WORKLOAD" "$CORES" "$L3_MB"
    done
  done
done

TOTAL_JOBS=$(( TOTAL_JOBS + JOB_COUNT ))
echo "  -> $JOB_COUNT jobs"

# ===============================================================
# STAGE 1b: Counterfactual DVFS — sram-DVFS (60 jobs)
#   MRAM cache but governor sees SRAM leakage → no boost headroom.
#   3 caps × 20 wl × 1 variant
#   n=4/n=8 use selective DVFS (k=1)
# ===============================================================
echo ""
echo "=============================="
echo " Stage 1b: Counterfactual (sram-DVFS)"
echo "=============================="

setup_stage "1b_counterfactual"

for CORES in 1 4 8; do
  for L3_MB in 16 32 128; do
    _LLC_LEAK_OVERRIDE=$(get_sram_leak_w "$L3_MB")
    mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
    for WORKLOAD in "${WLOADS[@]}"; do
      _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
      LC_BASE=$(make_lc_base "$_cap")
      emit_job "sram_${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
    done
    unset _LLC_LEAK_OVERRIDE
  done
done

TOTAL_JOBS=$(( TOTAL_JOBS + JOB_COUNT ))
echo "  -> $JOB_COUNT jobs"

# ===============================================================
# STAGE 2: MRAM Read-Latency Sweep (80 jobs)
#   128MB × 4 multipliers × (5 n4 mixes + 5 n8 mixes [MC FIRST] + 10 n1 workloads)
# ===============================================================
echo ""
echo "=============================="
echo " Stage 2: Read-Latency Sweep"
echo "=============================="

setup_stage "2_read_latency"

L3_MB=128

# --- Multicore first (n=4 then n=8) ---
for CORES in 4 8; do
  mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
  for MULT in 2 3 4 5; do
    _MRAM_RD_MULT="$MULT"
    for WORKLOAD in "${WLOADS[@]}"; do
      _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
      LC_BASE=$(make_lc_base "$_cap")
      emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
    done
    unset _MRAM_RD_MULT
  done
done

# --- Single-core (n=1) after ---
CORES=1
mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
for MULT in 2 3 4 5; do
  _MRAM_RD_MULT="$MULT"
  for WORKLOAD in "${WLOADS[@]}"; do
    _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
    LC_BASE=$(make_lc_base "$_cap")
    emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
  done
  unset _MRAM_RD_MULT
done

TOTAL_JOBS=$(( TOTAL_JOBS + JOB_COUNT ))
echo "  -> $JOB_COUNT jobs"

# ===============================================================
# STAGE 3: Leakage Gap Sensitivity (60 jobs)
#   128MB × 3 gap fracs × (5 n4 mixes + 5 n8 mixes [MC FIRST] + 10 n1 workloads)
# ===============================================================
echo ""
echo "=============================="
echo " Stage 3: Leakage Gap"
echo "=============================="

setup_stage "3_leakage_gap"

L3_MB=128

# --- Multicore first (n=4 then n=8) ---
for CORES in 4 8; do
  mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
  for FRAC in 0.25 0.50 0.75; do
    _MRAM_LEAK_GAP_FRAC="$FRAC"
    for WORKLOAD in "${WLOADS[@]}"; do
      _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
      LC_BASE=$(make_lc_base "$_cap")
      emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
    done
    unset _MRAM_LEAK_GAP_FRAC
  done
done

# --- Single-core (n=1) after ---
CORES=1
mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
for FRAC in 0.25 0.50 0.75; do
  _MRAM_LEAK_GAP_FRAC="$FRAC"
  for WORKLOAD in "${WLOADS[@]}"; do
    _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
    LC_BASE=$(make_lc_base "$_cap")
    emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
  done
  unset _MRAM_LEAK_GAP_FRAC
done

TOTAL_JOBS=$(( TOTAL_JOBS + JOB_COUNT ))
echo "  -> $JOB_COUNT jobs"

# ===============================================================
# STAGE 4: Cap ± MAE Sensitivity (60 jobs)
#   128MB × 2 dirs × 10 n=1 workloads              = 20 jobs
#    32MB × 2 dirs × (10 n=1 + 5 n=4 + 5 n=8)     = 40 jobs
#   MAE values:
#     n1_128M = 0.663 W
#     n1_32M  = 0.640 W,  n4_32M = 0.480 W,  n8_32M = 1.015 W
# ===============================================================
echo ""
echo "=============================="
echo " Stage 4: Cap ± MAE"
echo "=============================="

setup_stage "4_cap_mae"

# --- 128MB, n=1 only (original 20 jobs) ---
L3_MB=128
CORES=1
MAE=0.663
mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")

for WORKLOAD in "${WLOADS[@]}"; do
  _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")

  # Cap - MAE
  _cap_minus=$(python3 -c "print(f'{max(${_cap} - ${MAE}, 1.0):.2f}')")
  LC_MINUS=$(make_lc_base "$_cap_minus")
  emit_job "${LC_MINUS}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"

  # Cap + MAE
  _cap_plus=$(python3 -c "print(f'{${_cap} + ${MAE}:.2f}')")
  LC_PLUS=$(make_lc_base "$_cap_plus")
  emit_job "${LC_PLUS}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
done

# --- 32MB, all core counts (40 new jobs) ---
L3_MB=32
declare -A MAE_32=( [1]=0.640 [4]=0.480 [8]=1.015 )

for CORES in 1 4 8; do
  MAE="${MAE_32[$CORES]}"
  mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
  for WORKLOAD in "${WLOADS[@]}"; do
    _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")

    # Cap - MAE
    _cap_minus=$(python3 -c "print(f'{max(${_cap} - ${MAE}, 1.0):.2f}')")
    LC_MINUS=$(make_lc_base "$_cap_minus")
    emit_job "${LC_MINUS}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"

    # Cap + MAE
    _cap_plus=$(python3 -c "print(f'{${_cap} + ${MAE}:.2f}')")
    LC_PLUS=$(make_lc_base "$_cap_plus")
    emit_job "${LC_PLUS}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
  done
done
unset MAE_32

TOTAL_JOBS=$(( TOTAL_JOBS + JOB_COUNT ))
echo "  -> $JOB_COUNT jobs"

# ===============================================================
# STAGE 5: SmartDVFS (sticky util ranking) — multicore only
#   Re-runs Stage 1 multicore jobs (n=4/8) with rank_mode=util_sticky.
#   Sticky hysteresis prevents flip-flopping between cores with
#   similar utilization.  margin=0.05 (5% util gap to switch).
#   Also includes corresponding counterfactual variants.
#   3 caps × (5 n4 + 5 n8) × 2 (MRAM+DVFS + CF) = ~60 jobs
# ===============================================================
echo ""
echo "=============================="
echo " Stage 5: SmartDVFS (util_sticky)"
echo "=============================="

setup_stage "5_smart_dvfs"

for CORES in 4 8; do
  for L3_MB in 16 32 128; do
    _LC_RANK_MODE="util_sticky"
    _LC_STICKY_MARGIN="0.05"
    mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
    for WORKLOAD in "${WLOADS[@]}"; do
      _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
      LC_BASE=$(make_lc_base "$_cap")

      # SmartDVFS: MRAM + LeakDVFS with sticky-util ranking
      emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"

      # SmartDVFS counterfactual: MRAM cache, SRAM leakage in governor, sticky ranking
      _LLC_LEAK_OVERRIDE=$(get_sram_leak_w "$L3_MB")
      emit_job "sram_${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
      unset _LLC_LEAK_OVERRIDE
    done
    unset _LC_RANK_MODE _LC_STICKY_MARGIN
  done
done

TOTAL_JOBS=$(( TOTAL_JOBS + JOB_COUNT ))
echo "  -> $JOB_COUNT jobs"

# ===============================================================
# STAGE 6: SmartDVFS + TTL (bounded sticky-util) — multicore only
#   Same as Stage 5 but with sticky_ttl=10: mandatory re-election
#   every 10 intervals to prevent indefinite lock-in.
#   3 caps × (5 n4 + 5 n8) × 2 (MRAM+DVFS + CF) = ~60 jobs
# ===============================================================
echo ""
echo "=============================="
echo " Stage 6: SmartDVFS (util_sticky + TTL=10)"
echo "=============================="

setup_stage "6_smart_dvfs_ttl"

for CORES in 4 8; do
  for L3_MB in 16 32 128; do
    _LC_RANK_MODE="util_sticky"
    _LC_STICKY_MARGIN="0.05"
    _LC_STICKY_TTL="10"
    mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
    for WORKLOAD in "${WLOADS[@]}"; do
      _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
      LC_BASE=$(make_lc_base "$_cap")

      # SmartDVFS+TTL: MRAM + LeakDVFS with TTL-bounded sticky ranking
      emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"

      # SmartDVFS+TTL counterfactual: MRAM cache, SRAM leakage, sticky+TTL ranking
      _LLC_LEAK_OVERRIDE=$(get_sram_leak_w "$L3_MB")
      emit_job "sram_${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
      unset _LLC_LEAK_OVERRIDE
    done
    unset _LC_RANK_MODE _LC_STICKY_MARGIN _LC_STICKY_TTL
  done
done

TOTAL_JOBS=$(( TOTAL_JOBS + JOB_COUNT ))
echo "  -> $JOB_COUNT jobs"

# ===============================================================
# STAGE 7: Fixed DVFS (120 jobs)
#   Re-run of stages 1 + 1b with recompiled Sniper binary.
#   3 caps × 20 wl × 2 variants (MRAM+DVFS, counterfactual)
#   n=4/n=8 use selective DVFS (k=1)
# ===============================================================
echo ""
echo "=============================="
echo " Stage 7: Fixed DVFS"
echo "=============================="

setup_stage "7_fixed_dvfs"

for CORES in 1 4 8; do
  for L3_MB in 16 32 128; do
    mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
    for WORKLOAD in "${WLOADS[@]}"; do
      _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
      LC_BASE=$(make_lc_base "$_cap")

      # Variant 1: MRAM + LeakDVFS
      emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"

      # Variant 2: Counterfactual — MRAM cache, governor sees SRAM leakage
      _LLC_LEAK_OVERRIDE=$(get_sram_leak_w "$L3_MB")
      emit_job "sram_${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
      unset _LLC_LEAK_OVERRIDE
    done
  done
done

TOTAL_JOBS=$(( TOTAL_JOBS + JOB_COUNT ))
echo "  -> $JOB_COUNT jobs"


# ===============================================================
# STAGE 8: Global DVFS (60 jobs)
#   Multicore only: same sweep style as Stage 7, but uses global DVFS
#   (no selective k=1 boosting; uses full n4/n8 PLM models)
#   3 caps × (5 n4 + 5 n8) × 2 variants = 60 jobs
# ===============================================================
echo ""
echo "=============================="
echo " Stage 8: Global DVFS"
echo "=============================="

setup_stage "8_global_dvfs"

USE_SELECTIVE_DVFS=0

for CORES in 4 8; do
  for L3_MB in 16 32 128; do
    mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
    for WORKLOAD in "${WLOADS[@]}"; do
      _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
      LC_BASE=$(make_lc_base "$_cap")

      # Variant 1: MRAM + global LeakDVFS
      emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"

      # Variant 2: Counterfactual — MRAM cache, governor sees SRAM leakage
      _LLC_LEAK_OVERRIDE=$(get_sram_leak_w "$L3_MB")
      emit_job "sram_${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
      unset _LLC_LEAK_OVERRIDE
    done
  done
done

unset USE_SELECTIVE_DVFS

TOTAL_JOBS=$(( TOTAL_JOBS + JOB_COUNT ))
echo "  -> $JOB_COUNT jobs"

# ===============================================================
# STAGE 9: Fixed Read-Latency Sweep (120 jobs)
#   n=1 only, L3=32/128/16MB, 4 mults, 10 workloads
#   (16MB appended last to preserve existing indices)
# ===============================================================
echo ""
echo "=============================="
echo " Stage 9: Fixed Read-Latency Sweep"
echo "=============================="

setup_stage "9_fixed_read_latency"

for L3_MB in 32 128; do
  CORES=1
  mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
  for MULT in 2 3 4 5; do
    _MRAM_RD_MULT="$MULT"
    for WORKLOAD in "${WLOADS[@]}"; do
      _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
      LC_BASE=$(make_lc_base "$_cap")
      emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
    done
    unset _MRAM_RD_MULT
  done
done

# --- 16MB, n=1 (appended to preserve existing indices) ---
L3_MB=16
CORES=1
mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
for MULT in 2 3 4 5; do
  _MRAM_RD_MULT="$MULT"
  for WORKLOAD in "${WLOADS[@]}"; do
    _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
    LC_BASE=$(make_lc_base "$_cap")
    emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
  done
  unset _MRAM_RD_MULT
done

TOTAL_JOBS=$(( TOTAL_JOBS + JOB_COUNT ))
echo "  -> $JOB_COUNT jobs"

# ===============================================================
# STAGE 10: Fixed Leakage Gap Sweep (90 jobs)
#   n=1 only, L3=32/128/16MB, 3 fracs, 10 workloads
#   (16MB appended last to preserve existing indices)
# ===============================================================
echo ""
echo "=============================="
echo " Stage 10: Fixed Leakage Gap"
echo "=============================="

setup_stage "10_fixed_leakage_gap"

for L3_MB in 32 128; do
  CORES=1
  mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
  for FRAC in 0.25 0.50 0.75; do
    _MRAM_LEAK_GAP_FRAC="$FRAC"
    for WORKLOAD in "${WLOADS[@]}"; do
      _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
      LC_BASE=$(make_lc_base "$_cap")
      emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
    done
    unset _MRAM_LEAK_GAP_FRAC
  done
done

# --- 16MB, n=1 (appended to preserve existing indices) ---
L3_MB=16
CORES=1
mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
for FRAC in 0.25 0.50 0.75; do
  _MRAM_LEAK_GAP_FRAC="$FRAC"
  for WORKLOAD in "${WLOADS[@]}"; do
    _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")
    LC_BASE=$(make_lc_base "$_cap")
    emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
  done
  unset _MRAM_LEAK_GAP_FRAC
done

TOTAL_JOBS=$(( TOTAL_JOBS + JOB_COUNT ))
echo "  -> $JOB_COUNT jobs"

# ===============================================================
# STAGE 11: Fixed Cap ± MAE Sweep (60 jobs)
#   n=1 only, L3=32/128/16MB, 2 dirs, 10 workloads
#   (16MB appended last to preserve existing indices)
# ===============================================================
echo ""
echo "=============================="
echo " Stage 11: Fixed Cap ± MAE"
echo "=============================="

setup_stage "11_fixed_cap_mae"

declare -A FIXED_MAE=( [32]=0.640 [128]=0.663 )

for L3_MB in 32 128; do
  CORES=1
  MAE="${FIXED_MAE[$L3_MB]}"
  mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")

  for WORKLOAD in "${WLOADS[@]}"; do
    _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")

    _cap_minus=$(python3 -c "print(f'{max(${_cap} - ${MAE}, 1.0):.2f}')")
    LC_MINUS=$(make_lc_base "$_cap_minus")
    emit_job "${LC_MINUS}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"

    _cap_plus=$(python3 -c "print(f'{${_cap} + ${MAE}:.2f}')")
    LC_PLUS=$(make_lc_base "$_cap_plus")
    emit_job "${LC_PLUS}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
  done
done

# --- 16MB, n=1 (MAE=0.636 from validation, appended to preserve existing indices) ---
L3_MB=16
CORES=1
MAE=0.636
mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")

for WORKLOAD in "${WLOADS[@]}"; do
  _cap=$(get_plm_cap "$WORKLOAD" "$CORES" "$L3_MB")

  _cap_minus=$(python3 -c "print(f'{max(${_cap} - ${MAE}, 1.0):.2f}')")
  LC_MINUS=$(make_lc_base "$_cap_minus")
  emit_job "${LC_MINUS}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"

  _cap_plus=$(python3 -c "print(f'{${_cap} + ${MAE}:.2f}')")
  LC_PLUS=$(make_lc_base "$_cap_plus")
  emit_job "${LC_PLUS}" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
done
unset FIXED_MAE

TOTAL_JOBS=$(( TOTAL_JOBS + JOB_COUNT ))
echo "  -> $JOB_COUNT jobs"


# ===============================================================
# STAGE 12: BASELINE_RUN (120 jobs)
#   3 caps × 20 wl × 2 baselines (SRAM7, MRAM14)
# ===============================================================
echo ""
echo "=============================="
echo " Stage 12: BASELINE_RUN"
echo "=============================="

setup_stage "12_baseline_run"

for CORES in 1 4 8; do
  for L3_MB in 16 32 128; do
    mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$L3_MB")
    for WORKLOAD in "${WLOADS[@]}"; do
      # Baseline 1: SRAM7 fixed-frequency baseline
      emit_job "baseline_sram_only" "sram7" "$WORKLOAD" "$CORES" "$L3_MB"

      # Baseline 2: MRAM14 fixed-frequency baseline
      emit_job "baseline_mram_only" "mram14" "$WORKLOAD" "$CORES" "$L3_MB"
    done
  done
done

TOTAL_JOBS=$(( TOTAL_JOBS + JOB_COUNT ))
echo "  -> $JOB_COUNT jobs"


# ===============================================================
# STAGE 13: Fixed Frequency Sweep (400 jobs)
#   20 wl (10 n=1 + 5 n=4 + 5 n=8) × 4 device/cap pairs × 5 freqs
#   Device pairs: SRAM7@16MB, MRAM14@{16,32,128}MB
#   Freqs: 2.2, 2.6, 3.0, 3.4, 3.8 GHz
#   All fixed-frequency, no DVFS governor.
# ===============================================================
echo ""
echo "=============================="
echo " Stage 13: Fixed Frequency Sweep"
echo "=============================="

setup_stage "13_fixed_freq_sweep"

_ORIG_BASE_FREQ="$BASE_FREQ_GHZ"

declare -a _FF_TECHS=("sram7" "mram14" "mram14" "mram14")
declare -a _FF_CAPS=(16 16 32 128)
declare -a _FF_VARIANTS=("baseline_sram_only" "baseline_mram_only" "baseline_mram_only" "baseline_mram_only")

for FREQ in 2.2 2.6 3.0 3.4 3.8; do
  BASE_FREQ_GHZ="$FREQ"
  _FREQ_TAG="f${FREQ//./$'p'}"

  for i in "${!_FF_TECHS[@]}"; do
    _tech="${_FF_TECHS[$i]}"
    _l3="${_FF_CAPS[$i]}"
    _var="${_FF_VARIANTS[$i]}"

    for CORES in 1 4 8; do
      mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" "$_l3")
      for WORKLOAD in "${WLOADS[@]}"; do
        emit_job "$_var" "$_tech" "$WORKLOAD" "$CORES" "$_l3"
      done
    done
  done
done

BASE_FREQ_GHZ="$_ORIG_BASE_FREQ"
unset _FREQ_TAG _FF_TECHS _FF_CAPS _FF_VARIANTS

TOTAL_JOBS=$(( TOTAL_JOBS + JOB_COUNT ))
echo "  -> $JOB_COUNT jobs"


# ===============================================================
# STAGE 14: Cross-Capacity Leakage Gap DVFS (20 jobs)
#   MRAM14@32MB LLC but power cap uses SRAM7@16MB leakage.
#   Headroom = sram7_leak(16MB) - mram14_leak(32MB)
#            = 125.03 mW - 51.20 mW = 73.83 mW
#   (vs normal 32MB headroom: 250.05 - 51.20 = 198.85 mW)
#   20 wl (10 n=1 + 5 n=4 + 5 n=8) × 1 variant = 20 jobs
# ===============================================================
echo ""
echo "=============================="
echo " Stage 14: Cross-Cap Leakage DVFS"
echo "=============================="

setup_stage "14_cross_cap_dvfs"

for CORES in 1 4 8; do
  # Workloads come from the 16MB calibrated set (same workloads appear at all sizes)
  mapfile -t WLOADS < <(get_calibrated_workloads "$CORES" 16)
  for WORKLOAD in "${WLOADS[@]}"; do
    # Cap from 16MB → encodes SRAM7@16MB leakage as headroom ceiling
    _cap=$(get_plm_cap "$WORKLOAD" "$CORES" 16)
    LC_BASE=$(make_lc_base "$_cap")

    # Run at L3=32MB with MRAM14, but governor uses the 16MB-derived cap
    emit_job "${LC_BASE}" "mram14" "$WORKLOAD" "$CORES" 32
  done
done

TOTAL_JOBS=$(( TOTAL_JOBS + JOB_COUNT ))
echo "  -> $JOB_COUNT jobs"


# ===============================================================
# Summary
# ===============================================================
echo ""
echo "=============================================="
echo " DVFS Reproduction Study — Complete"
echo "=============================================="
echo " Uarch:      ${UARCH} @ ${BASE_FREQ_GHZ} GHz"
echo " Models:     ${PLM_MODELS}"
echo " Devices:    ${DEV_DIR}"
echo " Total jobs: ${TOTAL_JOBS}"
echo " Output:     ${OUT_BASE}/dvfs/"
echo "=============================================="
echo ""
echo "Submit all stages:"
echo "  for d in ${OUT_BASE}/dvfs/*/; do"
echo "    ${MX} submit \"\$d\""
echo "  done"
