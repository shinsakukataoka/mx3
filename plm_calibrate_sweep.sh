#!/usr/bin/env bash
# plm_calibrate_sweep.sh — PLM calibration sweep.
#
# Generates fixed-frequency SRAM-only simulation jobs across n=1, n=4, n=8
# core counts, each with their own workload sets, at every frequency point.
#
# USAGE
# -----
#   bash mx3/plm_calibrate_sweep.sh --mode calib \
#       --sram-device mx3/config/devices/sunnycove/sram14.yaml
#
#   bash mx3/plm_calibrate_sweep.sh --mode calib \
#       --sram-device mx3/config/devices/sunnycove/sram14.yaml \
#       --l3-mb 16,32,128
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
MX="$REPO_ROOT/mx3/bin/mx"
SITE_YAML="$REPO_ROOT/mx3/config/site.yaml"
OUT_BASE="$REPO_ROOT/repro"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
MODE=calib        # calib | validate
L3_MB_LIST=""     # override with --l3-mb
UARCH=sunnycove   # override with --uarch
SRAM_DEVICE=""    # required: path to SRAM device YAML

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --l3-mb) L3_MB_LIST="$2"; shift 2 ;;
    --uarch) UARCH="$2"; shift 2 ;;
    --sram-device) SRAM_DEVICE="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 --sram-device <path> [--mode calib|validate] [--uarch sunnycove] [--l3-mb 16,32,128]"
      exit 0 ;;
    *) echo "[ERR] Unknown arg: $1"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Resolve device file
# ---------------------------------------------------------------------------
if [[ -z "$SRAM_DEVICE" ]]; then
  echo "[ERR] --sram-device is required.  Example: --sram-device mx3/config/devices/sunnycove/sram14.yaml" >&2
  exit 1
fi
SRAM_DEVICE="$(readlink -f "$SRAM_DEVICE")"
[[ -f "$SRAM_DEVICE" ]] || { echo "[ERR] device file not found: $SRAM_DEVICE" >&2; exit 1; }

DEVICES_DIR="$(dirname "$SRAM_DEVICE")"
TECH="$(basename "$SRAM_DEVICE" .yaml)"

# ---------------------------------------------------------------------------
# Frequency + LLC config per mode
# ---------------------------------------------------------------------------
case "$MODE" in
  calib|test)
    RUN_FREQS=( 2.0 2.1 2.2 2.3 2.4 2.5 2.6 2.7 2.8 2.9 3.0 3.1 3.2 3.3 3.4 3.5 3.6 3.7 3.8 3.9 4.0 )
    [[ -z "$L3_MB_LIST" ]] && L3_MB_LIST="16,32,128"
    ;;
  validate)
    RUN_FREQS=( 2.0 2.2 2.8 3.2 4.0 )
    [[ -z "$L3_MB_LIST" ]] && L3_MB_LIST="16,32,128"
    ;;
  *)
    echo "[ERR] Unknown mode: $MODE (supported: calib, test, validate)"
    exit 1 ;;
esac

IFS=',' read -r -a L3_SIZES <<< "$L3_MB_LIST"

ROI_M=1000
WARMUP_M=200
DIR_ENTRIES=4194304
BASE_PERIODIC_INS=2000000
FAIL_ON_SIFT_ASSERT=1

# ---------------------------------------------------------------------------
# Workloads per core count
# ---------------------------------------------------------------------------
BENCHES_N1=(
  "500.perlbench_r"
  "502.gcc_r"
  "505.mcf_r"
  "520.omnetpp_r"
  "523.xalancbmk_r"
  "531.deepsjeng_r"
  "541.leela_r"
  "557.xz_r"
  "648.exchange2_s"
  "649.fotonik3d_s"
)

BENCHES_N4=(
  "502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r"
  "505.mcf_r+500.perlbench_r+648.exchange2_s+649.fotonik3d_s"
  "505.mcf_r+505.mcf_r+502.gcc_r+502.gcc_r"
  "505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r"
  "523.xalancbmk_r+523.xalancbmk_r+502.gcc_r+502.gcc_r"
)

BENCHES_N8=(
  "502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r"
  "505.mcf_r+505.mcf_r+500.perlbench_r+500.perlbench_r+648.exchange2_s+648.exchange2_s+649.fotonik3d_s+649.fotonik3d_s"
  "505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r"
  "505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r"
  "523.xalancbmk_r+523.xalancbmk_r+523.xalancbmk_r+523.xalancbmk_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r"
)

