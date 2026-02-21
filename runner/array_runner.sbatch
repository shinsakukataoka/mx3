#!/usr/bin/env bash
#SBATCH --partition=cpu-dense-preempt-q
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=24:00:00
#SBATCH --output=slurm/%x-%A_%a.out
#SBATCH --error=slurm/%x-%A_%a.err

set -euo pipefail
: "${RUN_DIR:?RUN_DIR must be set}"

RUN_DIR="$(readlink -f "$RUN_DIR")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"

# Load site env
if [[ -f "$RUN_DIR/env.sh" ]]; then
  # shellcheck source=/dev/null
  source "$RUN_DIR/env.sh"
fi

JOBS_FILE="$RUN_DIR/jobs.txt"
[[ -f "$JOBS_FILE" ]] || { echo "[ERR] missing jobs.txt: $JOBS_FILE" >&2; exit 2; }

mapfile -t JOBS < <(grep -v '^[[:space:]]*$' "$JOBS_FILE" || true)
NUM_JOBS="${#JOBS[@]}"
echo "[INFO] jobs=$NUM_JOBS"

IDX="${SLURM_ARRAY_TASK_ID:-0}"
if [[ "$IDX" -ge "$NUM_JOBS" ]]; then
  echo "[WARN] task $IDX >= $NUM_JOBS ; exit"
  exit 0
fi

JOB_LINE="${JOBS[$IDX]}"
echo "[INFO] task=$IDX line=$JOB_LINE"

# Export tokens safely (no eval)
for kv in $JOB_LINE; do
  if [[ "$kv" =~ ^[A-Za-z_][A-Za-z0-9_]*=.*$ ]]; then
    export "$kv"
  else
    echo "[ERR] bad token in jobs.txt: $kv" >&2
    exit 3
  fi
done

: "${JOB_OUTDIR:?JOB_OUTDIR must be set}"
mkdir -p "$JOB_OUTDIR" "$RUN_DIR/slurm" 2>/dev/null || true

cd "$REPO_ROOT"
bash "$REPO_ROOT/mx2/runner/dispatch.sh"
