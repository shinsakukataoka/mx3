#!/usr/bin/env bash
set_nvsim_params() {
  : "${TECH:?TECH must be set (mram14|mram32|sram7)}"
  : "${L3_MB:?L3_MB must be set (2|16|32|128)}"

  # WB_PJ stays in shell (not in YAML)
  WB_PJ="${WB_PJ:-0}"

  # REPO_ROOT is set by engine scripts; fallback to relative
  local repo="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
  local loader="$repo/mx3/engine/load_device_params.py"

  # Load SRAM_* and MRAM_* vars into this shell
  local _dev_args=( --tech "$TECH" --l3 "$L3_MB" )
  [[ -n "${DEVICES_DIR:-}" ]] && _dev_args+=( --devices-dir "$DEVICES_DIR" )
  # shellcheck disable=SC1090
  source <(python3 "$loader" "${_dev_args[@]}")

  # Runtime MRAM read-latency multiplier (avoids per-multiplier YAML files)
  if [[ -n "${MRAM_RD_MULT:-}" && "${MRAM_RD_MULT}" != "1" ]]; then
    MRAM_RD_CYC=$(awk "BEGIN{printf \"%d\", ${MRAM_RD_CYC} * ${MRAM_RD_MULT}}")
  fi

  # Runtime MRAM leakage gap fraction: shrinks sram-mram leakage gap.
  # 0.25 means new_gap = original_gap * 0.25 (most pessimistic for DVFS).
  if [[ -n "${MRAM_LEAK_GAP_FRAC:-}" ]]; then
    MRAM_LEAK_MW=$(awk -v sl="$SRAM_LEAK_MW" -v ml="$MRAM_LEAK_MW" -v f="$MRAM_LEAK_GAP_FRAC" \
      'BEGIN{printf "%.6f", sl - (sl - ml) * f}')
  fi
}

tech_common_flags() {
  cat <<EOF
-g perf_model/l3_cache/cache_size=$(( L3_MB * 1024 ))
-g perf_model/l3_cache/associativity=16
-g perf_model/l3_cache/perfect=false
-g perf_model/l3_cache/prefetcher=none
-g perf_model/l3_cache/hybrid/enabled=true
-g perf_model/l3_cache/hybrid/line_map/mode=none
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

dvfs_hpi_flags() {
  local per="$1"
  local cps="${SIM_N}"
  if [[ "${LC_SELECTIVE:-0}" == "1" ]]; then cps=1; fi
  cat <<EOF
-g perf_model/core/frequency=${BASE_FREQ_GHZ}
-g dvfs/type=simple
-g dvfs/transition_latency=2000
-g dvfs/simple/cores_per_socket=${cps}
-g core/hook_periodic_ins/ins_global=${per}
-g core/hook_periodic_ins/ins_per_core=0
EOF
}

# Parse lc_* label (with optional naive_/sram_ prefix)
LC_ENABLED=false
LC_NAIVE=false
LC_SRAM=false
CAP_W=""; STATIC_W=""; DYN_W=""; TF=""; HYS_W=""; FMAX_GHZ=""; STEP_GHZ=""; PERIOD_INS=""
parse_lc_variant() {
  LC_ENABLED=false; LC_NAIVE=false; LC_SRAM=false
  CAP_W=""; STATIC_W=""; DYN_W=""; TF=""; HYS_W=""; FMAX_GHZ=""; STEP_GHZ=""; PERIOD_INS=""
  local label="$1"
  if [[ "$label" == naive_* ]]; then
    LC_NAIVE=true
    label="${label#naive_}"
  elif [[ "$label" == sram_* ]]; then
    LC_SRAM=true
    label="${label#sram_}"
  fi
  # Strip optional sel_ or hca_sN_ prefix (selective / HCA DVFS labels)
  label="${label#sel_}"
  label="$(echo "$label" | sed 's/^hca_s[0-9]*_//')"
  if [[ "$label" =~ ^lc_c([0-9p]+)_s([0-9p]+)_d([0-9p]+)_tf([0-9p]+)_h([0-9p]+)_f([0-9p]+)_st([0-9p]+)_pi([0-9]+)$ ]]; then
    LC_ENABLED=true
    CAP_W="${BASH_REMATCH[1]//p/.}"
    STATIC_W="${BASH_REMATCH[2]//p/.}"
    DYN_W="${BASH_REMATCH[3]//p/.}"
    TF="${BASH_REMATCH[4]//p/.}"
    HYS_W="${BASH_REMATCH[5]//p/.}"
    FMAX_GHZ="${BASH_REMATCH[6]//p/.}"
    STEP_GHZ="${BASH_REMATCH[7]//p/.}"
    PERIOD_INS="${BASH_REMATCH[8]}"
  fi
}

flags_for_variant() {
  local var="$1"
  set_nvsim_params
  parse_lc_variant "$var"

  case "$var" in
    baseline_sram_only)
      cat <<EOF
$(dvfs_hpi_flags "${BASE_PERIODIC_INS}")
$(tech_common_flags)
-g perf_model/l3_cache/hybrid/sram_ways=16
-g perf_model/l3_cache/hybrid/fill_to=sram
-g perf_model/l3_cache/hybrid/migration/enabled=false
-g perf_model/l3_cache/llc/leak_power_mW=${SRAM_LEAK_MW}
-g lc/enabled=false
EOF
      ;;
    baseline_mram_only)
      cat <<EOF
