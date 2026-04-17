#!/usr/bin/env bash
set -euo pipefail

# run_hca_traces.sh — Multicore HCA simulation via SIFT traces.
#
# Combines:
#   - Trace-based multicore execution (from run_traces.sh)
#   - HCA hybrid cache flags (from hca_flags_common.sh)
#   - Fixed frequency, no DVFS controller
#
# Required env vars:
#   SNIPER_HOME, TRACE_ROOT, OUTDIR, WORKLOAD, VARIANT, TECH,
#   L3_MB, ROI_M, WARMUP_M, SIM_N, SNIPER_CONFIG,
#   SRAM_TECH, MRAM_TECH

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
: "${SRAM_TECH:?SRAM_TECH required}"
: "${MRAM_TECH:?MRAM_TECH required}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"

CONDA_LIB="${CONDA_LIB:-${CONDA_SQLITE_LIB:-}}"
CONDA_PY="${CONDA_PY:-python3}"

BASE_FREQ_GHZ="${BASE_FREQ_GHZ:-2.2}"
BASE_PERIODIC_INS="${BASE_PERIODIC_INS:-2000000}"
DIR_ENTRIES="${DIR_ENTRIES:-4194304}"
MAX_SIM_MIN="${MAX_SIM_MIN:-0}"
FAIL_ON_SIFT_ASSERT="${FAIL_ON_SIFT_ASSERT:-1}"
SANITY_CHECK_NO_MEM="${SANITY_CHECK_NO_MEM:-1}"

# Export DEVICES_DIR so hca_flags_common.sh can use it
export DEVICES_DIR="${DEVICES_DIR:-$REPO_ROOT/mx3/config/devices/sunnycove}"

# Source HCA flags (provides hca_flags_for_variant, hca_parse_variant)
source "$REPO_ROOT/mx3/engine/hca_flags_common.sh"

# Fixed-frequency flags (same as run_hca.sh)
fixed_freq_flags() {
  local per="$1"
  cat <<EOF
-g perf_model/core/frequency=${BASE_FREQ_GHZ}
-g dvfs/type=simple
-g dvfs/transition_latency=2000
-g dvfs/simple/cores_per_socket=${SIM_N}
-g core/hook_periodic_ins/ins_global=${per}
-g core/hook_periodic_ins/ins_per_core=0
EOF
}

# Build flags arrays
BASE_FLAGS=( $(fixed_freq_flags "$BASE_PERIODIC_INS") )
HCA_FLAGS=( $(hca_flags_for_variant "$VARIANT") )
VAR_FLAGS=( "${BASE_FLAGS[@]}" "${HCA_FLAGS[@]}" -g "perf_model/dram_directory/total_entries=${DIR_ENTRIES}" )

# For run.yaml knobs
hca_parse_variant "$VARIANT"

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
  [[ "${#MIX[@]}" -eq "$SIM_N" ]] || { echo "[ERR] mix must have SIM_N=$SIM_N parts, got ${#MIX[@]}" >&2; exit 2; }
  for ((i=0;i<SIM_N;i++)); do
    T="$TRACE_ROOT/${MIX[$i]}.sift"
    [[ -s "$T" ]] || { echo "[ERR] missing trace: $T" >&2; exit 2; }
    TRACE_LIST+="$T"
    [[ $i -lt $((SIM_N-1)) ]] && TRACE_LIST+=","
  done
fi

mkdir -p "$OUTDIR"

# stop-by-icount expects ROI then warmup
WARMUP_INS=$(( WARMUP_M * 1000000 ))
ROI_INS=$(( ROI_M * 1000000 ))
STOP_SPEC="stop-by-icount:${ROI_INS}:${WARMUP_INS}"

# run.yaml (HCA-specific knobs)
ts_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
sniper_git="$(git -C "$SNIPER_HOME" rev-parse --short HEAD 2>/dev/null || echo unknown)"
cat > "$OUTDIR/run.yaml" <<YAML
run:
  status: pending
  timestamp_utc: "$ts_utc"
  campaign: hca_traces
  workload: "$WORKLOAD"
  variant: "$VARIANT"
  tech: "$TECH"
  sram_tech: "$SRAM_TECH"
  mram_tech: "$MRAM_TECH"
  l3_size_kb: $(( L3_MB * 1024 ))
  roi_m: $ROI_M
  warmup_m: $WARMUP_M
  sim_n: $SIM_N
paths:
  trace_list: "$TRACE_LIST"
versions:
  sniper_git: "$sniper_git"
knobs:
  hca:
    sram_ways: ${HCA_SRAM_WAYS}
    fill_to: "${HCA_FILL_TO}"
    migration_enabled: ${HCA_MIG_ENABLED}
    promote_after_hits: ${HCA_PROMOTE:-null}
    cooldown_hits: ${HCA_COOLDOWN:-null}
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
  echo "sram_tech=$SRAM_TECH"
  echo "mram_tech=$MRAM_TECH"
  echo "L3_MB=$L3_MB"
  echo "SIM_N=$SIM_N"
  echo "ROI_M=$ROI_M"
  echo "WARMUP_M=$WARMUP_M"
  echo "STOP_SPEC=$STOP_SPEC"
  echo "TRACE_LIST=$TRACE_LIST"
  printf "CMD: %q " "${CMD[@]}"; echo
} > "$OUTDIR/cmd.info"

echo "[INFO] launching HCA traces: ${ENV_RUN[*]} ${CMD[*]}"

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

if [[ "$SANITY_CHECK_NO_MEM" == "1" && -s "$OUTDIR/sim.out" ]]; then
  l1d_access=$(awk '
    $1=="Cache" && $2=="L1-D" {inblk=1; next}
    inblk && $1=="num" && $2=="cache" && $3=="accesses" {
      n=split($0,a,"|"); gsub(/[ \t]/,"",a[2]); print a[2]; exit
    }
    inblk && $1=="Cache" {inblk=0}
  ' "$OUTDIR/sim.out" 2>/dev/null || true)
  if [[ "${l1d_access:-}" == "0" || -z "${l1d_access:-}" ]]; then
    echo "[ERR] Sanity check failed: Cache L1-D accesses are zero." | tee -a "$OUTDIR/sniper.log"
    exit 93
  fi
fi

echo "[OK] HCA_TRACES $WORKLOAD | n${SIM_N} | ${L3_MB}MB | $VARIANT | $TECH -> $OUTDIR"
