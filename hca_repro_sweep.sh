#!/usr/bin/env bash
# hca_repro_sweep.sh — HCA reproduction study (308 jobs).
#
# Plans all HCA simulation stages under repro/hca/.
# Uses device configs from config/devices/sunnycove/.
#
# USAGE
# -----
#   bash mx3/hca_repro_sweep.sh                # plan all stages
#   mx3/bin/mx submit repro/hca/<stage>        # submit a stage
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
MX="$REPO_ROOT/mx3/bin/mx"
DEV_DIR="$REPO_ROOT/mx3/config/devices/sunnycove"
OUT_BASE="$REPO_ROOT/repro"

UARCH="sunnycove"
BASE_FREQ_GHZ="2.2"
CORES=1
ROI_M=1000
WARMUP_M=200

ALL_BENCHES="500.perlbench_r,502.gcc_r,505.mcf_r,520.omnetpp_r,523.xalancbmk_r,531.deepsjeng_r,541.leela_r,557.xz_r,648.exchange2_s,649.fotonik3d_s"
TOP4_BENCHES="500.perlbench_r,505.mcf_r,520.omnetpp_r,531.deepsjeng_r"
LAT_BENCHES="505.mcf_r,520.omnetpp_r"

L3_ALL="16,32,128"

STUDY="hca_sunnycove"
TOTAL_JOBS=0

# Common plan-hca args
common_args() {
  echo "--out $OUT_BASE --uarch $UARCH --cores $CORES"
  echo "--roi-m $ROI_M --warmup-m $WARMUP_M"
  echo "--base-freq-ghz $BASE_FREQ_GHZ"
  echo "--devices-dir $DEV_DIR"
}

plan() {
  local run_id="$1" sram="$2" mram="$3" tag="$4" variants="$5" benches="$6" l3="$7"
  shift 7

  local run_dir="$OUT_BASE/hca/$run_id"

  "$MX" plan-hca \
    --out "$OUT_BASE" \
    --run-id "$run_id" \
    --sram-tech "$sram" --mram-tech "$mram" --tech-tag "$tag" \
    --benches "$benches" --l3 "$l3" \
    --uarch "$UARCH" --cores "$CORES" \
    --roi-m "$ROI_M" --warmup-m "$WARMUP_M" \
    --base-freq-ghz "$BASE_FREQ_GHZ" \
    --devices-dir "$DEV_DIR" \
    --variants "$variants" \
    "$@"

  local n
  n=$(wc -l < "$run_dir/jobs.txt")
  TOTAL_JOBS=$(( TOTAL_JOBS + n ))
  echo "  -> $n jobs"
}

# ===============================================================
# STAGE 1: Baselines — SRAM7 (30 jobs)
# ===============================================================
echo ""
echo "=============================="
echo " Stage 1: Baselines (SRAM7)"
echo "=============================="
plan "${STUDY}/1_baselines" sram7 mram14 sram7 \
  "baseline_sram_only" "$ALL_BENCHES" "$L3_ALL"

# ===============================================================
# STAGE 2: Cross-Node Comparison (120 jobs)
#   SRAM14, SRAM32 → baseline_sram_only
#   MRAM14, MRAM32 → baseline_mram_only
# ===============================================================
echo ""
echo "=============================="
echo " Stage 2: Cross-Node"
echo "=============================="
plan "${STUDY}/2_cross_node/sram14" sram14 mram14 sram14 \
  "baseline_sram_only" "$ALL_BENCHES" "$L3_ALL"
plan "${STUDY}/2_cross_node/sram32" sram32 mram14 sram32 \
  "baseline_sram_only" "$ALL_BENCHES" "$L3_ALL"
plan "${STUDY}/2_cross_node/mram14" sram14 mram14 mram14 \
  "baseline_mram_only" "$ALL_BENCHES" "$L3_ALL"
plan "${STUDY}/2_cross_node/mram32" sram14 mram32 mram32 \
  "baseline_mram_only" "$ALL_BENCHES" "$L3_ALL"

