# PLM Calibration Pipeline

## Overview

The PLM (Piecewise-Linear Model) predicts non-cache package power as a function of frequency, utilization, and IPC:

```
P_nocache(f) = b_f + a_util × U_sum + a_ipc × U_sum × ipc
```

Calibration fits `(b_f, a_util, a_ipc)` per frequency from fixed-frequency SRAM-only simulations.

## Pipeline

### 1. Plan calibration jobs

```bash
bash mx3/plm_calibrate_sweep.sh --mode calib \
    --sram-device mx3/config/devices/sunnycove/sram14.yaml
```

Generates **1260 jobs** at `repro/calibration/plm_calib_sunnycove/`:
- n=1: 10 SPEC benches × 21 freqs (2.0–4.0 GHz) × 3 LLC sizes (16/32/128 MB) = 630
- n=4: 5 mixes × 21 × 3 = 315
- n=8: 5 mixes × 21 × 3 = 315

### 2. Submit and wait

```bash
mx3/bin/mx submit repro/calibration/plm_calib_sunnycove
mx3/bin/mx verify repro/calibration/plm_calib_sunnycove
```

### 3. Extract McPAT oracle points

```bash
SNIPER_HOME=~/src/sniper ROOT=repro/calibration/plm_calib_sunnycove/runs \
    bash mx3/tools/extract_oracle_points.sh
```

Produces `runs/oracle_points.csv` with per-run McPAT power, frequency, utilization.

### 4. Fit all 18 models

```bash
bash mx3/tools/fit_all_plm.sh \
    --calib-dir repro/calibration/plm_calib_sunnycove \
    --sniper-home ~/src/sniper \
    --out-dir repro/calibration/models
```

Generates 6 combos × 3 capacities = **18 `.sh` model files**:

| Combo | Training data |
|-------|--------------|
| n1 | single-core only |
| n4 | 4-core mixes only |
| n8 | 8-core mixes only |
| n1n4 | 1 + 4-core combined |
| n4n8 | 4 + 8-core combined |
| n1n4n8 | all core counts |

### 4b. Generate power caps

```bash
python3 mx3/tools/gen_plm_cap_w.py \
    --oracle-csv repro/calibration/plm_calib_sunnycove/runs/oracle_points.csv \
    --sram-device mx3/config/devices/sunnycove/sram14.yaml \
    --base-freq 2.2
```

Computes per-(workload, capacity) power caps: `P_cap = P_nocache_oracle(f_base) + LLC_leak_sram`.
Output is a YAML fragment to paste into `config/params.yaml` under `plm_cap_w`.

### 4c. Derive selective DVFS coefficients

For per-core DVFS (only 1 of N cores boosts), scale PLM coefficients by `1/N`:

```bash
python3 mx3/tools/derive_selective_plm.py \
    --n-cores 4 --f-base 2.2 repro/calibration/models/plm_sunnycove_n4_cal_*.sh
python3 mx3/tools/derive_selective_plm.py \
    --n-cores 8 --f-base 2.2 repro/calibration/models/plm_sunnycove_n8_cal_*.sh
```

Generates `_selk1.sh` variants alongside the original models (e.g., `plm_sunnycove_n4_cal_16M_selk1.sh`).

### 5. Validate

```bash
python3 mx3/tools/validate_plm.py \
    --calib-dir repro/calibration/plm_calib_sunnycove \
    --models-dir repro/calibration/models \
    --sniper-home ~/src/sniper \
    --sram-device mx3/config/devices/sunnycove/sram14.yaml \
    --mram-device mx3/config/devices/sunnycove/mram14.yaml
```

Reports per model: bias, MAE, MAPE, and DVFS decision agreement (boost/hold/down) vs McPAT oracle.

Outputs: `validation_detail.csv`, `validation_summary.csv`, `validation_summary.txt`.

## Held-out test (optional)

To validate on unseen workloads (6 excluded n=4 + 6 excluded n=8 mixes):

```bash
bash mx3/plm_calibrate_sweep.sh --mode test \
    --sram-device mx3/config/devices/sunnycove/sram14.yaml
mx3/bin/mx submit repro/calibration/plm_test_sunnycove
mx3/bin/mx verify repro/calibration/plm_test_sunnycove
SNIPER_HOME=~/src/sniper ROOT=repro/calibration/plm_test_sunnycove/runs \
    bash mx3/tools/extract_oracle_points.sh

python3 mx3/tools/validate_plm.py \
    --calib-dir repro/calibration/plm_test_sunnycove \
    --models-dir repro/calibration/models \
    --sniper-home ~/src/sniper \
    --sram-device mx3/config/devices/sunnycove/sram14.yaml \
    --mram-device mx3/config/devices/sunnycove/mram14.yaml
```

## Key files

| File | Role |
|------|------|
| `plm_calibrate_sweep.sh` | Plans calibration/test jobs |
| `tools/extract_oracle_points.sh` | Runs McPAT on sim outputs → `oracle_points.csv` |
| `tools/mcpat_plm_fit.py` | Per-frequency OLS fitting engine |
| `tools/fit_all_plm.sh` | Fits all 18 models in one run |
| `tools/validate_plm.py` | Bias/MAE/MAPE + DVFS decision agreement |
| `tools/gen_plm_cap_w.py` | Generates per-workload power caps for params.yaml |
| `tools/derive_selective_plm.py` | Derives per-core (k=1) selective DVFS coefficients |
| `config/devices/sunnycove/sram14.yaml` | SRAM device parameters |
