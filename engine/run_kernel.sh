#!/usr/bin/env bash
set -euo pipefail

: "${SNIPER_HOME:?SNIPER_HOME required}"
: "${OUTDIR:?OUTDIR required}"
: "${KERNEL:?KERNEL required}"
: "${VARIANT:?VARIANT required}"
: "${TECH:?TECH required}"
: "${L3_MB:?L3_MB required}"
: "${ROI_M:?ROI_M required}"
: "${WARMUP_M:?WARMUP_M required}"
: "${SIM_N:?SIM_N required}"
: "${SNIPER_CONFIG:?SNIPER_CONFIG required}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"

CONDA_SQLITE_LIB="${CONDA_SQLITE_LIB:-${CONDA_LIB:-}}"
MAX_SIM_MIN="${MAX_SIM_MIN:-0}"
BASE_FREQ_GHZ="${BASE_FREQ_GHZ:-2.66}"
BASE_PERIODIC_INS="${BASE_PERIODIC_INS:-2000000}"
DIR_ENTRIES="${DIR_ENTRIES:-4194304}"
FAIL_ON_SIFT_ASSERT="${FAIL_ON_SIFT_ASSERT:-1}"
SANITY_CHECK_NO_MEM="${SANITY_CHECK_NO_MEM:-1}"

export LD_LIBRARY_PATH="${CONDA_SQLITE_LIB}${CONDA_SQLITE_LIB:+:}${LD_LIBRARY_PATH:-}"

[[ -x "$SNIPER_HOME/run-sniper" ]] || { echo "[ERR] missing $SNIPER_HOME/run-sniper" >&2; exit 11; }
[[ -f "$SNIPER_HOME/scripts/roi-icount.py" ]] || { echo "[ERR] missing roi-icount.py" >&2; exit 12; }

source "$REPO_ROOT/mx3/engine/flags_common.sh"

mkdir -p "$OUTDIR"

VAR_FLAGS=( $(flags_for_variant "$VARIANT") )
VAR_FLAGS+=( -g perf_model/dram_directory/total_entries="${DIR_ENTRIES}" )

if [[ "$VARIANT" == naive_* ]]; then
  VAR_FLAGS+=( -g lc/llc_leak_w=0 )
elif [[ "$VARIANT" == sram_* ]]; then
  set_nvsim_params
  sram_leak_w="$(awk -v mw="$SRAM_LEAK_MW" 'BEGIN{printf "%.6f", mw/1000.0}')"
  VAR_FLAGS+=( -g "lc/llc_leak_w=${sram_leak_w}" )
fi

STOP_ICOUNT=$(( ROI_M * 1000000 ))
WARM_ICOUNT=$(( WARMUP_M * 1000000 ))
USE_WARMUP=0
[[ "$WARMUP_M" -gt 0 ]] && USE_WARMUP=1

# Kernel command selection (must be set in site.yaml)
APP_CMD=()
EXTRA_LD=""
case "$KERNEL" in
  blis_gemm)
    : "${BLIS_BIN:?Set BLIS_BIN in mx3/config/site.yaml}"
    BLIS_M="${BLIS_M:-1536}"
    BLIS_N="${BLIS_N:-1536}"
    BLIS_K="${BLIS_K:-1536}"
    BLIS_REPS="${BLIS_REPS:-50}"
    APP_CMD=( "$BLIS_BIN" "$BLIS_M" "$BLIS_N" "$BLIS_K" "$BLIS_REPS" )
    if [[ -n "${BLIS_LIBDIR:-}" ]]; then
      EXTRA_LD="${BLIS_LIBDIR}"
    fi
    ;;
  simdjson_ondemand)
    : "${SIMDJSON_BIN:?Set SIMDJSON_BIN in mx3/config/site.yaml}"
    : "${JSON_INPUT:?Set JSON_INPUT in mx3/config/site.yaml}"
    SIMDJSON_REPS="${SIMDJSON_REPS:-2000}"
    APP_CMD=( "$SIMDJSON_BIN" "$JSON_INPUT" "$SIMDJSON_REPS" )
    ;;
  *)
    echo "[ERR] Unknown KERNEL=$KERNEL" >&2
    exit 20
    ;;
esac
[[ -x "${APP_CMD[0]}" ]] || { echo "[ERR] kernel binary not executable: ${APP_CMD[0]}" >&2; exit 21; }

