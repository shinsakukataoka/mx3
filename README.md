## Directory layout

mx2 source:
- `mx2/bin/mx`                    : CLI (plan/validate/submit/verify)
- `mx2/config/site.yaml`          : per-machine paths (Sniper/SPEC/traces/binaries)
- `mx2/config/params.yaml`        : calibrated caps + (p_static, k_dyn) per uarch
- `mx2/config/devices/*.yaml`     : NVSim tables per TECH (mram14/mram32/sram7)
- `mx2/engine/*.sh`               : self-contained runners (SPEC / traces / microbench / kernel)
- `mx2/runner/*.sbatch|.sh`       : SLURM array wrapper + dispatcher

Outputs (default):
- `results_test/<campaign>/<run_id>/`
  - `env.sh`      : exported site paths used by jobs
  - `jobs.txt`    : 1 job per line as KEY=VAL tokens
  - `slurm/`      : SLURM stdout/stderr logs
  - `runs/`       : Sniper run directories

Each Sniper run directory (`OUTDIR`) contains:
- `run.yaml`, `cmd.info`, `env.caller.dump`, `sniper.log`, `sim.stats.sqlite3`, `sim.out` (if generated)
- `mx2_status.yaml` (done/failed + reason)

---

## Prereqs

You need working installs/paths for:
- Sniper (`SNIPER_HOME/run-sniper`, `SNIPER_HOME/scripts/roi-icount.py`)
- SPEC (only if using SPEC campaign): `SPEC_ROOT/shrc` and pre-generated `run_*` dirs
- Traces (only if using traces campaign): `TRACE_ROOT/*.sift`
- Microbench binaries (only if using microbench): `MICROBENCH_BIN/*`
- Kernel driver (only if using kernel): `BLIS_BIN` (+ optional `BLIS_LIBDIR`)

---

## Config files

### 1) `mx2/config/site.yaml` (machine paths)
This is the only per-user/per-machine file.

Minimum:
- `SNIPER_HOME`
- `CONDA_SQLITE_LIB` (or `CONDA_LIB`)
- SPEC campaign: `SPEC_ROOT`
- traces campaign: `TRACE_ROOT`
- microbench campaign: `MICROBENCH_BIN`
- kernel campaign: `BLIS_BIN` (+ `BLIS_LIBDIR` if needed)

### 2) `mx2/config/params.yaml` (calibration numbers)
Contains:
- per-uarch `p_static_w`, `k_dyn_w_per_ghz_util`
- per-uarch caps `cap_w.single[L3_MB]` and `cap_w.multicore[L3_MB]`

This is the “frozen calibration” file used to generate LeakDVFS variant labels.

### 3) `mx2/config/devices/*.yaml` (NVSim tables)
These define device latencies/energies/leakage for each TECH and L3 size.
The engine loads these tables at runtime (so device numbers are editable without code changes).

Files:
- `mram14.yaml`, `mram32.yaml`, `sram7.yaml`

---

## CLI overview

### Plan commands (create a run directory)
- `mx2/bin/mx plan-spec ...`
- `mx2/bin/mx plan-traces ...`
- `mx2/bin/mx plan-microbench ...`
- `mx2/bin/mx plan-kernel ...`

### Validate planned run
- `mx2/bin/mx validate <run_dir>`

### Submit as SLURM array
- `mx2/bin/mx submit <run_dir> [--dry-run] [--sbatch=...]`

### Verify status of finished/ongoing runs
- `mx2/bin/mx verify <run_dir>`

---

## Common knobs (arguments)

### Core experiment shape
- `--uarch gainestown|sunnycove|...`  (passed to `run-sniper -c`)
- `--tech mram14|mram32|sram7`
- `--l3 2,32,128`
- `--cores 1|4|8`
- `--roi-m <M>` and `--warmup-m <M>`

### Variant sets
- `--variant-set baseline`  -> baseline_sram_only + baseline_mram_only
- `--variant-set leakdvfs`  -> lc_* only
- `--variant-set leakdvfs3` -> lc_* + naive_lc_* + sram_lc_*
- `--variant-set all`       -> baseline + leakdvfs3

