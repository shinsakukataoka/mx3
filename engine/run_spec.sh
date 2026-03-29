#!/usr/bin/env bash
set -euo pipefail

: "${SNIPER_HOME:?SNIPER_HOME required}"
: "${SPEC_ROOT:?SPEC_ROOT required}"
: "${OUTDIR:?OUTDIR required}"
: "${BENCH:?BENCH required}"
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
SPEC_SIZE="${SPEC_SIZE:-ref}"
SKIP_SPEC_BUILD="${SKIP_SPEC_BUILD:-1}"
BASE_FREQ_GHZ="${BASE_FREQ_GHZ:-2.66}"
BASE_PERIODIC_INS="${BASE_PERIODIC_INS:-2000000}"
DIR_ENTRIES="${DIR_ENTRIES:-4194304}"
FAIL_ON_SIFT_ASSERT="${FAIL_ON_SIFT_ASSERT:-1}"
SANITY_CHECK_NO_MEM="${SANITY_CHECK_NO_MEM:-1}"

export LD_LIBRARY_PATH="${CONDA_SQLITE_LIB}${CONDA_SQLITE_LIB:+:}${LD_LIBRARY_PATH:-}"

[[ -x "$SNIPER_HOME/run-sniper" ]] || { echo "[ERR] missing $SNIPER_HOME/run-sniper" >&2; exit 11; }
[[ -f "$SNIPER_HOME/scripts/roi-icount.py" ]] || { echo "[ERR] missing roi-icount.py" >&2; exit 12; }
[[ -f "$SPEC_ROOT/shrc" ]] || { echo "[ERR] missing $SPEC_ROOT/shrc" >&2; exit 13; }

source "$REPO_ROOT/mx3/engine/flags_common.sh"

# Build flags
VAR_FLAGS=( $(flags_for_variant "$VARIANT") )
VAR_FLAGS+=( -g perf_model/dram_directory/total_entries="${DIR_ENTRIES}" )

# Apply naive leak override to estimator (sram_ handled in flags_common.sh via LLC_LEAK_OVERRIDE)
if [[ "$VARIANT" == naive_* ]]; then
  VAR_FLAGS+=( -g lc/llc_leak_w=0 )
fi

# Load SPEC env + resolve latest run directory
pushd "$SPEC_ROOT" >/dev/null
# shellcheck source=/dev/null
. ./shrc
popd >/dev/null

command -v runcpu >/dev/null || { echo "[ERR] runcpu not found after sourcing shrc" >&2; exit 5; }

BENCH_DIR="$SPEC_ROOT/benchspec/CPU/$BENCH"
RUN_ROOT="$BENCH_DIR/run"
if ! ls -dt "$RUN_ROOT"/run_* >/dev/null 2>&1; then
  if [[ "$SKIP_SPEC_BUILD" == "1" ]]; then
    echo "[ERR] No run_* for $BENCH and SKIP_SPEC_BUILD=1" >&2
    exit 6
  else
    echo "[ERR] SPEC build/run not implemented in mx3 yet (set SKIP_SPEC_BUILD=1 and pre-run SPEC once)" >&2
    exit 6
  fi
fi
RUN_DIR="$(ls -dt "$RUN_ROOT"/run_* | head -1)"

RUN_CWD="$(awk '$1=="-C"{dir=$2} END{print dir}' "$RUN_DIR/speccmds.cmd")"
RUN_CWD="${RUN_CWD:-$RUN_DIR}"
CMD_LINE="$(awk '/^[[:space:]]*-o[[:space:]]+/{L=$0;sub(/^[ \t]*-o[ \t]+\S+[ \t]+-e[ \t]+\S+[ \t]+/,"",L);sub(/[ \t]*>[ \t].*$/,"",L);sub(/[ \t]*2>>[ \t].*$/,"",L);print L;exit}' "$RUN_DIR/speccmds.cmd")"
[[ -n "${CMD_LINE:-}" ]] || { echo "[ERR] failed to parse speccmds.cmd for $BENCH" >&2; exit 7; }