# tmp
TMP_PARENT="${SLURM_TMPDIR:-${TMPDIR:-$OUTDIR/.tmp}}"
TMP_BASE="${TMP_PARENT%/}/sniper_${KERNEL}_${VARIANT}_${SLURM_JOB_ID:-nojob}_${SLURM_ARRAY_TASK_ID:-notask}"
mkdir -p "$TMP_BASE" && chmod 700 "$TMP_BASE" 2>/dev/null || true

# run.yaml minimal
ts_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
sniper_git="$(git -C "$SNIPER_HOME" rev-parse --short HEAD 2>/dev/null || echo unknown)"
cat > "$OUTDIR/run.yaml" <<YAML
run:
  status: pending
  timestamp_utc: "$ts_utc"
  kernel: "$KERNEL"
  variant: "$VARIANT"
  tech: "$TECH"
  l3_size_kb: $(( L3_MB * 1024 ))
  roi_m: $ROI_M
  warmup_m: $WARMUP_M
  sim_n: $SIM_N
versions:
  sniper_git: "$sniper_git"
YAML

CMD_CORE=( "$SNIPER_HOME/run-sniper" -c "$SNIPER_CONFIG" -n "$SIM_N" -d "$OUTDIR" )
CMD_CORE+=( -g traceinput/enabled=false )
CMD_CORE+=( --roi-script )
if [[ "$USE_WARMUP" -eq 1 ]]; then
  CMD_CORE+=( -s "$SNIPER_HOME/scripts/roi-icount.py:0:${WARM_ICOUNT}:${STOP_ICOUNT}" )
else
  CMD_CORE+=( -s "$SNIPER_HOME/scripts/roi-icount.py:0:0:${STOP_ICOUNT}" )
fi
CMD_CORE+=( "${VAR_FLAGS[@]}" -- "${APP_CMD[@]}" )

env | sort > "$OUTDIR/env.caller.dump"
printf "CMD: %q " "${CMD_CORE[@]}" > "$OUTDIR/cmd.info"; echo >> "$OUTDIR/cmd.info"

LD_COMBINED="${LD_LIBRARY_PATH:-}"
if [[ -n "$EXTRA_LD" ]]; then
  LD_COMBINED="${EXTRA_LD}:${LD_COMBINED}"
fi

LAUNCH=( env -i PATH="$PATH" LD_LIBRARY_PATH="$LD_COMBINED"
  TMPDIR="$TMP_BASE" TMP="$TMP_BASE" TEMP="$TMP_BASE"
  HOME="$HOME" USER="${USER:-unknown}" SNIPER_HOME="$SNIPER_HOME"
  SNIPER_USE_SIFT=0 SNIPER_OPTIONS= SDE_ARGS= SDE_EXTRA_ARGS= PYTHONPATH=
  "${CMD_CORE[@]}" )

echo "[INFO] launching kernel: ${LAUNCH[*]}"

if command -v timeout >/dev/null 2>&1 && [[ "$MAX_SIM_MIN" -gt 0 ]]; then
  set +e
  timeout "${MAX_SIM_MIN}m" "${LAUNCH[@]}" >"$OUTDIR/sniper.log" 2>&1
  rc=$?
  set -e
  [[ $rc -eq 0 ]] || { tail -n 120 "$OUTDIR/sniper.log" || true; exit "$rc"; }
else
  "${LAUNCH[@]}" >"$OUTDIR/sniper.log" 2>&1
fi

if [[ "$FAIL_ON_SIFT_ASSERT" == "1" ]]; then
  if grep -qE 'zfstream\.cc:|sift_reader\.cc:|SIFT\].*Assertion' "$OUTDIR/sniper.log"; then
    echo "[ERR] Detected SIFT assertion(s)" | tee -a "$OUTDIR/sniper.log"
    exit 94
  fi
fi

if [[ ! -s "$OUTDIR/sim.out" && -s "$OUTDIR/sim.stats.sqlite3" ]]; then
  python3 "$SNIPER_HOME/tools/dumpstats.py" -d "$OUTDIR" > "$OUTDIR/sim.out" 2>>"$OUTDIR/sniper.log" || true
fi

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

echo "[OK] KERNEL $KERNEL | ${L3_MB}MB | $VARIANT | $TECH -> $OUTDIR"
