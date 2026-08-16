# Backend Integration Guide

How to run the *P. falciparum* drug-resistance pipeline as a **queued, native subprocess**
from a backend worker. The pipeline is a self-contained CLI job: the backend injects settings
via environment variables, runs one process per job, then reads the **exit code** and
**`manifest.json`**. No Docker, no live streaming, no GUI.

---

## 1. Contract at a glance

| Aspect | Value |
|--------|-------|
| Interface | **CLI / subprocess** (`bin/pf-drug-resistance-pipeline.sh`) |
| Parametrisation | **Environment variables** (no positional args) |
| Success signal | **Exit code `0`** (authoritative) |
| Structured result | **`$OUTPUT_DIR/manifest.json`** (`status: success \| partial \| error`) |
| Diagnostics | **stderr** = errors/warnings, **stdout** = info/progress |
| Always-produced outputs | 3 CSVs in `$OUTPUT_DIR/final_reports/` |
| Compute | **CPU only** (Clair3 runs on CPU); scale with `THREADS` |
| Concurrency | Safe — every write lands under `$OUTPUT_DIR` |

---

## 2. Host prerequisites (one-time)

1. **conda** (miniconda/mamba) available on the worker host.
2. Build the tool environment once:
   ```bash
   bash envs/install_pipeline_dependencies.sh   # creates conda env `nanopore_all`
   ```
   The Clair3 model download is skipped because weights ship in `data/clair3_models/`.
3. The unpacked bundle tree is **read-only reference data** shared by all jobs. Do not write into it.

---

## 3. Invoking a job

The script sources `config/pipeline.conf` (relative to its own location) for defaults, then
**exported env vars override any default**. A minimal per-job invocation:

```bash
env \
  FASTQ_PASS_DIR=/jobs/<jobid>/fastq_pass \
  OUTPUT_DIR=/jobs/<jobid>/out \
  THREADS=8 \
  CLAIR3_MODEL=r941_prom_sup_g5014 \
  REPORT_MODE=none \
  DEBUG_MODE=false \
  bash /opt/pf-drug-resistance-pipeline/bin/pf-drug-resistance-pipeline.sh \
  </dev/null >/jobs/<jobid>/out/run.log 2>&1
```

