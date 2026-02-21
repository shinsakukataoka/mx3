#!/usr/bin/env bash
set -euo pipefail

: "${SNIPER_HOME:?Need SNIPER_HOME set}"
: "${ROOT:?Need ROOT set to a calibration outroot (directory that contains many run dirs)}"

# mcpat.py expects mcpat lib in LD_LIBRARY_PATH
export LD_LIBRARY_PATH="$SNIPER_HOME/mcpat:${LD_LIBRARY_PATH:-}"

OUT="${OUT:-$ROOT/oracle_points.csv}"
echo "run_dir,bench,size_mb,f_ghz,U_sum,P_total_W,P_llc_leak_W,x_fU,y_PminusLLC" > "$OUT"

# Find unique run dirs by locating run.yaml
find "$ROOT" -name run.yaml -printf '%h\n' | sort -u | while read -r d; do
  cd "$d" || continue

  [[ -f sim.stats.sqlite3 ]] || { echo "[SKIP no-sqlite] $d"; continue; }
  [[ -f cmd.info ]] || { echo "[SKIP no-cmd.info] $d"; continue; }

  # (1) McPAT oracle (cache output table)
  if [[ ! -f mcpat_table.txt ]]; then
    python3 "$SNIPER_HOME/tools/mcpat.py" -d . -t total -o mcpat_total > /dev/null 2>&1 \
      && python3 "$SNIPER_HOME/tools/mcpat.py" -d . -t total -o mcpat_total > mcpat_table.txt 2>/dev/null \
      || { echo "[FAIL mcpat] $d"; continue; }
  fi

  P_TOTAL_W="$(awk '$1=="total"{print $2; exit}' mcpat_table.txt)"
  [[ -n "${P_TOTAL_W:-}" ]] || { echo "[FAIL parse total] $d"; continue; }

  # (2) Parse frequency + LLC leak power from cmd.info
  # cmd.info contains quoted args like: -g perf_model/core/frequency=2.66
  F_GHZ="$(grep -oE 'perf_model/core/frequency=[0-9]+(\.[0-9]+)?' cmd.info | head -1 | cut -d= -f2 || true)"
  LEAK_MW="$(grep -oE 'perf_model/l3_cache/llc/leak_power_mW=[0-9]+(\.[0-9]+)?' cmd.info | head -1 | cut -d= -f2 || true)"

  if [[ -z "${F_GHZ:-}" ]]; then
    echo "[FAIL parse freq from cmd.info] $d"
    continue
  fi
  if [[ -z "${LEAK_MW:-}" ]]; then
    echo "[FAIL parse leak_power_mW from cmd.info] $d"
    continue
  fi

  P_LLC_LEAK_W="$(python3 - <<PY
print(f"{float('$LEAK_MW')/1000.0:.9f}")
PY
)"

  # (3) Utilization sum from sqlite via sniper_lib
  U_SUM="$(python3 - <<'PY'
import os, sys
sniper_home=os.environ["SNIPER_HOME"]
sys.path.insert(0, os.path.join(sniper_home,"tools"))
import sniper_lib
r=sniper_lib.get_results(resultsdir=".", partial=None)
res=r.get("results", {})

elapsed=res.get("performance_model.elapsed_time")
idle=res.get("performance_model.idle_elapsed_time")

def clamp(x): return max(0.0, min(1.0, x))
U=0.0
if elapsed and idle and len(elapsed)==len(idle):
    for e,i in zip(elapsed,idle):
        e=float(e); i=float(i)
        U += 0.0 if e<=0 else clamp((e-i)/e)
else:
    instr=res.get("performance_model.instruction_count") or []
    U=float(sum(1 for x in instr if float(x)>0.0))
print(f"{U:.6f}")
PY
)"

  # (4) Compute x and y
  X="$(python3 - <<PY
print(f"{float('$F_GHZ')*float('$U_SUM'):.6f}")
PY
)"
  Y="$(python3 - <<PY
print(f"{float('$P_TOTAL_W')-float('$P_LLC_LEAK_W'):.6f}")
PY
)"

  # (5) bench + size from run.yaml
  BENCH="$(awk '/^[[:space:]]*bench:/ {gsub(/"/,"",$2); print $2; exit}' run.yaml)"
  SIZE_KB="$(awk '/^[[:space:]]*l3_size_kb:/ {print $2; exit}' run.yaml)"
  if [[ -z "${BENCH:-}" || -z "${SIZE_KB:-}" ]]; then
    echo "[FAIL parse bench/size from run.yaml] $d"
    continue
  fi
  SIZE_MB="$(python3 - <<PY
print(int(int('$SIZE_KB')/1024))
PY
)"

  echo "$d,$BENCH,$SIZE_MB,$F_GHZ,$U_SUM,$P_TOTAL_W,$P_LLC_LEAK_W,$X,$Y" >> "$OUT"
  echo "[OK] $d"
done

echo "Wrote: $OUT"