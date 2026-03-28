#!/usr/bin/env bash
set -euo pipefail

# Expected env (HCA):
#   L3_MB
#   SRAM_TECH, MRAM_TECH   (recommended; enables explicit hybrid pairing)
# Optional:
#   TECH                  (fallback for legacy runs; loader will use --tech)
#   WB_PJ

# Provides:
#   hca_flags_for_variant <label>   -> prints -g flags
#   hca_parse_variant <label>       -> sets: HCA_SRAM_WAYS, HCA_FILL_TO, HCA_MIG_ENABLED, HCA_PROMOTE, HCA_COOLDOWN

HCA_ASSOC=16

hca_set_nvsim_params() {
  : "${L3_MB:?L3_MB must be set (2|32|128)}"

  WB_PJ="${WB_PJ:-0}"

  local repo="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
  local loader="$repo/mx3/engine/load_device_params.py"

  if [[ -n "${SRAM_TECH:-}" || -n "${MRAM_TECH:-}" ]]; then
    : "${SRAM_TECH:?SRAM_TECH must be set when using explicit HCA pairing}"
    : "${MRAM_TECH:?MRAM_TECH must be set when using explicit HCA pairing}"
    local _dev_args=( --sram-tech "$SRAM_TECH" --mram-tech "$MRAM_TECH" --l3 "$L3_MB" )
    [[ -n "${DEVICES_DIR:-}" ]] && _dev_args+=( --devices-dir "$DEVICES_DIR" )
    # shellcheck disable=SC1090
    source <(python3 "$loader" "${_dev_args[@]}")
  else
    : "${TECH:?TECH must be set (or set SRAM_TECH+MRAM_TECH)}"
    local _dev_args=( --tech "$TECH" --l3 "$L3_MB" )
    [[ -n "${DEVICES_DIR:-}" ]] && _dev_args+=( --devices-dir "$DEVICES_DIR" )
    # legacy: single TECH file
    # shellcheck disable=SC1090
    source <(python3 "$loader" "${_dev_args[@]}")
  fi

  # Runtime MRAM read-latency multiplier (avoids per-multiplier YAML files)
  if [[ -n "${MRAM_RD_MULT:-}" && "${MRAM_RD_MULT}" != "1" ]]; then
    MRAM_RD_CYC=$(awk "BEGIN{printf \"%d\", ${MRAM_RD_CYC} * ${MRAM_RD_MULT}}")
  fi
}

hca_leak_mw_for_sram_ways() {
  # Weighted by way fraction
  local sw="$1"
  awk -v sw="$sw" -v A="$HCA_ASSOC" -v ls="$SRAM_LEAK_MW" -v lm="$MRAM_LEAK_MW" \
    'BEGIN{printf "%.6f", (ls*(sw/A) + lm*(1 - sw/A))}'
}

hca_tech_common_flags() {
  cat <<EOF
-g perf_model/l3_cache/cache_size=$(( L3_MB * 1024 ))
-g perf_model/l3_cache/associativity=${HCA_ASSOC}
-g perf_model/l3_cache/perfect=false
-g perf_model/l3_cache/prefetcher=none
-g perf_model/l3_cache/hybrid/enabled=true
-g perf_model/l3_cache/hybrid/line_map/mode=set-parity
-g perf_model/l3_cache/hybrid/line_map/set_parity=even_is_mram
-g perf_model/l3_cache/hybrid/sram/read_hit_latency_cycles=${SRAM_RD_CYC}
-g perf_model/l3_cache/hybrid/sram/write_hit_latency_cycles=${SRAM_WR_CYC}
-g perf_model/l3_cache/hybrid/mram/read_hit_latency_cycles=${MRAM_RD_CYC}
-g perf_model/l3_cache/hybrid/mram/write_hit_latency_cycles=${MRAM_WR_CYC}
-g perf_model/l3_cache/hybrid/sram/read_hit_energy_pJ=${SRAM_R_PJ}
-g perf_model/l3_cache/hybrid/sram/write_hit_energy_pJ=${SRAM_W_PJ}
-g perf_model/l3_cache/hybrid/mram/read_hit_energy_pJ=${MRAM_R_PJ}
-g perf_model/l3_cache/hybrid/mram/write_hit_energy_pJ=${MRAM_W_PJ}
-g perf_model/l3_cache/llc/writeback_energy_pJ=${WB_PJ}
EOF
}

# Globals set by hca_parse_variant
HCA_SRAM_WAYS=""
HCA_FILL_TO=""
HCA_MIG_ENABLED="false"
HCA_PROMOTE=""
HCA_COOLDOWN=""
HCA_RESTRICT_FILL_WAYS="true"  # false for noparity_* variants

