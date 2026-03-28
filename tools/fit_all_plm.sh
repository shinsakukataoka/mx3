#!/usr/bin/env bash
# fit_all_plm.sh — Fit PLM models for all (combo × capacity) pairs.
#
# Generates 18 cal.sh files:
#   6 combos (n1, n4, n8, n1n4, n4n8, n1n4n8) × 3 capacities (16, 32, 128 MB)
#
# USAGE
# -----
#   bash mx3/tools/fit_all_plm.sh \
#     --calib-dir repro/calibration/plm_calib_sunnycove \
#     --sniper-home ~/src/sniper \
#     --out-dir repro/calibration/models
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
FIT="$REPO_ROOT/mx3/tools/mcpat_plm_fit.py"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
CALIB_DIR=""
SNIPER_HOME=""
OUT_DIR=""
UARCH=sunnycove

# Clean workload lists for filtering n=4 and n=8
N4_CLEAN=(
  "502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r"
  "505.mcf_r+500.perlbench_r+648.exchange2_s+649.fotonik3d_s"
  "505.mcf_r+505.mcf_r+502.gcc_r+502.gcc_r"
  "505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r"
  "523.xalancbmk_r+523.xalancbmk_r+502.gcc_r+502.gcc_r"
)
N8_CLEAN=(
  "502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r"
  "505.mcf_r+505.mcf_r+500.perlbench_r+500.perlbench_r+648.exchange2_s+648.exchange2_s+649.fotonik3d_s+649.fotonik3d_s"
  "505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r"
  "505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r"
  "523.xalancbmk_r+523.xalancbmk_r+523.xalancbmk_r+523.xalancbmk_r+502.gcc_r+502.gcc_r+502.gcc_r+502.gcc_r"
)

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --calib-dir) CALIB_DIR="$2"; shift 2 ;;
    --sniper-home) SNIPER_HOME="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    --uarch) UARCH="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 --calib-dir <path> --sniper-home <path> --out-dir <path> [--uarch sunnycove]"
      echo ""
      echo "Fits 18 PLM models (6 combos × 3 capacities) from calibration oracle data."
      echo ""
      echo "  --calib-dir    Calibration run directory (contains runs/oracle_points.csv)"
      echo "  --sniper-home  Path to Sniper installation"
      echo "  --out-dir      Output directory for .cal.sh model files"
      echo "  --uarch        Microarchitecture label (default: sunnycove)"
      exit 0 ;;
    *) echo "[ERR] Unknown arg: $1"; exit 1 ;;
  esac
done

[[ -n "$CALIB_DIR" ]] || { echo "[ERR] --calib-dir required" >&2; exit 1; }
[[ -n "$SNIPER_HOME" ]] || { echo "[ERR] --sniper-home required" >&2; exit 1; }
[[ -n "$OUT_DIR" ]] || { echo "[ERR] --out-dir required" >&2; exit 1; }

CALIB_DIR="$(readlink -f "$CALIB_DIR")"
SNIPER_HOME="$(readlink -f "$SNIPER_HOME")"
MASTER_CSV="$CALIB_DIR/runs/oracle_points.csv"

[[ -f "$MASTER_CSV" ]] || { echo "[ERR] Oracle CSV not found: $MASTER_CSV" >&2; exit 1; }

mkdir -p "$OUT_DIR"

SUMMARY="$OUT_DIR/fit_summary.txt"
> "$SUMMARY"

# ---------------------------------------------------------------------------
# Split master CSV by (ncores, capacity)
#   ncores inferred from run_dir path: /n1/ → 1, /n4/ → 4, /n8/ → 8
#   capacity from size_mb column
# ---------------------------------------------------------------------------
TMPDIR_SPLIT="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_SPLIT"' EXIT

echo "[1/3] Splitting oracle CSV by (ncores, capacity) ..."

HEADER="$(head -1 "$MASTER_CSV")"

for NC in 1 4 8; do
  for L3 in 16 32 128; do
    OUTCSV="$TMPDIR_SPLIT/oracle_n${NC}_${L3}M.csv"
    echo "$HEADER" > "$OUTCSV"
    # Match /nX/ in run_dir (column 1) and size_mb==L3 (column 3)
    awk -F, -v nc="$NC" -v l3="$L3" \
      'NR>1 && $1 ~ "/n"nc"/" && $3==l3' "$MASTER_CSV" >> "$OUTCSV"

    n_pts=$(($(wc -l < "$OUTCSV") - 1))
    echo "  n=${NC}, ${L3}MB: ${n_pts} points"
  done
done

# ---------------------------------------------------------------------------
# Filter n=4/n=8 to clean workloads
# ---------------------------------------------------------------------------
echo ""
echo "[2/3] Filtering to clean workloads for n=4/n=8 ..."

