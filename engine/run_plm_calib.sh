#!/usr/bin/env bash
# run_plm_calib.sh — fixed-frequency SRAM-only calibration runner for PLM.
#
# Each job: one benchmark (4 homogeneous copies) at one fixed calibration
# frequency.  LC is always disabled (baseline_sram_only variant).
# Outputs are compatible with extract_oracle_points.sh and mcpat_plm_fit.py.
#
# Required env vars (set by jobs.txt / dispatch):
#   SNIPER_HOME, TRACE_ROOT, OUTDIR, WORKLOAD (single bench),
#   TECH (e.g. sram14), L3_MB, ROI_M, WARMUP_M, SIM_N,
#   SNIPER_CONFIG (uarch label, e.g. sunnycove),
#   BASE_FREQ_GHZ (calibration frequency, e.g. 2.2 / 3.0 / 4.0)
set -euo pipefail

: "${SNIPER_HOME:?SNIPER_HOME required}"
: "${TRACE_ROOT:?TRACE_ROOT required}"
: "${OUTDIR:?OUTDIR required}"
: "${WORKLOAD:?WORKLOAD required (single bench, e.g. 500.perlbench_r)}"
: "${TECH:?TECH required (e.g. sram14)}"
: "${L3_MB:?L3_MB required}"
: "${ROI_M:?ROI_M required}"
: "${WARMUP_M:?WARMUP_M required}"
: "${SIM_N:?SIM_N required}"
: "${SNIPER_CONFIG:?SNIPER_CONFIG required}"
: "${BASE_FREQ_GHZ:?BASE_FREQ_GHZ required (calibration frequency, e.g. 2.2)}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"

CONDA_LIB="${CONDA_LIB:-${CONDA_SQLITE_LIB:-}}"
CONDA_PY="${CONDA_PY:-python3}"

BASE_PERIODIC_INS="${BASE_PERIODIC_INS:-2000000}"
DIR_ENTRIES="${DIR_ENTRIES:-4194304}"
MAX_SIM_MIN="${MAX_SIM_MIN:-0}"
FAIL_ON_SIFT_ASSERT="${FAIL_ON_SIFT_ASSERT:-1}"

source "$REPO_ROOT/mx3/engine/flags_common.sh"

# Build TRACE_LIST: supports both single bench (SIM_N copies) and A+B+C+D mixes.
# For mixes the number of elements must equal SIM_N.
IFS='+' read -r -a MIX <<< "$WORKLOAD"
TRACE_LIST=""
if [[ "${#MIX[@]}" -eq 1 ]]; then
  T="$TRACE_ROOT/${WORKLOAD}.sift"
  [[ -s "$T" ]] || { echo "[ERR] missing trace: $T" >&2; exit 2; }
  for ((i=0; i<SIM_N; i++)); do
    TRACE_LIST+="$T"
    [[ $i -lt $((SIM_N-1)) ]] && TRACE_LIST+=","
  done
else
  [[ "${#MIX[@]}" -eq "$SIM_N" ]] || {
    echo "[ERR] mix has ${#MIX[@]} parts but SIM_N=${SIM_N}" >&2; exit 2; }
  for ((i=0; i<SIM_N; i++)); do
    T="$TRACE_ROOT/${MIX[$i]}.sift"
    [[ -s "$T" ]] || { echo "[ERR] missing trace: $T" >&2; exit 2; }
    TRACE_LIST+="$T"
    [[ $i -lt $((SIM_N-1)) ]] && TRACE_LIST+=","
  done
fi

mkdir -p "$OUTDIR"

# Always baseline_sram_only: lc/enabled=false, SRAM leakage, frequency pinned
# to BASE_FREQ_GHZ via dvfs_hpi_flags inside flags_for_variant.
VAR_FLAGS=( $(flags_for_variant "baseline_sram_only") )
VAR_FLAGS+=( -g perf_model/dram_directory/total_entries="${DIR_ENTRIES}" )