hca_parse_variant() {
  local v="$1"
  HCA_SRAM_WAYS=""
  HCA_FILL_TO=""
  HCA_MIG_ENABLED="false"
  HCA_PROMOTE=""
  HCA_COOLDOWN=""

  if [[ "$v" == "baseline_sram_only" ]]; then
    HCA_SRAM_WAYS=16; HCA_FILL_TO="sram"; HCA_MIG_ENABLED="false"
    return
  fi
  if [[ "$v" == "baseline_mram_only" ]]; then
    HCA_SRAM_WAYS=0; HCA_FILL_TO="mram"; HCA_MIG_ENABLED="false"
    return
  fi
  if [[ "$v" =~ ^baseline_([0-9]+)_sram$ ]]; then
    HCA_SRAM_WAYS="${BASH_REMATCH[1]}"; HCA_FILL_TO="sram"; HCA_MIG_ENABLED="false"
    return
  fi
  if [[ "$v" =~ ^grid_s([0-9]+)_fill(sram|mram)$ ]]; then
    HCA_SRAM_WAYS="${BASH_REMATCH[1]}"; HCA_FILL_TO="${BASH_REMATCH[2]}"; HCA_MIG_ENABLED="false"
    HCA_RESTRICT_FILL_WAYS="true"
    return
  fi
  if [[ "$v" =~ ^mig_s([0-9]+)_fill(sram|mram)_p([0-9]+)_c([0-9]+)$ ]]; then
    HCA_SRAM_WAYS="${BASH_REMATCH[1]}"
    HCA_FILL_TO="${BASH_REMATCH[2]}"
    HCA_MIG_ENABLED="true"
    HCA_PROMOTE="${BASH_REMATCH[3]}"
    HCA_COOLDOWN="${BASH_REMATCH[4]}"
    HCA_RESTRICT_FILL_WAYS="true"
    return
  fi
  # Canonical/standard HCA: unrestricted way fills (full associativity).
  # Lines are tagged SRAM/MRAM by their physical way index after insertion.
  # set-parity line_map is disabled; uses global fill_to as the preferred
  # target but LRU may choose any way if the preferred ways are occupied.
  if [[ "$v" =~ ^noparity_s([0-9]+)_fill(sram|mram)$ ]]; then
    HCA_SRAM_WAYS="${BASH_REMATCH[1]}"; HCA_FILL_TO="${BASH_REMATCH[2]}"; HCA_MIG_ENABLED="false"
    HCA_RESTRICT_FILL_WAYS="false"
    return
  fi
  if [[ "$v" =~ ^noparity_s([0-9]+)_fill(sram|mram)_p([0-9]+)_c([0-9]+)$ ]]; then
    HCA_SRAM_WAYS="${BASH_REMATCH[1]}"
    HCA_FILL_TO="${BASH_REMATCH[2]}"
    HCA_MIG_ENABLED="true"
    HCA_PROMOTE="${BASH_REMATCH[3]}"
    HCA_COOLDOWN="${BASH_REMATCH[4]}"
    HCA_RESTRICT_FILL_WAYS="false"
    return
  fi
  # Canonical HCA with restricted fills: no set-parity, but fills steered to
  # target tech ways.  Migration is the only path to populate SRAM ways.
  if [[ "$v" =~ ^noparity_s([0-9]+)_fill(sram|mram)_rf$ ]]; then
    HCA_SRAM_WAYS="${BASH_REMATCH[1]}"; HCA_FILL_TO="${BASH_REMATCH[2]}"; HCA_MIG_ENABLED="false"
    HCA_RESTRICT_FILL_WAYS="true"
    return
  fi
  if [[ "$v" =~ ^noparity_s([0-9]+)_fill(sram|mram)_rf_p([0-9]+)_c([0-9]+)$ ]]; then
    HCA_SRAM_WAYS="${BASH_REMATCH[1]}"
    HCA_FILL_TO="${BASH_REMATCH[2]}"
    HCA_MIG_ENABLED="true"
    HCA_PROMOTE="${BASH_REMATCH[3]}"
    HCA_COOLDOWN="${BASH_REMATCH[4]}"
    HCA_RESTRICT_FILL_WAYS="true"
    return
  fi

  echo "[ERR] Unknown HCA variant: $v" >&2
  exit 10
}

hca_flags_for_variant() {
  local v="$1"
  hca_set_nvsim_params
  hca_parse_variant "$v"

  local sw="$HCA_SRAM_WAYS"
  local leak_mw
  leak_mw="$(hca_leak_mw_for_sram_ways "$sw")"

  # base flags + per-variant knobs
  cat <<EOF
$(hca_tech_common_flags)
-g perf_model/l3_cache/hybrid/sram_ways=${sw}
-g perf_model/l3_cache/hybrid/fill_to=${HCA_FILL_TO}
-g perf_model/l3_cache/hybrid/migration/enabled=${HCA_MIG_ENABLED}
-g perf_model/l3_cache/hybrid/restrict_fill_ways=${HCA_RESTRICT_FILL_WAYS}
EOF

  # noparity variants: override line_map to none (no set-parity split)
  if [[ "$v" == noparity_* ]]; then
    echo "-g perf_model/l3_cache/hybrid/line_map/mode=none"
  fi

  if [[ "$HCA_MIG_ENABLED" == "true" ]]; then
    cat <<EOF
-g perf_model/l3_cache/hybrid/migration/promote_after_hits=${HCA_PROMOTE}
-g perf_model/l3_cache/hybrid/migration/cooldown_hits=${HCA_COOLDOWN}
EOF
  fi

  # leakage for this hybrid composition
  echo "-g perf_model/l3_cache/llc/leak_power_mW=${leak_mw}"
}