filter_csv() {
  local src="$1" dst="$2"
  shift 2
  local benches=("$@")
  head -1 "$src" > "$dst"
  for b in "${benches[@]}"; do
    # bench is column 2 in the CSV
    awk -F, -v b="$b" 'NR>1 && $2==b' "$src" >> "$dst"
  done
}

for L3 in 16 32 128; do
  # n=1: no filtering needed
  cp "$TMPDIR_SPLIT/oracle_n1_${L3}M.csv" "$TMPDIR_SPLIT/oracle_n1_${L3}M_clean.csv"

  # n=4: filter to clean workloads
  filter_csv "$TMPDIR_SPLIT/oracle_n4_${L3}M.csv" \
             "$TMPDIR_SPLIT/oracle_n4_${L3}M_clean.csv" \
             "${N4_CLEAN[@]}"

  # n=8: filter to clean workloads
  filter_csv "$TMPDIR_SPLIT/oracle_n8_${L3}M.csv" \
             "$TMPDIR_SPLIT/oracle_n8_${L3}M_clean.csv" \
             "${N8_CLEAN[@]}"

  for NC in 1 4 8; do
    n_pts=$(($(wc -l < "$TMPDIR_SPLIT/oracle_n${NC}_${L3}M_clean.csv") - 1))
    echo "  n=${NC}, ${L3}MB (clean): ${n_pts} points"
  done
done

# ---------------------------------------------------------------------------
# Fit all combos
# ---------------------------------------------------------------------------
echo ""
echo "[3/3] Fitting 18 PLM models ..."
echo ""

COMBOS=( "n1" "n4" "n8" "n1n4" "n4n8" "n1n4n8" )
CAPACITIES=( 16 32 128 )
FIT_COUNT=0

for COMBO in "${COMBOS[@]}"; do
  for L3 in "${CAPACITIES[@]}"; do
    OUT_SH="$OUT_DIR/plm_${UARCH}_${COMBO}_cal_${L3}M.sh"
    LOG="/tmp/plm_fit_${COMBO}_${L3}M.log"

    # Determine CSV args based on combo
    case "$COMBO" in
      n1)
        CSV_ARGS=( --csv "$TMPDIR_SPLIT/oracle_n1_${L3}M_clean.csv" --calib-ncores 1 )
        ;;
      n4)
        CSV_ARGS=( --csv "$TMPDIR_SPLIT/oracle_n4_${L3}M_clean.csv" --calib-ncores 4 )
        ;;
      n8)
        CSV_ARGS=( --csv "$TMPDIR_SPLIT/oracle_n8_${L3}M_clean.csv" --calib-ncores 8 )
        ;;
      n1n4)
        CSV_ARGS=( --csv "$TMPDIR_SPLIT/oracle_n1_${L3}M_clean.csv"
                   --extra-csv "$TMPDIR_SPLIT/oracle_n4_${L3}M_clean.csv"
                   --calib-ncores 1 )
        ;;
      n4n8)
        CSV_ARGS=( --csv "$TMPDIR_SPLIT/oracle_n4_${L3}M_clean.csv"
                   --extra-csv "$TMPDIR_SPLIT/oracle_n8_${L3}M_clean.csv"
                   --calib-ncores 4 )
        ;;
      n1n4n8)
        CSV_ARGS=( --csv "$TMPDIR_SPLIT/oracle_n1_${L3}M_clean.csv"
                   --extra-csv "$TMPDIR_SPLIT/oracle_n4_${L3}M_clean.csv"
                               "$TMPDIR_SPLIT/oracle_n8_${L3}M_clean.csv"
                   --calib-ncores 1 )
        ;;
    esac

    echo "=============================================="
    echo "  Fitting: ${COMBO} @ ${L3}MB"
    echo "=============================================="

    python3 "$FIT" \
      "${CSV_ARGS[@]}" \
      --sniper-home "$SNIPER_HOME" \
      --uarch "$UARCH" \
      --out "$OUT_SH" \
      --skip-mcpat \
      2>&1 | tee "$LOG"

    # Extract summary stats from log
    echo "--- ${COMBO} ${L3}MB ---" >> "$SUMMARY"
    grep -iE 'MAE|MAPE|[Bb]ias|points|R²|Wrote' "$LOG" >> "$SUMMARY" 2>/dev/null || true
    echo "" >> "$SUMMARY"

    (( FIT_COUNT++ )) || true
    echo ""
  done
done

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "=============================================="
echo "  All $FIT_COUNT models fitted."
echo "=============================================="
echo ""
echo "Model files:"
ls -la "$OUT_DIR"/plm_*.sh 2>/dev/null || echo "  (none found)"
echo ""
echo "Summary: $SUMMARY"
cat "$SUMMARY"