### Global vs selective DVFS
- global: `--dvfs global`
- selective: `--dvfs selective --topk K`
  - sets `lc/selective/enabled=true` and `lc/selective/k=K`

### LeakDVFS tuning knobs (encoded into variant label)
Defaults:
- `--target-frac 1.0`
- `--hyst-w 0.35`
- `--fmax-ghz 4.0`
- `--step-ghz 0.15`
- `--ldvfs-periodic-ins 2000000`

Overrides:
- `--static-w <W>` override p_static for label
- `--dyn-w <W>` override k_dyn for label

These are encoded into the variant string like:
`lc_c<cap>_s<static>_d<dyn>_tf<tf>_h<hyst>_f<fmax>_st<step>_pi<period>`

You can recover knob values from:
- `run.yaml` (variant field),
- `cmd.info` (explicit `-g lc/...` flags),
- `sniper.log` (LC init / DVFS change lines).

### Traces only
- `--workloads "<mix1>,<mix2>,..."`  (comma-separated; each mix uses `+` between traces)
- `--fmin-ghz 1.6` (sets `LC_FMIN_GHZ` for traces)

### Microbench only
- `--microbenches llc_read_hit,pointer_chase,gather_scatter`
- `--wss 2,8,32,128,256`  (WSS list in MB)

### Kernel only
- `--blis-sizes 512,1024,1536,2048`
- `--blis-reps 50`

### DRAM directory entries
- `--dir-entries 4194304` (default)
Example:
- `--dir-entries 2097152`

---

## How to pass variables beyond CLI

mx2 generates `jobs.txt` lines with KEY=VAL tokens. The SLURM wrapper exports them.
If you want to add a global knob that isn’t exposed as a CLI flag yet, you can:

1) Edit `<run_dir>/env.sh` to export an env var for all jobs, OR
2) Add `KEY=VAL` to each line in `jobs.txt` (no spaces allowed).

Example: set `LC_FMIN_GHZ=1.6` for SPEC too:
- add `export LC_FMIN_GHZ=1.6` to `env.sh`, or
- append `LC_FMIN_GHZ=1.6` to each job line.

---

## SLURM stdout/stderr location

SLURM logs go to:
- `<run_dir>/slurm/%x-%A_%a.out`
- `<run_dir>/slurm/%x-%A_%a.err`

Sniper logs go to each run’s `OUTDIR/sniper.log`.

---

## Small test runs

```bash
# Plan (creates results_test/spec/<run_id>/...)
mx2/bin/mx plan-spec \
  --uarch gainestown \
  --tech mram14 \
  --benches 505.mcf_r \
  --l3 32 \
  --cores 1 \
  --roi-m 50 --warmup-m 10 \
  --variant-set baseline \
  --tag smoke

# Grab latest run dir
RUN_DIR="$(ls -dt results_test/spec/* | head -1)"

mx2/bin/mx validate "$RUN_DIR"
mx2/bin/mx submit "$RUN_DIR" --dry-run
mx2/bin/mx submit "$RUN_DIR"
mx2/bin/mx verify "$RUN_DIR"
```

```bash
mx2/bin/mx plan-spec \
  --uarch gainestown \
  --tech mram14 \
  --benches 505.mcf_r \
  --l3 32 \
  --cores 1 \
  --roi-m 50 --warmup-m 10 \
  --variant-set leakdvfs3 \
  --tag ldvfs_smoke

RUN_DIR="$(ls -dt results_test/spec/* | head -1)"
mx2/bin/mx validate "$RUN_DIR"
mx2/bin/mx submit "$RUN_DIR"
```

