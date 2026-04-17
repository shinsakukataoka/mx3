#!/usr/bin/env bash
# hca_multicore_sweep.sh — Multicore HCA reproduction study (120 jobs).
#
# 4 HCA variants × 3 L3 capacities × 10 multicore workloads = 120 jobs
#
# Variants (all unrestricted fill, noparity, fillmram):
#   Static:  noparity_s4_fillmram, noparity_s8_fillmram, noparity_s12_fillmram
#   Dynamic: noparity_s4_fillmram_p4_c32
#
# Capacities: 16, 32, 128 MB
# Workloads:  5 × n=4  +  5 × n=8
#
# USAGE
# -----
#   bash mx3/hca_multicore_sweep.sh                # plan all
#   mx3/bin/mx submit repro/hca/<stage>            # submit a stage
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
MX="$REPO_ROOT/mx3/bin/mx"
DEV_DIR="$REPO_ROOT/mx3/config/devices/sunnycove"
SITE_YAML="$REPO_ROOT/mx3/config/site.yaml"
OUT_BASE="$REPO_ROOT/repro"

UARCH="sunnycove"
BASE_FREQ_GHZ="2.2"
ROI_M=1000
WARMUP_M=200
DIR_ENTRIES=4194304
BASE_PERIODIC_INS=2000000
FAIL_ON_SIFT_ASSERT=1

SRAM_TECH="sram14"
MRAM_TECH="mram14"
TECH_TAG="sram14_mram14"

STUDY="hca_multicore"
TOTAL_JOBS=0

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
# Multicore workloads
# ---------------------------------------------------------------------------
N4_WORKLOADS=(
  "502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r"
  "505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r"
  "557.xz_r+557.xz_r+557.xz_r+557.xz_r"
  "505.mcf_r+505.mcf_r+502.gcc_r+502.gcc_r"
  "505.mcf_r+505.mcf_r+557.xz_r+557.xz_r"
)

N8_WORKLOADS=(
  "502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r"
  "505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r"
  "557.xz_r+557.xz_r+557.xz_r+557.xz_r+557.xz_r+557.xz_r+557.xz_r+557.xz_r"
  "505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r"
  "505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+557.xz_r+557.xz_r+557.xz_r+557.xz_r"
)

# HCA variants
VARIANTS=(
  "noparity_s4_fillmram"
  "noparity_s8_fillmram"
  "noparity_s12_fillmram"
  "noparity_s4_fillmram_p4_c32"
  "baseline_sram_only"
  "baseline_mram_only"
)
L3_CAPS=(16 32 128)

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
  local VARIANT="$1" WORKLOAD="$2" CORES="$3" L3_MB="$4"

  local OUTDIR="${RUNS_ROOT}/${WORKLOAD}/n${CORES}/l3_${L3_MB}MB/${VARIANT}_${TECH_TAG}"

  local _line="CAMPAIGN=hca_traces"
  _line+=" OUTDIR=${OUTDIR} JOB_OUTDIR=${OUTDIR}"
  _line+=" SNIPER_CONFIG=${UARCH} TECH=${TECH_TAG}"
  _line+=" SRAM_TECH=${SRAM_TECH} MRAM_TECH=${MRAM_TECH}"
  _line+=" WORKLOAD=${WORKLOAD} L3_MB=${L3_MB} VARIANT=${VARIANT}"
  _line+=" ROI_M=${ROI_M} WARMUP_M=${WARMUP_M}"
  _line+=" SIM_N=${CORES} BASE_FREQ_GHZ=${BASE_FREQ_GHZ}"
  _line+=" BASE_PERIODIC_INS=${BASE_PERIODIC_INS}"
  _line+=" DIR_ENTRIES=${DIR_ENTRIES}"
  _line+=" FAIL_ON_SIFT_ASSERT=${FAIL_ON_SIFT_ASSERT}"
  _line+=" DEVICES_DIR=${DEV_DIR}"

  echo "$_line" >> "$JOBS_FILE"
  (( JOB_COUNT++ )) || true
}

# ---------------------------------------------------------------------------
# Setup a stage
# ---------------------------------------------------------------------------
setup_stage() {
  local stage="$1"
  RUN_DIR="$OUT_BASE/hca/$stage"
  RUNS_ROOT="$RUN_DIR/runs"
  JOBS_FILE="$RUN_DIR/jobs.txt"
  mkdir -p "$RUNS_ROOT" "$RUN_DIR/slurm"
  write_env "$RUN_DIR"
  : > "$JOBS_FILE"
  JOB_COUNT=0
}

# ===============================================================
# STAGE 1: Multicore HCA
# ===============================================================
echo ""
echo "=============================="
echo " Multicore HCA"
echo "=============================="

setup_stage "${STUDY}/1_multicore_hca"

for VARIANT in "${VARIANTS[@]}"; do
  for L3_MB in "${L3_CAPS[@]}"; do
    # n=4 workloads
    for WORKLOAD in "${N4_WORKLOADS[@]}"; do
      emit_job "$VARIANT" "$WORKLOAD" 4 "$L3_MB"
    done
    # n=8 workloads
    for WORKLOAD in "${N8_WORKLOADS[@]}"; do
      emit_job "$VARIANT" "$WORKLOAD" 8 "$L3_MB"
    done
  done
done

TOTAL_JOBS=$(( TOTAL_JOBS + JOB_COUNT ))
echo "  -> $JOB_COUNT jobs"

# ===============================================================
# Summary
# ===============================================================
echo ""
echo "=============================================="
echo " Multicore HCA Study — Complete"
echo "=============================================="
echo " Uarch:      ${UARCH} @ ${BASE_FREQ_GHZ} GHz"
echo " Devices:    ${DEV_DIR}"
echo " SRAM tech:  ${SRAM_TECH}"
echo " MRAM tech:  ${MRAM_TECH}"
echo " Total jobs: ${TOTAL_JOBS}"
echo " Output:     ${OUT_BASE}/hca/${STUDY}/"
echo "=============================================="
echo ""
echo "Submit:"
echo "  for d in ${OUT_BASE}/hca/${STUDY}/*/; do"
echo "    ${MX} submit \"\$d\""
echo "  done"