WARMUP_INS=$(( WARMUP_M * 1000000 ))
ROI_INS=$(( ROI_M * 1000000 ))
STOP_SPEC="stop-by-icount:${ROI_INS}:${WARMUP_INS}"

ts_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
sniper_git="$(git -C "$SNIPER_HOME" rev-parse --short HEAD 2>/dev/null || echo unknown)"

# run.yaml — extract_oracle_points.sh expects bench: and l3_size_kb: fields
cat > "$OUTDIR/run.yaml" <<YAML
run:
  status: pending
  timestamp_utc: "$ts_utc"
  workload: "$WORKLOAD"
  bench: "$WORKLOAD"
  variant: baseline_sram_only
  tech: "$TECH"
  l3_size_kb: $(( L3_MB * 1024 ))
  f_ghz: $BASE_FREQ_GHZ
  roi_m: $ROI_M
  warmup_m: $WARMUP_M
  sim_n: $SIM_N
  power_model: plm_calibration
paths:
  trace_list: "$TRACE_LIST"
versions:
  sniper_git: "$sniper_git"
YAML

CMD=( "$SNIPER_HOME/run-sniper" --roi-script -c "$SNIPER_CONFIG" -n "$SIM_N" -d "$OUTDIR"
      "${VAR_FLAGS[@]}"
      -s "$STOP_SPEC"
      --traces="$TRACE_LIST"
)

ENV_RUN=( env )
if [[ -n "${CONDA_LIB:-}" ]]; then
  ENV_RUN+=( LD_LIBRARY_PATH="$CONDA_LIB:${LD_LIBRARY_PATH:-}" )
fi

# cmd.info — extract_oracle_points.sh greps for:
#   perf_model/core/frequency=X        (set by dvfs_hpi_flags via BASE_FREQ_GHZ)
#   perf_model/l3_cache/llc/leak_power_mW=X  (set by baseline_sram_only flags)
# Both appear in VAR_FLAGS → CMD → cmd.info via printf %q.
{
  echo "workload=$WORKLOAD"
  echo "bench=$WORKLOAD"
  echo "variant=baseline_sram_only"
  echo "tech=$TECH"
  echo "L3_MB=$L3_MB"
  echo "SIM_N=$SIM_N"
  echo "ROI_M=$ROI_M"
  echo "WARMUP_M=$WARMUP_M"
  echo "BASE_FREQ_GHZ=$BASE_FREQ_GHZ"
  echo "STOP_SPEC=$STOP_SPEC"
  echo "TRACE_LIST=$TRACE_LIST"
  printf "CMD: %q " "${CMD[@]}"; echo
} > "$OUTDIR/cmd.info"

echo "[INFO] PLM_CALIB $WORKLOAD @ ${BASE_FREQ_GHZ}GHz n=${SIM_N} ${L3_MB}MB ${TECH}"

if command -v timeout >/dev/null 2>&1 && [[ "$MAX_SIM_MIN" -gt 0 ]]; then
  "${ENV_RUN[@]}" timeout -s INT "${MAX_SIM_MIN}m" "${CMD[@]}" >"$OUTDIR/sniper.log" 2>&1
else
  "${ENV_RUN[@]}" "${CMD[@]}" >"$OUTDIR/sniper.log" 2>&1
fi

if [[ "$FAIL_ON_SIFT_ASSERT" == "1" ]]; then
  if grep -qE 'zfstream\.cc:|sift_reader\.cc:|SIFT\].*Assertion' "$OUTDIR/sniper.log"; then
    echo "[ERR] Detected SIFT assertion(s)" | tee -a "$OUTDIR/sniper.log"
    exit 94
  fi
fi

"$CONDA_PY" "$SNIPER_HOME/tools/dumpstats.py" -d "$OUTDIR" >"$OUTDIR/sim.out" 2>"$OUTDIR/dumpstats.err" || true
ln -sf sim.out "$OUTDIR/dumpstats.out"

echo "[OK] PLM_CALIB $WORKLOAD | ${BASE_FREQ_GHZ}GHz | ${L3_MB}MB | ${TECH} -> $OUTDIR"
