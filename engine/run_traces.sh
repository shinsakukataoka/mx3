#!/usr/bin/env bash
set -euo pipefail

: "${SNIPER_HOME:?SNIPER_HOME required}"
: "${TRACE_ROOT:?TRACE_ROOT required}"
: "${OUTDIR:?OUTDIR required}"
: "${WORKLOAD:?WORKLOAD required}"
: "${VARIANT:?VARIANT required}"
: "${TECH:?TECH required}"
: "${L3_MB:?L3_MB required}"
: "${ROI_M:?ROI_M required}"
: "${WARMUP_M:?WARMUP_M required}"
: "${SIM_N:?SIM_N required}"
: "${SNIPER_CONFIG:?SNIPER_CONFIG required}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"

CONDA_LIB="${CONDA_LIB:-${CONDA_SQLITE_LIB:-}}"
CONDA_PY="${CONDA_PY:-python3}"

BASE_FREQ_GHZ="${BASE_FREQ_GHZ:-2.66}"
BASE_PERIODIC_INS="${BASE_PERIODIC_INS:-2000000}"
LC_FMIN_GHZ="${LC_FMIN_GHZ:-1.6}"
DIR_ENTRIES="${DIR_ENTRIES:-4194304}"
MAX_SIM_MIN="${MAX_SIM_MIN:-0}"
FAIL_ON_SIFT_ASSERT="${FAIL_ON_SIFT_ASSERT:-1}"

source "$REPO_ROOT/mx2/engine/flags_common.sh"

# Build TRACE_LIST from WORKLOAD (supports A+B+C+D mixes)
IFS='+' read -r -a MIX <<< "$WORKLOAD"
TRACE_LIST=""
if [[ "${#MIX[@]}" -eq 1 ]]; then
  T="$TRACE_ROOT/${MIX[0]}.sift"
  [[ -s "$T" ]] || { echo "[ERR] missing trace: $T" >&2; exit 2; }
  for ((i=0;i<SIM_N;i++)); do
    TRACE_LIST+="$T"
    [[ $i -lt $((SIM_N-1)) ]] && TRACE_LIST+=","
  done
else
  [[ "${#MIX[@]}" -eq "$SIM_N" ]] || { echo "[ERR] mix must have SIM_N parts" >&2; exit 2; }
  for ((i=0;i<SIM_N;i++)); do
    T="$TRACE_ROOT/${MIX[$i]}.sift"
    [[ -s "$T" ]] || { echo "[ERR] missing trace: $T" >&2; exit 2; }
    TRACE_LIST+="$T"
    [[ $i -lt $((SIM_N-1)) ]] && TRACE_LIST+=","
  done
fi

mkdir -p "$OUTDIR"

# Flags for variant
VAR_FLAGS=( $(flags_for_variant "$VARIANT") )
VAR_FLAGS+=( -g perf_model/dram_directory/total_entries="${DIR_ENTRIES}" )

if [[ "$VARIANT" == naive_* ]]; then
  VAR_FLAGS+=( -g lc/llc_leak_w=0 )
elif [[ "$VARIANT" == sram_* ]]; then
  set_nvsim_params
  sram_leak_w="$(awk -v mw="$SRAM_LEAK_MW" 'BEGIN{printf "%.6f", mw/1000.0}')"
  VAR_FLAGS+=( -g "lc/llc_leak_w=${sram_leak_w}" )
fi

# stop-by-icount expects ROI then warmup
WARMUP_INS=$(( WARMUP_M * 1000000 ))
ROI_INS=$(( ROI_M * 1000000 ))
STOP_SPEC="stop-by-icount:${ROI_INS}:${WARMUP_INS}"

# run.yaml minimal
ts_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
sniper_git="$(git -C "$SNIPER_HOME" rev-parse --short HEAD 2>/dev/null || echo unknown)"
cat > "$OUTDIR/run.yaml" <<YAML
run:
  status: pending
  timestamp_utc: "$ts_utc"
  workload: "$WORKLOAD"
  variant: "$VARIANT"
  tech: "$TECH"
  l3_size_kb: $(( L3_MB * 1024 ))
  roi_m: $ROI_M
  warmup_m: $WARMUP_M
  sim_n: $SIM_N
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

{
  echo "workload=$WORKLOAD"
  echo "variant=$VARIANT"
  echo "tech=$TECH"
  echo "L3_MB=$L3_MB"
  echo "SIM_N=$SIM_N"
  echo "ROI_M=$ROI_M"
  echo "WARMUP_M=$WARMUP_M"
  echo "STOP_SPEC=$STOP_SPEC"
  echo "TRACE_LIST=$TRACE_LIST"
  printf "CMD: %q " "${CMD[@]}"; echo
} > "$OUTDIR/cmd.info"

echo "[INFO] launching traces: ${ENV_RUN[*]} ${CMD[*]}"

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

# dumpstats -> sim.out
"$CONDA_PY" "$SNIPER_HOME/tools/dumpstats.py" -d "$OUTDIR" >"$OUTDIR/sim.out" 2>"$OUTDIR/dumpstats.err" || true
ln -sf sim.out "$OUTDIR/dumpstats.out"

echo "[OK] TRACES $WORKLOAD | ${L3_MB}MB | $VARIANT | $TECH -> $OUTDIR"
