# DVFS Reproduction Experiments

## Overview

210 DVFS simulation jobs testing MRAM LLC leakage-driven frequency boosting across workloads, read-latency regimes, leakage gap scenarios, and power cap uncertainty.

- **uarch**: sunnycove @ 2.2 GHz base
- **PLM models**: `repro/calibration/models/` (per core count, per capacity)
- **Devices**: `config/devices/sunnycove/`
- **Output**: `repro/dvfs/`

## Stages

| # | Stage | Configs | Benches | L3 | Jobs |
|---|-------|---------|---------|-----|------|
| 1 | Main DVFS | MRAM+LeakDVFS vs SRAM baseline | 10 n=1 + 5 n=4 + 5 n=8 | 16/32/128 | 120 |
| 2 | Read-latency sweep | MRAM rd 2x/3x/4x/5x | 10 (n=1) | 128 | 40 |
| 3 | Leakage gap | Gap frac 0.25/0.50/0.75 | 10 (n=1) | 128 | 30 |
| 4 | Cap ± MAE | Cap ± 0.663W | 10 (n=1) | 128 | 20 |
| | **Total** | | | | **210** |

**Stage 1 details**: n=4/n=8 use selective DVFS (per-core boosting, k=1) with `_selk1` PLM models. Each (workload, capacity) runs two variants: MRAM+LeakDVFS and SRAM baseline at fixed 2.2 GHz.

## Pipeline

### 1. Plan

```bash
bash mx3/dvfs_repro_sweep.sh
```

### 2. Submit

```bash
for d in repro/dvfs/*/; do mx3/bin/mx submit "$d"; done
```

### 3. Verify

```bash
for d in repro/dvfs/*/; do mx3/bin/mx verify "$d"; done
```

## Runtime Sensitivity Parameters

No extra YAML files needed — all handled via env vars in `flags_common.sh`:

| Env Var | Stage | Effect |
|---------|-------|--------|
| `MRAM_RD_MULT` | 2 | Multiplies MRAM read latency cycles at runtime |
| `MRAM_LEAK_GAP_FRAC` | 3 | Shrinks SRAM-MRAM leakage gap (0.25 = gap becomes X/4) |
| `LLC_LEAK_OVERRIDE` | — | Directly overrides LLC leakage in governor (W) |

## Model Selection

| Core count | PLM model | DVFS mode |
|-----------|-----------|-----------|
| n=1 | `plm_sunnycove_n1_cal_<cap>M.sh` | Global |
| n=4 | `plm_sunnycove_n4_cal_<cap>M_selk1.sh` | Selective (k=1) |
| n=8 | `plm_sunnycove_n8_cal_<cap>M_selk1.sh` | Selective (k=1) |

## Key Files

| File | Role |
|------|------|
| `dvfs_repro_sweep.sh` | Plans all 210 jobs |
| `engine/flags_common.sh` | Variant parsing, PLM loading, runtime multipliers |
| `tools/derive_selective_plm.py` | Generates `_selk1` models for per-core DVFS |
| `config/params.yaml` | Per-workload power caps (`plm_cap_w`) |