```bash
mx2/bin/mx plan-traces \
  --uarch gainestown \
  --tech mram14 \
  --cores 4 \
  --workloads "505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r" \
  --l3 32 \
  --roi-m 200 --warmup-m 50 \
  --variant-set leakdvfs \
  --dvfs global \
  --tag trace_smoke

RUN_DIR="$(ls -dt results_test/traces/* | head -1)"
mx2/bin/mx validate "$RUN_DIR"
mx2/bin/mx submit "$RUN_DIR"
```

```bash
mx2/bin/mx plan-traces --uarch gainestown --cores 4 --l3 32 \
  --workloads "505.mcf_r+505.mcf_r+505.mcf_r+505.mcf_r" \
  --variant-set leakdvfs --dvfs selective --topk 2 --tag trace_sel2
```

```bash
mx2/bin/mx plan-microbench \
  --uarch gainestown \
  --tech mram14 \
  --cores 4 \
  --microbenches llc_read_hit \
  --wss 32 \
  --l3 32 \
  --roi-m 50 --warmup-m 0 \
  --variant-set baseline \
  --tag mb_smoke

RUN_DIR="$(ls -dt results_test/microbench/* | head -1)"
mx2/bin/mx validate "$RUN_DIR"
mx2/bin/mx submit "$RUN_DIR"
```

```bash
mx2/bin/mx plan-kernel \
  --uarch gainestown \
  --tech mram14 \
  --cores 4 \
  --l3 32 \
  --blis-sizes 512 \
  --roi-m 50 --warmup-m 0 \
  --variant-set baseline \
  --tag kern_smoke

RUN_DIR="$(ls -dt results_test/kernel/* | head -1)"
mx2/bin/mx validate "$RUN_DIR"
mx2/bin/mx submit "$RUN_DIR"
```

```bash
mx2/bin/mx plan-hca \
  --uarch gainestown \
  --tech mram14 \
  --benches 505.mcf_r \
  --l3 32 \
  --cores 1 \
  --roi-m 50 --warmup-m 10 \
  --variants baseline_sram_only,baseline_mram_only \
  --tag hca_smoke_base

RUN_DIR="$(ls -dt results_test/hca/*hca_smoke_base | head -1)"
mx2/bin/mx validate "$RUN_DIR"
mx2/bin/mx submit "$RUN_DIR" --dry-run
mx2/bin/mx submit "$RUN_DIR"
mx2/bin/mx verify "$RUN_DIR"
```

```bash
mx2/bin/mx plan-hca \
  --uarch gainestown \
  --tech mram14 \
  --benches 505.mcf_r \
  --l3 2,32,128 \
  --cores 1 \
  --roi-m 50 --warmup-m 10 \
  --variants baseline_mram_only,grid_s8_fillsram,mig_s8_fillsram_p8_c32 \
  --tag hca_smoke_hybrid

RUN_DIR="$(ls -dt results_test/hca/*hca_smoke_hybrid | head -1)"
mx2/bin/mx validate "$RUN_DIR"
mx2/bin/mx submit "$RUN_DIR"
mx2/bin/mx verify "$RUN_DIR"
```

```bash
mx2/bin/mx plan-hca \
  --uarch gainestown \
  --tech mram32 \
  --benches 505.mcf_r \
  --l3 32 \
  --cores 1 \
  --roi-m 50 --warmup-m 10 \
  --variants baseline_sram_only,baseline_mram_only \
  --tag hca_mram32_smoke

RUN_DIR="$(ls -dt results_test/hca/*hca_mram32_smoke | head -1)"
mx2/bin/mx validate "$RUN_DIR"
mx2/bin/mx submit "$RUN_DIR"

mx2/bin/mx plan-hca --uarch gainestown --tech sram7  --benches 505.mcf_r --l3 32 --cores 1 --roi-m 50 --warmup-m 10 --variants baseline_sram_only --tag hca_sram7_smoke
mx2/bin/mx plan-hca --uarch gainestown --tech sram32 --benches 505.mcf_r --l3 32 --cores 1 --roi-m 50 --warmup-m 10 --variants baseline_sram_only --tag hca_sram32_smoke

```


---

## Submitting with different SLURM resources

`array_runner.sbatch` has defaults, but you can override at submit time:

