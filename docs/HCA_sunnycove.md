# HCA Sunnycove Reproduction Study

## Overview

332 single-core HCA simulations comparing SRAM/MRAM cache technologies across node sizes, policies, and latency regimes.

- **uarch**: sunnycove @ 2.2 GHz
- **Devices**: `config/devices/sunnycove/` (sram7, sram14, sram32, mram14, mram32)
- **Output**: `repro/hca/hca_sunnycove/`

## Stages

| # | Stage | Configs | Benches | L3 (MB) | Jobs |
|---|-------|---------|---------|---------|------|
| 1 | Baselines | SRAM7 baseline | 10 | 16/32/128 | 30 |
| 2 | Cross-node | SRAM14, SRAM32, MRAM14, MRAM32 | 10 | 16/32/128 | 120 |
| 3 | Static HCA | noparity s4/s8/s12 | 10 | 16/32/128 | 90 |
| 4 | Migration (unrestricted) | s4 p4c32, s4 p1c0 | 4 (top) | 16 | 8 |
| 5 | Restricted fill (static) | s4_rf | 4 (top) | 16/32/128 | 12 |
| 6 | Restricted fill (migration) | s4_rf p4c32, s4_rf p1c0 | 4 (top) | 16/32/128 | 24 |
| 7 | Read-latency sweep | s4 + MRAM rd 2x/3x/4x/5x | 2 (mcf, omnetpp) | 16/32/128 | 24 |
| 7b | MRAM baseline (latency) | baseline_mram_only + rd 2x/3x/4x/5x | 2 (mcf, omnetpp) | 16/32/128 | 24 |
| | **Total** | | | | **332** |

**Top 4 benches**: perlbench, mcf, omnetpp, deepsjeng

## Pipeline

### 1. Plan

```bash
bash mx3/hca_repro_sweep.sh
```

### 2. Submit

```bash
find repro/hca/hca_sunnycove -name jobs.txt -printf '%h\n' | sort | \
  while read d; do mx3/bin/mx submit "$d"; done
```

### 3. Verify

```bash
find repro/hca/hca_sunnycove -name jobs.txt -printf '%h\n' | sort | \
  while read d; do mx3/bin/mx verify "$d"; done
```

## Dynamic MRAM Read Multiplier

Stage 7 uses `MRAM_RD_MULT` env var instead of separate YAML files per multiplier. The engine (`hca_flags_common.sh`) applies `MRAM_RD_CYC *= MRAM_RD_MULT` at runtime after loading the base `mram14.yaml`.

## Key Files

| File | Role |
|------|------|
| `hca_repro_sweep.sh` | Plans all 332 jobs |
| `engine/run_hca.sh` | Per-job HCA simulation runner |
| `engine/hca_flags_common.sh` | Variant parsing + device loading + MRAM_RD_MULT |
| `bin/mx plan-hca` | Job planner (called by sweep script) |
| `config/devices/sunnycove/*.yaml` | Device parameters |