# Held-out test workloads (excluded from calibration)
TEST_N4=(
  "557.xz_r+557.xz_r+557.xz_r+557.xz_r"
  "505.mcf_r+505.mcf_r+557.xz_r+557.xz_r"
  "502.gcc_r+502.gcc_r+557.xz_r+557.xz_r"
  "520.omnetpp_r+520.omnetpp_r+531.deepsjeng_r+531.deepsjeng_r"
  "505.mcf_r+557.xz_r+648.exchange2_s+649.fotonik3d_s"
  "500.perlbench_r+502.gcc_r+505.mcf_r+557.xz_r"
)

TEST_N8=(
  "557.xz_r+557.xz_r+557.xz_r+557.xz_r+557.xz_r+557.xz_r+557.xz_r+557.xz_r"
  "505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+557.xz_r+557.xz_r+557.xz_r+557.xz_r"
  "502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+557.xz_r+557.xz_r+557.xz_r+557.xz_r"
  "520.omnetpp_r+520.omnetpp_r+520.omnetpp_r+520.omnetpp_r+531.deepsjeng_r+531.deepsjeng_r+531.deepsjeng_r+531.deepsjeng_r"
  "505.mcf_r+505.mcf_r+557.xz_r+557.xz_r+648.exchange2_s+648.exchange2_s+649.fotonik3d_s+649.fotonik3d_s"
  "500.perlbench_r+500.perlbench_r+502.gcc_r+502.gcc_r+505.mcf_r+505.mcf_r+557.xz_r+557.xz_r"
)

# ---------------------------------------------------------------------------
# Read paths from site.yaml
# ---------------------------------------------------------------------------
read_yaml_key() {
  grep -E "^${1}:" "$2" | head -1 | sed 's/^[^:]*:[[:space:]]*//'
}
SNIPER_HOME="$(read_yaml_key SNIPER_HOME "$SITE_YAML")"
TRACE_ROOT="$(read_yaml_key  TRACE_ROOT  "$SITE_YAML")"
CONDA_LIB="$(read_yaml_key   CONDA_LIB  "$SITE_YAML")"
CONDA_PY="$(read_yaml_key    CONDA_PY   "$SITE_YAML")"
GCC_DIR="$(read_yaml_key     GCC_DIR    "$SITE_YAML")"

# ---------------------------------------------------------------------------
# Set up run directory  →  repro/calibration/<run_id>/
# ---------------------------------------------------------------------------
RUN_ID="plm_${MODE}_${UARCH}"
RUN_DIR="$OUT_BASE/calibration/$RUN_ID"
RUNS_ROOT="$RUN_DIR/runs"

mkdir -p "$RUNS_ROOT" "$RUN_DIR/slurm"

cat > "$RUN_DIR/env.sh" <<ENVSH
#!/usr/bin/env bash
set -euo pipefail
export SNIPER_HOME='${SNIPER_HOME}'
export TRACE_ROOT='${TRACE_ROOT}'
export CONDA_LIB='${CONDA_LIB}'
export CONDA_PY='${CONDA_PY}'
export GCC_DIR='${GCC_DIR}'
export REPO_ROOT='${REPO_ROOT}'
ENVSH

# ---------------------------------------------------------------------------
# Generate jobs.txt
# ---------------------------------------------------------------------------
JOBS_FILE="$RUN_DIR/jobs.txt"
: > "$JOBS_FILE"
JOB_COUNT=0

emit_jobs() {
  local sim_n="$1"
  shift
  local benches=("$@")

  for L3_MB in "${L3_SIZES[@]}"; do
    for FREQ in "${RUN_FREQS[@]}"; do
      FREQ_TAG="f${FREQ//./p}"
      for BENCH in "${benches[@]}"; do
        BENCH_TAG="${BENCH//+/_}"
        OUTDIR="${RUNS_ROOT}/n${sim_n}/${BENCH_TAG}/l3_${L3_MB}M/${FREQ_TAG}"
        _line="CAMPAIGN=plm_calib"
        _line+=" OUTDIR=${OUTDIR}"
        _line+=" JOB_OUTDIR=${OUTDIR}"
        _line+=" SNIPER_CONFIG=${UARCH}"
        _line+=" TECH=${TECH}"
        _line+=" DEVICES_DIR=${DEVICES_DIR}"
        _line+=" WORKLOAD=${BENCH}"
        _line+=" L3_MB=${L3_MB}"
        _line+=" ROI_M=${ROI_M}"
        _line+=" WARMUP_M=${WARMUP_M}"
        _line+=" SIM_N=${sim_n}"
        _line+=" BASE_FREQ_GHZ=${FREQ}"
        _line+=" BASE_PERIODIC_INS=${BASE_PERIODIC_INS}"
        _line+=" DIR_ENTRIES=${DIR_ENTRIES}"
        _line+=" FAIL_ON_SIFT_ASSERT=${FAIL_ON_SIFT_ASSERT}"
        echo "$_line" >> "$JOBS_FILE"
        (( JOB_COUNT++ )) || true
      done
    done
  done
}