Example:
```bash
mx2/bin/mx submit <run_dir> \
  --sbatch=--time=72:00:00 \
  --sbatch=--mem=32G \
  --sbatch=--partition=cpu-dense-preempt-q
```

Full sweep:

```bash
# -----------------------
# 1) SPEC full sweep
# -----------------------
mx2/bin/mx plan-spec \
  --uarch gainestown \
  --tech mram14 \
  --cores 4 \
  --l3 2,32,128 \
  --roi-m 1000 --warmup-m 200 \
  --variant-set all \
  --tag full

SPEC_RUN="$(ls -dt results_test/spec/*full | head -1)"
mx2/bin/mx validate "$SPEC_RUN"
mx2/bin/mx submit "$SPEC_RUN" --sbatch=--time=72:00:00 --sbatch=--mem=8G


# -----------------------
# 2) TRACES global full sweep
# -----------------------
mx2/bin/mx plan-traces \
  --uarch gainestown \
  --tech mram14 \
  --cores 4 \
  --l3 2,32,128 \
  --roi-m 1000 --warmup-m 200 \
  --variant-set all \
  --tag global

TR_RUN="$(ls -dt results_test/traces/*global | head -1)"
mx2/bin/mx validate "$TR_RUN"
mx2/bin/mx submit "$TR_RUN" --sbatch=--time=72:00:00 --sbatch=--mem=32G


# -----------------------
# 3) TRACES selective LeakDVFS sweep (top-2 cores)
# (LeakDVFS only; no baselines here)
# -----------------------
mx2/bin/mx plan-traces \
  --uarch gainestown \
  --tech mram14 \
  --cores 4 \
  --l3 2,32,128 \
  --roi-m 1000 --warmup-m 200 \
  --variant-set leakdvfs \
  --dvfs selective --topk 2 \
  --tag sel_k2

TR_SEL="$(ls -dt results_test/traces/*sel_k2 | head -1)"
mx2/bin/mx validate "$TR_SEL"
mx2/bin/mx submit "$TR_SEL" --sbatch=--time=72:00:00 --sbatch=--mem=32G


# -----------------------
# 4) Microbench full sweep
# -----------------------
mx2/bin/mx plan-microbench \
  --uarch gainestown \
  --tech mram14 \
  --cores 4 \
  --l3 2,32,128 \
  --roi-m 1000 --warmup-m 200 \
  --variant-set all \
  --tag full

MB_RUN="$(ls -dt results_test/microbench/*full | head -1)"
mx2/bin/mx validate "$MB_RUN"
mx2/bin/mx submit "$MB_RUN" --sbatch=--time=24:00:00 --sbatch=--mem=8G


# -----------------------
# 5) Kernel full sweep (BLIS sizes default)
# -----------------------
mx2/bin/mx plan-kernel \
  --uarch gainestown \
  --tech mram14 \
  --cores 4 \
  --l3 2,32,128 \
  --roi-m 1000 --warmup-m 200 \
  --variant-set all \
  --tag full

K_RUN="$(ls -dt results_test/kernel/*full | head -1)"
mx2/bin/mx validate "$K_RUN"
mx2/bin/mx submit "$K_RUN" --sbatch=--time=24:00:00 --sbatch=--mem=8G

# -----------------------
# 6) HCA full sweep
# -----------------------

# mram32 full HCA sweep (12 benches × 3 L3 × 18 variants = 648 jobs)
mx2/bin/mx plan-hca \
  --uarch gainestown \
  --tech mram32 \
  --l3 2,32,128 \
  --cores 4 \
  --roi-m 1000 --warmup-m 200 \
  --tag hca_mram32_full

RUN_DIR="$(ls -dt results_test/hca/*hca_mram32_full | head -1)"
mx2/bin/mx validate "$RUN_DIR"
mx2/bin/mx submit "$RUN_DIR" --sbatch=--time=72:00:00 --sbatch=--mem=8G

# sram7 baseline-only (12 benches × 3 L3 × 1 variant = 36 jobs)
mx2/bin/mx plan-hca \
  --uarch gainestown \
  --tech sram7 \
  --l3 2,32,128 \
  --cores 4 \
  --roi-m 1000 --warmup-m 200 \
  --variants baseline_sram_only \
  --tag hca_sram7_base

RUN_DIR="$(ls -dt results_test/hca/*hca_sram7_base | head -1)"
mx2/bin/mx validate "$RUN_DIR"
mx2/bin/mx submit "$RUN_DIR" --sbatch=--time=72:00:00 --sbatch=--mem=8G

# sram32 baseline-only (12 benches × 3 L3 × 1 variant = 36 jobs)
mx2/bin/mx plan-hca \
  --uarch gainestown \
  --tech sram32 \
  --l3 2,32,128 \
  --cores 4 \
  --roi-m 1000 --warmup-m 200 \
  --variants baseline_sram_only \
  --tag hca_sram32_base

RUN_DIR="$(ls -dt results_test/hca/*hca_sram32_base | head -1)"
mx2/bin/mx validate "$RUN_DIR"
mx2/bin/mx submit "$RUN_DIR" --sbatch=--time=72:00:00 --sbatch=--mem=8G

# mram14 full HCA sweep (also 648 jobs)
mx2/bin/mx plan-hca \
  --uarch gainestown \
  --tech mram14 \
  --l3 2,32,128 \
  --cores 4 \
  --roi-m 1000 --warmup-m 200 \
  --tag hca_mram14_full

RUN_DIR="$(ls -dt results_test/hca/*hca_mram14_full | head -1)"
mx2/bin/mx validate "$RUN_DIR"
mx2/bin/mx submit "$RUN_DIR" --sbatch=--time=72:00:00 --sbatch=--mem=8G

```