- **`</dev/null`** — always run non-interactive (see gotcha #1).
- **`>run.log 2>&1`** — capture everything to a per-job log file (no live logs by design).
- Each job gets its **own `OUTPUT_DIR`**; `FASTQ_PASS_DIR` may be shared read-only.

### Expected input layout
```
$FASTQ_PASS_DIR/
├── barcode01/ *.fastq[.gz]
├── barcode02/ *.fastq[.gz]
└── ...
```

---

## 4. Key parameters (env-overridable)

| Variable | Default | Notes |
|----------|---------|-------|
| `FASTQ_PASS_DIR` | `data/fastq_pass` | Input; dir of `barcode*/` subdirs |
| `OUTPUT_DIR` | `results/analysis_output` | **Per-job, writable** |
| `THREADS` | `24` | CPU threads; keep `concurrency × THREADS ≤ host cores` |
| `CLAIR3_MODEL_DIR` | `data/clair3_models` | Model registry (dir of model subdirs) |
| `CLAIR3_MODEL` | `r941_prom_sup_g5014` | Must be a subdir with `pileup.pt` + `full_alignment.pt`; **must match flowcell chemistry** |
| `MIN_QUAL` | `15` | Variant QUAL filter |
| `MIN_DP` | `10` | Min depth |
| `MIN_READ_LENGTH` / `MAX_READ_LENGTH` | `300` / `5000` | Amplicon length filter |
| `QC_MAXLENGTH` | `8000` | NanoPlot x-axis cap only |
| `REPORT_MODE` | `combined` | `combined \| per-sample \| both \| none/off/skip` |
| `DEBUG_MODE` | `false` | **Must stay `false`** for backend (see gotcha #1) |
| `REFERENCE`, `ANNOTATION_GFF`, `RESISTANCE_CATALOG`, `ORIGINAL_BED_FILE` | bundled paths | Leave as defaults unless swapping reference data |

Relative paths resolve against the pipeline root, so defaults work regardless of the worker's CWD.

---

## 5. Reading the result

### Exit code (authoritative)
- `0` → run finished; inspect `manifest.json` for `success` vs `partial`.
- non-zero → hard failure; the EXIT trap still writes a `manifest.json` with `status: "error"`
  and the `stage` that failed, then preserves the original exit code.

### `manifest.json` (at `$OUTPUT_DIR/manifest.json`)
```json
{
  "status": "success",            // success | partial | error
  "stage": "complete",            // last stage reached (e.g. process:barcode02)
  "run_started": "2026-07-20T14:00:00Z",
  "run_finished": "2026-07-20T14:07:31Z",
  "sample_count": 4,
  "samples": ["barcode01", "barcode02", "barcode03", "barcode04"],
  "report_mode": "none",
  "outputs": {
    "final_reports_dir":    "/jobs/<id>/out/final_reports",
    "resistance_calls_csv": "/jobs/<id>/out/final_reports/resistance_calls.csv",
    "variant_detail_csv":   "/jobs/<id>/out/final_reports/variant_detail.csv",
    "coverage_report_csv":  "/jobs/<id>/out/final_reports/coverage_report.csv",
    "pdf_reports": []             // filled only when REPORT_MODE produces PDFs
  },
  "params": { "THREADS": 8, "MIN_QUAL": 15, "CLAIR3_MODEL": "r941_prom_sup_g5014" }
}
```

**Status meanings**
- `success` — CSVs produced; PDF step (if requested) succeeded.
- `partial` — CSVs produced but the optional PDF step warned/failed. **Job data is usable.**
- `error` — pipeline aborted at `stage`; see `run.log` (stderr).

### Output tree
```
$OUTPUT_DIR/
├── manifest.json                 # machine-readable result (read this)
├── final_reports/
│   ├── resistance_calls.csv      # always produced — per-sample drug classifications
│   ├── variant_detail.csv        # always produced — per-variant detail
│   ├── coverage_report.csv       # always produced — per-gene coverage
│   └── *.pdf                     # only if REPORT_MODE != none
├── qc_raw/  qc_trimmed/          # per-barcode QC
├── aligned/  clair3/  variants/  # per-barcode intermediates
└── reports/                      # per-barcode coverage/haplotype text
```
The **3 CSVs are the durable product** — decouple PDFs and render them on demand later
(`REPORT_MODE=none` at run time, generate PDFs from CSVs when a user asks).

---

## 6. Concurrency & isolation

- **Write isolation:** every job writes only under its own `$OUTPUT_DIR`. Reference data and
  model weights are read in place (read-only), so N jobs can share them.
- **Sizing:** each concurrent Clair3 loads its model into RAM → memory scales with
  concurrency, not disk. Keep `worker_concurrency × THREADS ≤ host cores`.
- **`.fai` note:** the bundle ships `REFERENCE.fai`, so no job writes into the reference tree.
  (If you ever supply a reference without its `.fai`, the *first* job builds it there — pre-build
  it to keep the reference tree strictly read-only under concurrency.)

---

## 7. Gotchas (read before shipping)

1. **`DEBUG_MODE=false` is mandatory.** When true the script pauses for `[Enter]` and will hang a
   worker. Force `DEBUG_MODE=false` and redirect `</dev/null`.
2. **Missing `config/pipeline.conf` → exit 1.** Keep the file next to the script (it ships in the bundle).
3. **Never pass `REPORT_MODE=none` to the Python report tool.** The bash guard already handles the
   opt-out; the tool itself only accepts `combined|per-sample|both`.
4. **Model/chemistry mismatch degrades silently.** A wrong `CLAIR3_MODEL` for the run's flowcell
   won't error — it lowers accuracy. Validate the chosen model against the flowcell **before** enqueue.
   The script *does* hard-fail if the model dir is absent/incomplete and lists available models on stderr.
5. **conda must be on PATH** for the worker; the script activates `nanopore_all` (with a PATH fallback).

---

## 8. Reference worker loop (Python)

```python
import json, os, subprocess

def run_job(job):
    env = {
        **os.environ,
        "FASTQ_PASS_DIR": job.fastq_dir,
        "OUTPUT_DIR":     job.out_dir,
        "THREADS":        str(job.threads),
        "CLAIR3_MODEL":   job.model,
        "REPORT_MODE":    "none",
        "DEBUG_MODE":     "false",
    }
    os.makedirs(job.out_dir, exist_ok=True)
    with open(job.log_path, "w") as log, open(os.devnull) as devnull:
        rc = subprocess.run(
            ["/opt/pf-drug-resistance-pipeline/bin/pf-drug-resistance-pipeline.sh"],
            env=env, stdin=devnull, stdout=log, stderr=subprocess.STDOUT,
        ).returncode

    manifest_path = os.path.join(job.out_dir, "manifest.json")
    manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else None

    if rc == 0 and manifest and manifest["status"] in ("success", "partial"):
        return "done", manifest          # partial still has usable CSVs
    return "failed", manifest            # inspect job.log_path (stderr) for the stage
```

---

## 9. Model registry (adding chemistries without a rebuild)

- A model is a **directory** containing `pileup.pt` + `full_alignment.pt`.
- Admin drops new model dirs under `CLAIR3_MODEL_DIR` (or points it at a shared mount).
- A backend "list models" endpoint enumerates subdirs of `CLAIR3_MODEL_DIR` that contain both
  `.pt` files; the user picks one; the job sets `CLAIR3_MODEL=<dirname>`.
- Weights are read in place at runtime — never copied into the job output.