eval "set -- $CMD_LINE"
PROG="$1"; shift || true
ABS_PROG=$([[ "$PROG" = /* ]] && echo "$PROG" || readlink -f "$RUN_CWD/$PROG")
APP_CMD=( "$ABS_PROG" "$@" )
[[ -x "$ABS_PROG" ]] || { echo "[ERR] program not executable: $ABS_PROG" >&2; exit 8; }

# ROI icounts
STOP_ICOUNT=$(( ROI_M * 1000000 ))
WARM_ICOUNT=$(( WARMUP_M * 1000000 ))
USE_WARMUP=0
if [[ "$WARMUP_M" -gt 0 ]]; then USE_WARMUP=1; fi

mkdir -p "$OUTDIR"

# tmp dir
TMP_PARENT="${SLURM_TMPDIR:-${TMPDIR:-$OUTDIR/.tmp}}"
TMP_BASE="${TMP_PARENT%/}/sniper_${BENCH//./_}_${VARIANT}_${SLURM_JOB_ID:-nojob}_${SLURM_ARRAY_TASK_ID:-notask}"
mkdir -p "$TMP_BASE" && chmod 700 "$TMP_BASE" 2>/dev/null || true

# run.yaml (minimal)
ts_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
sniper_git="$(git -C "$SNIPER_HOME" rev-parse --short HEAD 2>/dev/null || echo unknown)"
cat > "$OUTDIR/run.yaml" <<YAML
run:
  status: pending
  timestamp_utc: "$ts_utc"
  bench: "$BENCH"
  variant: "$VARIANT"
  tech: "$TECH"
  l3_size_kb: $(( L3_MB * 1024 ))
  roi_m: $ROI_M
  warmup_m: $WARMUP_M
  sim_n: $SIM_N
  spec_size: "$SPEC_SIZE"
versions:
  sniper_git: "$sniper_git"
YAML

# Build run-sniper command
CMD_CORE=( "$SNIPER_HOME/run-sniper" -c "$SNIPER_CONFIG" -n "$SIM_N" -d "$OUTDIR" )
CMD_CORE+=( -g traceinput/enabled=false )
CMD_CORE+=( --roi-script )
if [[ "$USE_WARMUP" -eq 1 ]]; then
  CMD_CORE+=( -s "$SNIPER_HOME/scripts/roi-icount.py:0:${WARM_ICOUNT}:${STOP_ICOUNT}" )
else
  CMD_CORE+=( -s "$SNIPER_HOME/scripts/roi-icount.py:0:0:${STOP_ICOUNT}" )
fi
CMD_CORE+=( "${VAR_FLAGS[@]}" -- "${APP_CMD[@]}" )

# Save caller env + cmd
pushd "$RUN_CWD" >/dev/null
env | sort > "$OUTDIR/env.caller.dump"
popd >/dev/null

{
  echo "bench=$BENCH"
  echo "variant=$VARIANT"
  echo "tech=$TECH"
  echo "L3_MB=$L3_MB"
  echo "ROI_M=$ROI_M"
  echo "WARMUP_M=$WARMUP_M"
  echo "SIM_N=$SIM_N"
  echo "OUTDIR=$OUTDIR"
  echo "TMP_BASE=$TMP_BASE"
  printf "CMD: %q " "${CMD_CORE[@]}"; echo
} > "$OUTDIR/cmd.info"

# Launch with sanitized env
LAUNCH=( env -i \
  PATH="$PATH" \
  LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}" \
  TMPDIR="$TMP_BASE" TMP="$TMP_BASE" TEMP="$TMP_BASE" \
  HOME="$HOME" USER="${USER:-unknown}" \
  SNIPER_HOME="$SNIPER_HOME" SPEC_ROOT="$SPEC_ROOT" GCC_DIR="${GCC_DIR:-}" \
  SNIPER_USE_SIFT=0 SNIPER_OPTIONS= SDE_ARGS= SDE_EXTRA_ARGS= PYTHONPATH= \
  "${CMD_CORE[@]}" )

echo "[INFO] launching: ${LAUNCH[*]}"

# Run the SPEC binary from RUN_CWD (SPEC run directory) so relative inputs resolve
pushd "$RUN_CWD" >/dev/null

if command -v timeout >/dev/null 2>&1 && [[ "$MAX_SIM_MIN" -gt 0 ]]; then
  set +e
  timeout "${MAX_SIM_MIN}m" "${LAUNCH[@]}" >"$OUTDIR/sniper.log" 2>&1
  rc=$?
  set -e
  if [[ $rc -ne 0 ]]; then
    popd >/dev/null
    [[ $rc -eq 124 ]] || true
    tail -n 120 "$OUTDIR/sniper.log" || true
    exit "$rc"
  fi
else
  "${LAUNCH[@]}" >"$OUTDIR/sniper.log" 2>&1
fi

popd >/dev/null

# Fail on SIFT assertions
if [[ "$FAIL_ON_SIFT_ASSERT" == "1" ]]; then
  if grep -qE 'zfstream\.cc:|sift_reader\.cc:|SIFT\].*Assertion' "$OUTDIR/sniper.log"; then
    echo "[ERR] Detected SIFT assertion(s)" | tee -a "$OUTDIR/sniper.log"
    exit 94
  fi
fi

# Generate sim.out if needed
if [[ ! -s "$OUTDIR/sim.out" && -s "$OUTDIR/sim.stats.sqlite3" ]]; then
  echo "[WARN] sim.out missing; generating from sqlite" | tee -a "$OUTDIR/sniper.log"
  if [[ -x "$SNIPER_HOME/tools/dumpstats.py" ]]; then
    python3 "$SNIPER_HOME/tools/dumpstats.py" -d "$OUTDIR" > "$OUTDIR/sim.out" 2>>"$OUTDIR/sniper.log" || true
  fi
fi

if [[ ! -s "$OUTDIR/sim.out" && ! -s "$OUTDIR/sim.stats.sqlite3" ]]; then
  echo "[ERR] Neither sim.out nor sim.stats.sqlite3 present" | tee -a "$OUTDIR/sniper.log"
  tail -n 120 "$OUTDIR/sniper.log" || true
  exit 92
fi

# Sanity: fail if no memory events
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

echo "[OK] SPEC $BENCH | ${L3_MB}MB | $VARIANT | $TECH -> $OUTDIR"
