#!/usr/bin/env bash
# dvfs_repro_sweep.sh — DVFS reproduction study (270 jobs).
#
# Stage 1:  Main DVFS        — 3 caps × 20 wl × 2 variants      = 120 jobs
# Stage 1b: Counterfactual   — 3 caps × 20 wl (sram-DVFS)        =  60 jobs
# Stage 2:  Read latency     — 4 mults × 10 wl × 128MB           =  40 jobs
# Stage 3:  Leakage gap      — 3 fracs × 10 wl × 128MB           =  30 jobs
# Stage 4:  Cap ± MAE        — 2 dirs  × 10 wl × 128MB           =  20 jobs
#                                                         Total   = 270 jobs
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
  local OUTDIR="${RUNS_ROOT}/${WORKLOAD}/n${CORES}/l3_${L3_MB}MB/${VARIANT}_${TECH}"

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

  # PLM model selection: n=1 → n1 model, n=4/8 → per-core model
  local _plm_cfg="${PLM_MODELS}/plm_${UARCH}_n${CORES}_cal_${L3_MB}M"
  # Selective DVFS for n>1: use _selk1 model
  if [[ "$CORES" -gt 1 ]]; then
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
# STAGE 2: MRAM Read-Latency Sweep (40 jobs)
#   128MB × 4 multipliers × 10 n=1 workloads
# ===============================================================
echo ""
echo "=============================="
echo " Stage 2: Read-Latency Sweep"
echo "=============================="

setup_stage "2_read_latency"

L3_MB=128
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
# STAGE 3: Leakage Gap Sensitivity (30 jobs)
#   128MB × 3 gap fractions (0.25, 0.50, 0.75) × 10 n=1 workloads
# ===============================================================
echo ""
echo "=============================="
echo " Stage 3: Leakage Gap"
echo "=============================="

setup_stage "3_leakage_gap"

L3_MB=128
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
# STAGE 4: Cap ± MAE Sensitivity (20 jobs)
#   128MB × 2 directions (cap-MAE, cap+MAE) × 10 n=1 workloads
#   n1_128M MAE = 0.663 W
# ===============================================================
echo ""
echo "=============================="
echo " Stage 4: Cap ± MAE"
echo "=============================="

setup_stage "4_cap_mae"

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