# ===============================================================
# STAGE 3: Static HCA (90 jobs)
#   SRAM14/MRAM14, s4/s8/s12, unrestricted
# ===============================================================
echo ""
echo "=============================="
echo " Stage 3: Static HCA"
echo "=============================="
plan "${STUDY}/3_static_hca" sram14 mram14 sram14_mram14 \
  "noparity_s4_fillmram,noparity_s8_fillmram,noparity_s12_fillmram" \
  "$ALL_BENCHES" "$L3_ALL"

# ===============================================================
# STAGE 4: Migration, unrestricted (8 jobs)
#   SRAM14/MRAM14, s4, 16MB only, top4
# ===============================================================
echo ""
echo "=============================="
echo " Stage 4: Migration (unrestricted)"
echo "=============================="
plan "${STUDY}/4_migration_unrestricted" sram14 mram14 sram14_mram14 \
  "noparity_s4_fillmram_p4_c32,noparity_s4_fillmram_p1_c0" \
  "$TOP4_BENCHES" "16"

# ===============================================================
# STAGE 5: Restricted fill, static (12 jobs)
#   SRAM14/MRAM14, s4_rf, 16/32/128MB, top4
# ===============================================================
echo ""
echo "=============================="
echo " Stage 5: Restricted fill (static)"
echo "=============================="
plan "${STUDY}/5_restricted_static" sram14 mram14 sram14_mram14 \
  "noparity_s4_fillmram_rf" \
  "$TOP4_BENCHES" "$L3_ALL"

# ===============================================================
# STAGE 6: Restricted fill, migration (24 jobs)
#   SRAM14/MRAM14, s4_rf, 16/32/128MB, top4
# ===============================================================
echo ""
echo "=============================="
echo " Stage 6: Restricted fill (migration)"
echo "=============================="
plan "${STUDY}/6_restricted_migration" sram14 mram14 sram14_mram14 \
  "noparity_s4_fillmram_rf_p4_c32,noparity_s4_fillmram_rf_p1_c0" \
  "$TOP4_BENCHES" "$L3_ALL"

# ===============================================================
# STAGE 7: Read-Latency Sweep (24 jobs)
#   SRAM14/MRAM14, s4 static, MRAM rd 2x/3x/4x/5x
#   16/32/128MB, mcf + omnetpp
# ===============================================================
echo ""
echo "=============================="
echo " Stage 7: Read-Latency Sweep"
echo "=============================="
for MULT in 2 3 4 5; do
  plan "${STUDY}/7_read_latency_sweep/rd_${MULT}x" sram14 mram14 "sram14_mram14_rd${MULT}x" \
    "noparity_s4_fillmram" \
    "$LAT_BENCHES" "$L3_ALL" \
    --mram-rd-mult "$MULT"
done

# Stage 7b: Baseline MRAM-only at each latency scale (for normalization)
echo ""
echo "=============================="
echo " Stage 7b: MRAM Baselines (latency sweep)"
echo "=============================="
for MULT in 2 3 4 5; do
  plan "${STUDY}/7_read_latency_sweep/mram_base_rd_${MULT}x" sram14 mram14 "mram14_rd${MULT}x" \
    "baseline_mram_only" \
    "$LAT_BENCHES" "$L3_ALL" \
    --mram-rd-mult "$MULT"
done

# ===============================================================
# Summary
# ===============================================================
echo ""
echo "=============================================="
echo " HCA Reproduction Study — Complete"
echo "=============================================="
echo " Uarch:      ${UARCH}"
echo " Base freq:  ${BASE_FREQ_GHZ} GHz"
echo " Devices:    ${DEV_DIR}"
echo " Total jobs: ${TOTAL_JOBS}"
echo " Output:     ${OUT_BASE}/hca/${STUDY}/"
echo "=============================================="
echo ""
echo "Submit all stages:"
echo "  for d in ${OUT_BASE}/hca/${STUDY}/*/; do"
echo "    ${MX} submit \"\$d\""
echo "  done"
