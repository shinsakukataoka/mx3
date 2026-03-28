#!/usr/bin/env bash
set -euo pipefail

: "${CAMPAIGN:?CAMPAIGN must be set}"
: "${OUTDIR:?OUTDIR must be set}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"

STATUS_FILE="$OUTDIR/mx3_status.yaml"
mkdir -p "$OUTDIR"

ts_start="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat >"$STATUS_FILE" <<YAML
status: running
campaign: ${CAMPAIGN}
start_utc: ${ts_start}
reason: ""
exit_code: ""
YAML

rc=0
set +e
case "$CAMPAIGN" in
  spec)       bash "$REPO_ROOT/mx3/engine/run_spec.sh";       rc=$? ;;
  hca)        bash "$REPO_ROOT/mx3/engine/run_hca.sh";        rc=$? ;;
  traces)     bash "$REPO_ROOT/mx3/engine/run_traces.sh";     rc=$? ;;
  microbench)     bash "$REPO_ROOT/mx3/engine/run_microbench.sh";     rc=$? ;;
  kernel)         bash "$REPO_ROOT/mx3/engine/run_kernel.sh";         rc=$? ;;
  vf_sensitivity) bash "$REPO_ROOT/mx3/engine/run_vf_sensitivity.sh"; rc=$? ;;
  plm_calib)     bash "$REPO_ROOT/mx3/engine/run_plm_calib.sh";     rc=$? ;;
  *)
    echo "[ERR] unknown CAMPAIGN=$CAMPAIGN" >&2
    rc=2
    ;;
esac
set -e

ts_end="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

reason="unknown"
if [[ "$rc" -eq 0 ]]; then
  reason="ok"
else
  if [[ "$rc" -eq 124 ]]; then
    reason="timeout"
  elif [[ -f "$OUTDIR/sniper.log" ]]; then
    if grep -qE 'zfstream\.cc:|sift_reader\.cc:|SIFT\].*Assertion' "$OUTDIR/sniper.log"; then
      reason="sift_assertion"
    elif grep -q 'Sanity check failed: Cache L1-D accesses are zero' "$OUTDIR/sniper.log"; then
      reason="no_memory_events"
    elif grep -q 'Neither sim.out nor sim.stats.sqlite3 present' "$OUTDIR/sniper.log"; then
      reason="missing_outputs"
    elif grep -q 'run-sniper failed' "$OUTDIR/sniper.log"; then
      reason="sniper_failed"
    fi
  fi
fi

status="failed"
[[ "$rc" -eq 0 ]] && status="done"

cat >"$STATUS_FILE" <<YAML
status: ${status}
campaign: ${CAMPAIGN}
start_utc: ${ts_start}
end_utc: ${ts_end}
reason: ${reason}
exit_code: ${rc}
YAML

exit "$rc"