if [[ "$MODE" == "test" ]]; then
  emit_jobs 4 "${TEST_N4[@]}"
  emit_jobs 8 "${TEST_N8[@]}"
else
  emit_jobs 1 "${BENCHES_N1[@]}"
  emit_jobs 4 "${BENCHES_N4[@]}"
  emit_jobs 8 "${BENCHES_N8[@]}"
fi

# ---------------------------------------------------------------------------
# Summary + next-step instructions
# ---------------------------------------------------------------------------
N_FREQS=${#RUN_FREQS[@]}
N_L3=${#L3_SIZES[@]}

if [[ "$MODE" == "test" ]]; then
  N4_JOBS=$(( ${#TEST_N4[@]} * N_FREQS * N_L3 ))
  N8_JOBS=$(( ${#TEST_N8[@]} * N_FREQS * N_L3 ))
  echo "=============================================="
  echo " PLM sweep — mode=${MODE}  uarch=${UARCH}"
  echo " LLC sizes:   ${L3_SIZES[*]} MB"
  echo " Device:      ${SRAM_DEVICE}"
  echo " Frequencies: ${N_FREQS} (${RUN_FREQS[0]}–${RUN_FREQS[-1]} GHz)"
  echo " n=4: ${#TEST_N4[@]} mixes   × ${N_FREQS} freqs × ${N_L3} L3 = ${N4_JOBS} jobs"
  echo " n=8: ${#TEST_N8[@]} mixes   × ${N_FREQS} freqs × ${N_L3} L3 = ${N8_JOBS} jobs"
else
  N1_JOBS=$(( ${#BENCHES_N1[@]} * N_FREQS * N_L3 ))
  N4_JOBS=$(( ${#BENCHES_N4[@]} * N_FREQS * N_L3 ))
  N8_JOBS=$(( ${#BENCHES_N8[@]} * N_FREQS * N_L3 ))
  echo "=============================================="
  echo " PLM sweep — mode=${MODE}  uarch=${UARCH}"
  echo " LLC sizes:   ${L3_SIZES[*]} MB"
  echo " Device:      ${SRAM_DEVICE}"
  echo " Frequencies: ${N_FREQS} (${RUN_FREQS[0]}–${RUN_FREQS[-1]} GHz)"
  echo " n=1: ${#BENCHES_N1[@]} benches × ${N_FREQS} freqs × ${N_L3} L3 = ${N1_JOBS} jobs"
  echo " n=4: ${#BENCHES_N4[@]} mixes   × ${N_FREQS} freqs × ${N_L3} L3 = ${N4_JOBS} jobs"
  echo " n=8: ${#BENCHES_N8[@]} mixes   × ${N_FREQS} freqs × ${N_L3} L3 = ${N8_JOBS} jobs"
fi
echo " Total jobs:  ${JOB_COUNT}"
echo " Run dir:     ${RUN_DIR}"
echo "=============================================="
echo
echo "[OK] planned ${JOB_COUNT} jobs -> ${RUN_DIR}"
echo

OUT_CAL_SH="$OUT_BASE/calibration/plm_${UARCH}_cal.sh"

echo "Next steps:"
echo
echo "  # 1. Submit:"
echo "  ${MX} submit ${RUN_DIR}"
echo
echo "  # 2. Verify:"
echo "  ${MX} verify ${RUN_DIR}"
echo
echo "  # 3. Extract oracle points:"
echo "  SNIPER_HOME=${SNIPER_HOME} ROOT=${RUNS_ROOT} \\"
echo "      bash ${REPO_ROOT}/mx3/tools/extract_oracle_points.sh"
echo
echo "  # 4. Fit PLM:"
echo "  python3 ${REPO_ROOT}/mx3/tools/mcpat_plm_fit.py \\"
echo "      --csv ${RUNS_ROOT}/oracle_points.csv \\"
echo "      --sniper-home ${SNIPER_HOME} \\"
echo "      --uarch ${UARCH} --calib-ncores 8 \\"
echo "      --out ${OUT_CAL_SH}"
