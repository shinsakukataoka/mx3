#!/usr/bin/env bash
# run_vf_sensitivity.sh — traces runner with piecewise-linear power model (PLM)
# Identical to run_traces.sh except PLM flags are injected into VAR_FLAGS.
# Model: P_est = llc_leak_w + b_f + a_util * U_sum + a_ipc * U_sum * ipc_interval
#   U_sum = avg_util * N_cores  (total utilisation summed across all cores)
# One linear model entry per DVFS operating frequency, indexed by exact lookup.
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

BASE_FREQ_GHZ="${BASE_FREQ_GHZ:-2.2}"
BASE_PERIODIC_INS="${BASE_PERIODIC_INS:-2000000}"
LC_FMIN_GHZ="${LC_FMIN_GHZ:-1.6}"
DIR_ENTRIES="${DIR_ENTRIES:-4194304}"
MAX_SIM_MIN="${MAX_SIM_MIN:-0}"
FAIL_ON_SIFT_ASSERT="${FAIL_ON_SIFT_ASSERT:-1}"

# ---------------------------------------------------------------------------
# Piecewise-linear power model (PLM) config
#
# 7 representative sunnycove DVFS operating points.
# Coefficients are LINEAR-MODEL-EQUIVALENT PLACEHOLDERS until offline
# calibration is run via mcpat_plm_fit.py.
#
#   b_f      = p_static_w             = 20.08 W  (idle/uncore at that op-point)
#   a_util   = k_dyn * f_ghz / N_cal  (W per U_sum unit; U_sum = N_cores * avg_util)
#              Placeholder derived from k_dyn ≈ 2.45 W/GHz, N_cal = 8 (sunnycove):
#              k_dyn / N_cal = 2.45 / 8 = 0.30625 W/(GHz·U_sum)
#              Dividing by N_cal makes a_util portable across core counts:
#              P_dyn = a_util * U_sum scales correctly (doubles when N doubles).
#   a_ipc    = 0.0                    (IPC interaction term — set after regression)
#
# To update: replace PLM_F, PLM_B, PLM_AUTIL, PLM_AIPC with regression
# results from mcpat_plm_fit.py (see mx2/tools/mcpat_plm_fit.py).
# Optional override: set PLM_CFG_SH to an absolute path of a shell snippet
# that re-defines the four arrays before the flag-injection loop below.
# ---------------------------------------------------------------------------
PLM_N=7
PLM_F=(     1.6     1.9     2.2     2.5     3.0     3.5     4.0  )
PLM_B=(   20.08   20.08   20.08   20.08   20.08   20.08   20.08  )
PLM_AUTIL=( 0.490   0.582   0.674   0.766   0.919   1.072   1.225 )
PLM_AIPC=(  0.0     0.0     0.0     0.0     0.0     0.0     0.0   )
PLM_VERBOSE="${PLM_VERBOSE:-false}"

# Source an external coefficient file if provided (overrides arrays above)
if [[ -n "${PLM_CFG_SH:-}" && -f "$PLM_CFG_SH" ]]; then
  # shellcheck source=/dev/null
  source "$PLM_CFG_SH"
fi

source "$REPO_ROOT/mx3/engine/flags_common.sh"

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

# Standard variant flags
VAR_FLAGS=( $(flags_for_variant "$VARIANT") )
VAR_FLAGS+=( -g perf_model/dram_directory/total_entries="${DIR_ENTRIES}" )
# Override config total_cores to match SIM_N so LeakageConversion sees correct N
VAR_FLAGS+=( -g general/total_cores="${SIM_N}" )

if [[ "$VARIANT" == naive_* ]]; then
  VAR_FLAGS+=( -g lc/llc_leak_w=0 )
elif [[ "$VARIANT" == sram_* ]]; then
  set_nvsim_params
  sram_leak_w="$(awk -v mw="$SRAM_LEAK_MW" 'BEGIN{printf "%.6f", mw/1000.0}')"
  VAR_FLAGS+=( -g "lc/llc_leak_w=${sram_leak_w}" )
fi

# Piecewise-linear model flags.
# For baselines (lc/enabled=false), LeakageConversion returns early in the
# constructor and never reads these — they are harmless but present for uniformity.
VAR_FLAGS+=(
  -g lc/piecewise/enabled=true
  -g "lc/piecewise/verbose=${PLM_VERBOSE}"
  -g "lc/piecewise/n_models=${PLM_N}"
)
for (( _i=0; _i<PLM_N; _i++ )); do
  VAR_FLAGS+=(
    -g "lc/piecewise/${_i}/f_ghz=${PLM_F[$_i]}"
    -g "lc/piecewise/${_i}/b=${PLM_B[$_i]}"
    -g "lc/piecewise/${_i}/a_util=${PLM_AUTIL[$_i]}"
    -g "lc/piecewise/${_i}/a_ipc=${PLM_AIPC[$_i]}"
  )
done
unset _i

WARMUP_INS=$(( WARMUP_M * 1000000 ))
ROI_INS=$(( ROI_M * 1000000 ))
STOP_SPEC="stop-by-icount:${ROI_INS}:${WARMUP_INS}"

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
  power_model: piecewise_linear
  plm_n_models: $PLM_N
  plm_verbose: $PLM_VERBOSE
  plm_cfg_sh: "${PLM_CFG_SH:-<builtin defaults>}"
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
  echo "plm: n_models=${PLM_N} verbose=${PLM_VERBOSE} cfg=${PLM_CFG_SH:-<builtin>}"
  printf "CMD: %q " "${CMD[@]}"; echo
} > "$OUTDIR/cmd.info"

echo "[INFO] launching vf_sensitivity: ${ENV_RUN[*]} ${CMD[*]}"

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

echo "[OK] PLM_SENS $WORKLOAD | ${L3_MB}MB | $VARIANT | $TECH -> $OUTDIR"