Helpful runs:

```bash
# LeakDVFS tuning runs
UARCH="gainestown"
TECH="mram14"
BENCHES="505.mcf_r"
L3_LIST="2,32,128"
CORES="1"
ROI="1000"
WARM="200"

HYST_LIST="0.10,0.20,0.35"
PI_LIST="500000,1000000,2000000"
STEP_LIST="0.05,0.10,0.15"

for H in ${HYST_LIST//,/ }; do
  for PI in ${PI_LIST//,/ }; do
    for ST in ${STEP_LIST//,/ }; do
      mx2/bin/mx plan-spec \
        --uarch "$UARCH" --tech "$TECH" \
        --benches "$BENCHES" --l3 "$L3_LIST" --cores "$CORES" \
        --roi-m "$ROI" --warmup-m "$WARM" \
        --variant-set leakdvfs \
        --hyst-w "$H" \
        --ldvfs-periodic-ins "$PI" \
        --step-ghz "$ST" \
        --tag "tune_l3${L3_LIST}_roi${ROI}_warm${WARM}_h${H}_pi${PI}_st${ST}"
    done
  done
done
```

```bash
# Bash
UARCH="gainestown"
TECH="sram14"
BENCHES="500.perlbench_r"
L3_LIST="2,32,128"
CORES="1"
ROI="1000"
WARM="200"

OUTROOT="results_test/calibration/${UARCH}_perlbench_l3_${TECH}_roi${ROI}_warm${WARM}_leakdvfs3"

for F in 2.0 2.66 3.2; do
  mx2/bin/mx plan-spec \
    --out "$OUTROOT" \
    --uarch "$UARCH" \
    --tech "$TECH" \
    --benches "$BENCHES" \
    --l3 "$L3_LIST" \
    --cores "$CORES" \
    --roi-m "$ROI" --warmup-m "$WARM" \
    --variant-set leakdvfs3 \
    --base-freq-ghz "$F" \
    --tag "f${F}"
done

for RUN_DIR in "$OUTROOT"/spec/*_spec_gainestown_f*; do
  mx2/bin/mx validate "$RUN_DIR"
  mx2/bin/mx submit "$RUN_DIR"
done
```