$(dvfs_hpi_flags "${BASE_PERIODIC_INS}")
$(tech_common_flags)
-g perf_model/l3_cache/hybrid/sram_ways=0
-g perf_model/l3_cache/hybrid/fill_to=mram
-g perf_model/l3_cache/hybrid/migration/enabled=false
-g perf_model/l3_cache/llc/leak_power_mW=${MRAM_LEAK_MW}
-g lc/enabled=false
EOF
      ;;
    static_lift_f*)
      # Static frequency lift: MRAM cache at boosted freq, no DVFS
      local _fstr="${var#static_lift_f}"
      local _freq="${_fstr//p/.}"
      cat <<EOF
-g perf_model/core/frequency=${_freq}
-g dvfs/type=simple
-g dvfs/transition_latency=2000
-g dvfs/simple/cores_per_socket=${SIM_N}
-g core/hook_periodic_ins/ins_global=${BASE_PERIODIC_INS}
-g core/hook_periodic_ins/ins_per_core=0
$(tech_common_flags)
-g perf_model/l3_cache/hybrid/sram_ways=0
-g perf_model/l3_cache/hybrid/fill_to=mram
-g perf_model/l3_cache/hybrid/migration/enabled=false
-g perf_model/l3_cache/llc/leak_power_mW=${MRAM_LEAK_MW}
-g lc/enabled=false
EOF
      ;;
    *)
      if [[ "$LC_ENABLED" != "true" ]]; then
        echo "[ERR] Unknown variant: $var" >&2
        exit 10
      fi

      # HCA override: allow sram_ways and fill_to to be set via env for HCA+DVFS
      local _sw="${HCA_SRAM_WAYS:-0}"
      local _fillto="${HCA_FILL_TO:-mram}"
      local _mig="${HCA_MIG_ENABLED:-false}"
      local _line_map="${HCA_LINE_MAP_MODE:-none}"
      local _restrict="${HCA_RESTRICT_FILL_WAYS:-false}"

      # Compute blended leak for HCA (weighted by sram_ways / 16)
      local _leak_mw="$MRAM_LEAK_MW"
      if [[ "$_sw" -gt 0 ]]; then
        _leak_mw="$(awk -v sw="$_sw" -v A=16 -v ls="$SRAM_LEAK_MW" -v lm="$MRAM_LEAK_MW" \
          'BEGIN{printf "%.6f", (ls*(sw/A) + lm*(1 - sw/A))}')"
      fi

      # mW -> W for LC governor (use blended HCA leak when applicable)
      local mram_leak_w
      mram_leak_w="$(awk -v mw="$MRAM_LEAK_MW" 'BEGIN{printf "%.6f", mw/1000.0}')"
      local _llc_leak_w
      _llc_leak_w="$(awk -v mw="$_leak_mw" 'BEGIN{printf "%.6f", mw/1000.0}')"

      local sel_enabled="false"
      local sel_k="1"
      if [[ "${LC_SELECTIVE:-0}" == "1" ]]; then
        sel_enabled="true"
        sel_k="${LC_TOPK:-1}"
      fi

      cat <<EOF
$(tech_common_flags)
-g perf_model/l3_cache/hybrid/sram_ways=${_sw}
-g perf_model/l3_cache/hybrid/fill_to=${_fillto}
-g perf_model/l3_cache/hybrid/migration/enabled=${_mig}
-g perf_model/l3_cache/hybrid/line_map/mode=${_line_map}
-g perf_model/l3_cache/hybrid/restrict_fill_ways=${_restrict}
-g perf_model/l3_cache/llc/leak_power_mW=${_leak_mw}
-g perf_model/core/frequency=${BASE_FREQ_GHZ}
-g dvfs/type=simple
-g dvfs/transition_latency=2000
-g dvfs/simple/cores_per_socket=${SIM_N}
-g core/hook_periodic_ins/ins_global=${PERIOD_INS}
-g core/hook_periodic_ins/ins_per_core=0
-g lc/enabled=true
-g lc/periodic_ins=${PERIOD_INS}
-g lc/power_cap_w=${CAP_W}
-g lc/target_frac=${TF}
-g lc/hysteresis_w=${HYS_W}
-g lc/static_w=${STATIC_W}
-g lc/dyn_w_per_ghz=${DYN_W}
-g lc/llc_leak_w=${LLC_LEAK_OVERRIDE:-${_llc_leak_w}}
-g lc/freq/min_ghz=${LC_FMIN_GHZ:-${BASE_FREQ_GHZ}}
-g lc/freq/max_ghz=${FMAX_GHZ}
-g lc/freq/step_ghz=${STEP_GHZ}
-g lc/selective/enabled=${sel_enabled}
-g lc/selective/k=${sel_k}
EOF

      # If PLM_CFG_SH is set, add piecewise-linear model flags
      if [[ -n "${PLM_CFG_SH:-}" && -f "${PLM_CFG_SH}" ]]; then
        # Source the PLM config (defines PLM_F, PLM_B, PLM_AUTIL, PLM_AIPC arrays)
        # shellcheck disable=SC1090
        source "${PLM_CFG_SH}"
        local _plm_n=${#PLM_F[@]}
        echo "-g lc/piecewise/enabled=true"
        echo "-g lc/piecewise/verbose=0"
        echo "-g lc/piecewise/n_models=${_plm_n}"
        for (( _i=0; _i<_plm_n; _i++ )); do
          echo "-g lc/piecewise/${_i}/f_ghz=${PLM_F[$_i]}"
          echo "-g lc/piecewise/${_i}/b=${PLM_B[$_i]}"
          echo "-g lc/piecewise/${_i}/a_util=${PLM_AUTIL[$_i]}"
          echo "-g lc/piecewise/${_i}/a_ipc=${PLM_AIPC[$_i]}"
        done
        unset _i
      else
        echo "-g lc/piecewise/enabled=false"
      fi
      ;;
  esac
}
