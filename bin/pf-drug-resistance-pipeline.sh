#!/bin/bash
set -o pipefail

# ========================
# CONFIGURATION LOADING
# ========================

# Get script directory to find config file
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Config resolution: an explicit first CLI arg wins, else the PIPELINE_CONFIG
# env var (set by the desktop GUI / a caller), else the shipped default. This
# lets the GUI run a per-job config without touching the repo default.
if [[ -n "${1:-}" ]]; then
    CONFIG_FILE="$1"
elif [[ -n "${PIPELINE_CONFIG:-}" ]]; then
    CONFIG_FILE="$PIPELINE_CONFIG"
else
    CONFIG_FILE="$PROJECT_ROOT/config/pipeline.conf"
fi

# Preserve a caller-provided reference-set label (the cloud worker / GUI passes
# it as an env var) before sourcing the config, which may also define it. The
# env value wins so per-run overrides aren't clobbered by the shipped default.
_ENV_REFERENCE_SET_VERSION="${REFERENCE_SET_VERSION:-}"

# Load configuration
if [[ -f "$CONFIG_FILE" ]]; then
    source "$CONFIG_FILE"
    echo "Loaded configuration from: $CONFIG_FILE"
else
    echo "ERROR: Configuration file not found at $CONFIG_FILE"
    echo "Please copy and edit config/pipeline.conf"
    exit 1
fi

# Convert relative paths to absolute (in-container safety: all inputs are used
# with absolute paths regardless of CWD)
if [[ ! "$REFERENCE" = /* ]]; then
    REFERENCE="$PROJECT_ROOT/$REFERENCE"
fi
if [[ ! "$ORIGINAL_BED_FILE" = /* ]]; then
    ORIGINAL_BED_FILE="$PROJECT_ROOT/$ORIGINAL_BED_FILE"
fi
[[ "$ANNOTATION_GFF" = /* ]] || ANNOTATION_GFF="$PROJECT_ROOT/$ANNOTATION_GFF"
[[ "$RESISTANCE_CATALOG" = /* ]] || RESISTANCE_CATALOG="$PROJECT_ROOT/$RESISTANCE_CATALOG"
[[ "$CLAIR3_MODEL_DIR" = /* ]] || CLAIR3_MODEL_DIR="$PROJECT_ROOT/$CLAIR3_MODEL_DIR"

# Effective reference-set provenance label: caller env > config value > native
# default. Stamped into the run manifest (and the PDF report) as the
# offline-capable token the backend resolves/backfills reference_set_id on.
REFERENCE_SET_VERSION="${_ENV_REFERENCE_SET_VERSION:-${REFERENCE_SET_VERSION:-WHO 2025 / PlasmoDB-68}}"

# Color definitions — only emit ANSI codes for an interactive terminal. When the
# output is redirected to a saved log (backend/queue runs) or NO_COLOR is set,
# these stay empty so the log is clean plain text.
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    NC='\033[0m'       # No Color
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    PURPLE='\033[0;35m'
    CYAN='\033[0;36m'
    WHITE='\033[1;37m'
else
    NC='' RED='' GREEN='' YELLOW='' BLUE='' PURPLE='' CYAN='' WHITE=''
fi

# Header formatting
function print_header() {
    echo -e "${PURPLE}"
    echo "===================================================================="
    echo -e " $1"
    echo "===================================================================="
    echo -e "${NC}"
}

function print_section() {
    echo -e "${CYAN}"
    echo "=== $1 ==="
    echo -e "${NC}"
}

function print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

function print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

# Diagnostics go to stderr so a backend worker can separate them from normal
# progress output (e.g. `cmd >run.log 2>errors.log`).
function print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1" >&2
}

function print_error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

# Single writer for manifest.json — used for success, partial, AND error, so
# every consumer gets the same shape however a run ends. Delegates to the one
# merged writer (src/write_manifest.py), which emits both run status (read by the
# backend poller) and provenance (read by the GUI Results view). Guarded by
# MANIFEST_WRITTEN so it emits exactly once per run.
#   $1 = status (success|partial|error)   $2 = stage (last stage reached)
MANIFEST_WRITTEN=false
CURRENT_STAGE="starting"
function emit_manifest() {
    local status="$1" stage="$2"
    MANIFEST_WRITTEN=true

    # Ensure the target dir exists even if we failed before initialize created it.
    mkdir -p "$OUTPUT_DIR" 2>/dev/null || true

    # Collect any PDFs produced (empty when opted out or on failure).
    local pdf PDF_REPORTS=()
    if compgen -G "$OUTPUT_DIR/final_reports/*.pdf" > /dev/null 2>&1; then
        for pdf in "$OUTPUT_DIR/final_reports/"*.pdf; do
            PDF_REPORTS+=( "$pdf" )
        done
    fi

    # Pass the resolved shell variables through as env so write_manifest.py stays
    # a thin, side-effect-free writer. write_manifest.py never hard-fails.
    MANIFEST_STATUS="$status" \
    MANIFEST_STAGE="$stage" \
    MANIFEST_RUN_STARTED="${RUN_STARTED:-}" \
    MANIFEST_SAMPLES="${PROCESSED_SAMPLES[*]:-}" \
    MANIFEST_PDFS="${PDF_REPORTS[*]:-}" \
    REPORT_MODE="${REPORT_MODE:-}" \
    REFERENCE_SET_VERSION="${REFERENCE_SET_VERSION:-}" \
    REFERENCE="${REFERENCE:-}" \
    ORIGINAL_BED_FILE="${ORIGINAL_BED_FILE:-}" \
    ANNOTATION_GFF="${ANNOTATION_GFF:-}" \
    RESISTANCE_CATALOG="${RESISTANCE_CATALOG:-}" \
    CLAIR3_MODEL="${CLAIR3_MODEL:-}" \
    CLAIR3_QUAL="${CLAIR3_QUAL:-}" \
    MIN_COVERAGE="${MIN_COVERAGE:-}" \
    MIN_QUAL="${MIN_QUAL:-}" \
    MIN_DP="${MIN_DP:-}" \
    MIN_MQ="${MIN_MQ:-}" \
    MIN_READ_LENGTH="${MIN_READ_LENGTH:-}" \
    MAX_READ_LENGTH="${MAX_READ_LENGTH:-}" \
    COV_MIN_BREADTH="${COV_MIN_BREADTH:-}" \
    QC_TOOL="${QC_TOOL:-}" \
        "${PYTHON_CMD:-python3}" "$PROJECT_ROOT/src/write_manifest.py" "$OUTPUT_DIR/manifest.json" \
        || print_warning "Failed to write manifest (non-fatal)"
}

# EXIT trap: if the run is exiting non-zero and no manifest was written yet,
# emit a status="error" manifest recording the last stage reached, so a backend
# never sees a failure as a silent absence of output.
function on_exit() {
    local rc=$?
    if [[ "$rc" -ne 0 && "$MANIFEST_WRITTEN" != true ]]; then
        print_error "Pipeline failed at stage '$CURRENT_STAGE' (exit $rc)"
        emit_manifest "error" "$CURRENT_STAGE"
    fi
    return "$rc"
}

run_qc() {
    local fastq="$1" outdir="$2" name="$3"
    mkdir -p "$outdir"
    if [ "$QC_TOOL" = "nanoplot" ]; then
        NanoPlot --legacy hex --fastq "$fastq" \
            -o "$outdir" \
            -t "$THREADS" \
            --maxlength "${QC_MAXLENGTH:-$MAX_READ_LENGTH}" \
            --plots dot kde
    else
        NanoStat --fastq "$fastq" \
            --outdir "$outdir" \
            --name "${name}_nanostat.txt" \
            --tsv \
            -t "$THREADS"
    fi
}

compute_coverage() {
    local bam="$1" bed="$2" barcode="$3" out="$4"
    : > "$out"
    while read -r chrom start end gid; do
        [ -z "$chrom" ] && continue
        samtools depth -aa -r "${chrom}:$((start + 1))-${end}" "$bam" \
        | awk -v s="$barcode" -v g="$gid" -v min="$MIN_DP" '
            { tot++; sum += $3; if ($3 >= 1) { seen++; sumseen += $3 } }
            END {
                if (seen == 0) {
                    printf "%s\t%s\t0.0\t0.0\tNO_COVERAGE\n", s, g
                } else {
                    whole = sum / tot          # whole-gene mean (diluted, for reference)
                    ampd  = sumseen / seen     # depth where reads actually landed
                    st = (ampd < min ? "LOW_COVERAGE" : "OK")
                    printf "%s\t%s\t%.1f\t%.1f\t%s\n", s, g, whole, ampd, st
                }
            }' >> "$out"
    done < "$bed"
}

# Reclaim disk by dropping large per-barcode intermediates as soon as the
# downstream step that consumes them has finished. Gated by KEEP_INTERMEDIATE:
# the desktop app defaults to dropping (a run's scratch is the bulk of its
# footprint), while the backend keeps them (KEEP_INTERMEDIATE=true) so a run's
# BAMs remain available for re-analysis (e.g. read-backed haplotype phasing).
# Guarded so an empty path can never widen the rm. Best-effort: never aborts.
function drop_intermediate() {
    [[ "${KEEP_INTERMEDIATE:-false}" == "true" ]] && return 0
    local target=$1
    [[ -n "$target" && "$target" != "$OUTPUT_DIR" && -e "$target" ]] || return 0
    rm -rf "$target" 2>/dev/null || print_warning "Could not remove $target"
}

# Set output directory relative to input if not absolute path
if [[ ! "$OUTPUT_DIR" = /* ]]; then
    OUTPUT_DIR="$PROJECT_ROOT/$OUTPUT_DIR"
fi

# Live-scan mode: a fast, decision-only pass the desktop GUI drives while a
# sequencing run is still in progress. When true, process_barcode aligns the
# length-filtered reads and computes per-gene coverage only (no QC, no adapter
# trimming, no Clair3, no report), and the run exits right after the barcode
# loop. LIVE_ONLY_BARCODES optionally limits a cycle to a space-separated list
# of barcode basenames (the ones whose input changed). Both default off so a
# bare / normal run is completely unaffected.
LIVE_SCAN="${LIVE_SCAN:-false}"
LIVE_ONLY_BARCODES="${LIVE_ONLY_BARCODES:-}"

# Find Python executable (prefer conda environment)
if command -v conda &> /dev/null && conda info --envs | grep -q "$CONDA_ENV"; then
    PYTHON_CMD="python"  # Use python from activated conda environment
elif command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
elif command -v python &> /dev/null; then
    PYTHON_CMD="python"
else
    echo "ERROR: No Python executable found. Please install Python or activate conda environment."
    exit 1
fi

# ========================
# PIPELINE FUNCTIONS
# ========================

function initialize() {
    print_header "Initializing pipeline environment"

    # Try a normal conda activation first, but don't hard-exit if it fails.
    # In a queue worker HOME may be unset or the shell non-interactive, which can
    # break `conda activate`; in that case resolve the env prefix directly and put
    # its bin on PATH. The prefix is derived from conda itself (host-agnostic),
    # never a hardcoded install location.
    eval "$(conda shell.bash hook)" 2>/dev/null || true
    if conda activate "$CONDA_ENV" 2>/dev/null; then
        print_success "Activated conda environment: $CONDA_ENV"
        # Ensure the env's bin takes precedence so tools (e.g. Clair3's python3)
        # resolve to this environment.
        export PATH="$CONDA_PREFIX/bin:$PATH"
    else
        print_warning "conda activate failed; resolving env prefix directly for: $CONDA_ENV"
        env_prefix=""
        # Prefer <conda base>/envs/<name>; fall back to scanning `conda env list`
        # (handles envs created with --prefix outside the base envs dir).
        conda_base="$(conda info --base 2>/dev/null)"
        if [[ -n "$conda_base" && -d "$conda_base/envs/$CONDA_ENV/bin" ]]; then
            env_prefix="$conda_base/envs/$CONDA_ENV"
        else
            env_prefix="$(conda env list 2>/dev/null \
                | awk -v e="$CONDA_ENV" '$1==e {print $NF}')"
        fi
        if [[ -n "$env_prefix" && -d "$env_prefix/bin" ]]; then
            export PATH="$env_prefix/bin:$PATH"
            print_success "Using conda env at: $env_prefix"
        else
            print_error "Could not locate conda env '$CONDA_ENV' on this host"
            exit 1
        fi
    fi

    mkdir -p "$OUTPUT_DIR"/{qc_raw,qc_trimmed,trimmed,aligned,clair3,variants,reports,final_reports}
    print_success "Created output directory structure"

    for f in "$REFERENCE" "$ORIGINAL_BED_FILE" "$ANNOTATION_GFF"; do
        [[ -f "$f" ]] || { print_error "Required input not found: $f"; exit 1; }
    done
    print_info "Found genome, BED, and GFF"

    # The baked fasta and its .fai ship as a matched pair inside the image, and
    # the data tree may be mounted read-only, so only index when the .fai is
    # genuinely absent (a write into /app/data would otherwise fail). Staleness
    # is meaningless for an immutable image layer.
    if [[ ! -f "${REFERENCE}.fai" ]]; then
        print_info "Indexing reference (index missing)..."
        samtools faidx "$REFERENCE" || { print_error "Failed to index reference"; exit 1; }
        print_success "Reference indexed"
    else
        print_info "Reference index present"
    fi

    ref_contigs=$(grep "^>" "$REFERENCE" | sed 's/^>//' | awk '{print $1}' | sort -u)

    missing=$(cut -f1 "$ORIGINAL_BED_FILE" | sort -u | grep -vxF -f <(echo "$ref_contigs") || true)
    if [[ -n "$missing" ]]; then
        print_error "BED contigs absent from genome (name mismatch):"
        echo "$missing"
        exit 1
    fi
    print_success "BED contig names match the genome"

    gff_missing=$(grep -v '^#' "$ANNOTATION_GFF" | cut -f1 | sort -u | grep -vxF -f <(echo "$ref_contigs") || true)
    [[ -n "$gff_missing" ]] && print_warning "GFF seqids not in genome: $(echo "$gff_missing" | tr '\n' ' ')"

    # Verify native Clair3 is available (conda install bioconda::clair3)
    if command -v run_clair3.sh &>/dev/null; then
        print_success "Clair3 is available"
    else
        print_error "Clair3 not available (expected run_clair3.sh on PATH)"
        exit 1
    fi

    pause_if_debug "Initialization complete"
}

function pause_if_debug() {
    if $DEBUG_MODE; then
        echo -e "${YELLOW}$1 Press [Enter] to continue or [Ctrl+C] to abort...${NC}"
        read
    fi
}

function prepare_bed_file() {
    print_section "Preparing target BED file"

    CLEANED_BED="$OUTPUT_DIR/cleaned_genes.bed"
    bedtools sort -i "$ORIGINAL_BED_FILE" > "$CLEANED_BED"

    if [[ ! -s "$CLEANED_BED" ]]; then
        print_error "Target BED is empty after sorting: $ORIGINAL_BED_FILE"
        exit 1
    fi

    export CLEANED_BED
    print_success "Target BED ready: $CLEANED_BED ($(wc -l < "$CLEANED_BED") regions)"
}

function process_barcode() {
    local barcode_dir=$1
    local barcode=$(basename "$barcode_dir")
    
    print_header "Processing $barcode"
    
    # Create output directories
    mkdir -p "$OUTPUT_DIR/qc_raw/$barcode"
    mkdir -p "$OUTPUT_DIR/qc_trimmed/$barcode"
    mkdir -p "$OUTPUT_DIR/trimmed/$barcode"
    mkdir -p "$OUTPUT_DIR/aligned/$barcode"
    mkdir -p "$OUTPUT_DIR/clair3/$barcode"
    mkdir -p "$OUTPUT_DIR/variants/$barcode"
    
   
    # Step 1: Combine + length-filter FASTQs
    print_section "Combining and length-filtering FASTQs for $barcode"

    combined="$OUTPUT_DIR/trimmed/${barcode}_combined.fastq"
    filtered="$OUTPUT_DIR/trimmed/${barcode}_lenfilt.fastq"

    shopt -s nullglob
    gz=( "$barcode_dir"/*.fastq.gz )
    plain=( "$barcode_dir"/*.fastq )
    if (( ${#gz[@]} )); then
        gzip -dc "${gz[@]}" > "$combined"
    elif (( ${#plain[@]} )); then
        cat "${plain[@]}" > "$combined"
    else
        print_error "No FASTQ files in $barcode_dir"; exit 1
    fi
    print_success "Combined $(( $(wc -l < "$combined") / 4 )) reads for $barcode"

    awk -v min="$MIN_READ_LENGTH" -v max="$MAX_READ_LENGTH" '
        NR%4==1{h=$0} NR%4==2{s=$0} NR%4==3{p=$0}
        NR%4==0{ if (length(s)>=min && length(s)<=max) print h"\n"s"\n"p"\n"$0 }
    ' "$combined" > "$filtered"
    print_success "Length-filtered to $(( $(wc -l < "$filtered") / 4 )) reads for $barcode"

    pause_if_debug "FASTQ prep complete for $barcode"

    # Live-scan fast path: the GUI's folder-watch only needs a live coverage
    # estimate to decide when depth is adequate, so skip the expensive QC /
    # adapter-trim / Clair3 steps. Align the length-filtered reads directly and
    # compute per-gene coverage, then return before variant calling. Coverage on
    # untrimmed reads is an estimate; the finalize pass (LIVE_SCAN=false) later
    # recomputes authoritative coverage and calls with the full pipeline.
    if [[ "$LIVE_SCAN" == "true" ]]; then
        print_section "Live alignment for $barcode"
        if minimap2 -ax map-ont \
                 --eqx \
                 -t $THREADS \
                 -R "@RG\tID:$barcode\tSM:$barcode" \
                 -L \
                 "$REFERENCE" \
                 "$filtered" \
            | samtools sort -@ $THREADS -o "$OUTPUT_DIR/aligned/$barcode/${barcode}_sorted.bam" - \
            && samtools index "$OUTPUT_DIR/aligned/$barcode/${barcode}_sorted.bam"; then
            print_success "Live alignment complete for $barcode"
        else
            print_error "Live alignment failed for $barcode"
            exit 1
        fi

        drop_intermediate "$combined"
        drop_intermediate "$filtered"

        print_section "Live coverage for $barcode"
        compute_coverage \
            "$OUTPUT_DIR/aligned/$barcode/${barcode}_sorted.bam" \
            "$CLEANED_BED" \
            "$barcode" \
            "$OUTPUT_DIR/reports/${barcode}_coverage.tsv"
        print_success "Live coverage written for $barcode"

        # The BAM is scratch for a live cycle (no Clair3 to consume it). Drop it
        # unless the operator opted to keep intermediates.
        drop_intermediate "$OUTPUT_DIR/aligned/$barcode"
        return 0
    fi


    # Step 2: Initial QC (pre-trim) — optional
    if [ "$RUN_PRETRIM_QC" = true ]; then
        print_section "Running initial QC for $barcode"
        if run_qc "$filtered" "$OUTPUT_DIR/qc_raw/$barcode" "$barcode"; then
            print_success "Initial QC complete for $barcode"
        else
            print_error "Initial QC failed for $barcode"
            exit 1
        fi
        pause_if_debug "Initial QC complete for $barcode"
    fi

    # Step 3: Adapter Trimming
    print_section "Trimming adapters for $barcode"
    if porechop_abi -i "$filtered" \
        -o "$OUTPUT_DIR/trimmed/$barcode/${barcode}_trimmed.fastq" \
        -t $THREADS \
        --verbosity 1; then
        print_success "Adapter trimming complete for $barcode"
    else
        print_error "Adapter trimming failed for $barcode"
        exit 1
    fi
    pause_if_debug "Adapter trimming complete for $barcode"

    # Step 4: Post-trimming QC
    print_section "Running post-trimming QC for $barcode"
    if run_qc "$OUTPUT_DIR/trimmed/$barcode/${barcode}_trimmed.fastq" "$OUTPUT_DIR/qc_trimmed/$barcode" "$barcode"; then
        print_success "Post-trimming QC complete for $barcode"
    else
        print_error "Post-trimming QC failed for $barcode"
        exit 1
    fi
    pause_if_debug "Post-trimming QC complete for $barcode"
    
    # Step 5: Alignment — pipe minimap2 straight to a sorted, indexed BAM
    print_section "Alignment for $barcode"
    local trimmed_fastq="$OUTPUT_DIR/trimmed/$barcode/${barcode}_trimmed.fastq"

    if minimap2 -ax map-ont \
             --eqx \
             -t $THREADS \
             -R "@RG\tID:$barcode\tSM:$barcode" \
             -L \
             "$REFERENCE" \
             "$trimmed_fastq" \
        | samtools sort -@ $THREADS -o "$OUTPUT_DIR/aligned/$barcode/${barcode}_sorted.bam" - \
        && samtools index "$OUTPUT_DIR/aligned/$barcode/${barcode}_sorted.bam"; then
        print_success "Alignment, sort, and index completed for $barcode"
    else
        print_error "Alignment/BAM processing failed for $barcode"
        exit 1
    fi
    pause_if_debug "Alignment complete for $barcode"

    # Reclaim space: the trimmed FASTQs are only needed up to alignment, which
    # has now consumed them into the sorted BAM. Drop the combined / length-
    # filtered scratch and the porechop output (no-op when KEEP_INTERMEDIATE).
    drop_intermediate "$combined"
    drop_intermediate "$filtered"
    drop_intermediate "$OUTPUT_DIR/trimmed/$barcode"

    # Step 5b: Per-gene coverage — must run before any early return below,
    # so genes that are silent for lack of reads are recorded, not skipped.
    compute_coverage \
        "$OUTPUT_DIR/aligned/$barcode/${barcode}_sorted.bam" \
        "$CLEANED_BED" \
        "$barcode" \
        "$OUTPUT_DIR/reports/${barcode}_coverage.tsv"
    
    # Step 6: Variant Calling (native conda Clair3, local model)
    print_section "Variant Calling for $barcode"

    run_clair3 "$OUTPUT_DIR/aligned/$barcode/${barcode}_sorted.bam" "$barcode"
    pause_if_debug "Variant calling complete for $barcode"

    # Reclaim space: the sorted BAM has now been consumed by both per-gene
    # coverage (Step 5b) and Clair3. Nothing downstream reads it again, so drop
    # it. Kept when KEEP_INTERMEDIATE=true (e.g. for read-backed phasing).
    drop_intermediate "$OUTPUT_DIR/aligned/$barcode"
    
    # Step 7: Robust Variant Filtering
    print_section "Variant Filtering for $barcode"
    
    # Input/Output Paths
    vcf_input="$OUTPUT_DIR/clair3/$barcode/merge_output.vcf.gz"
    filtered_vcf="$OUTPUT_DIR/variants/$barcode/${barcode}_filtered.vcf.gz"
    
    # 1. Validate Clair3 Output
    if [[ ! -f "$vcf_input" ]]; then
        print_error "Clair3 output missing at $vcf_input"
        exit 1
    fi
    
    # 2. Force Reindex (handles corrupted indices)
    if ! tabix -f -p vcf "$vcf_input"; then
        print_warning "Recompressing corrupted VCF..."
        if zcat "$vcf_input" | bgzip -c > "${vcf_input}.tmp" && \
           mv "${vcf_input}.tmp" "$vcf_input" && \
           tabix -f -p vcf "$vcf_input"; then
            print_success "VCF recovery successful"
        else
            print_error "VCF recovery failed"
            exit 1
        fi
    fi
    
    # 3. Normalise and Filter with BED Regions, Quality and Depth
    if bcftools view -R "$CLEANED_BED" "$vcf_input" -Ou | \
       bcftools norm -f "$REFERENCE" -m -any -Ou | \
       bcftools view -i "QUAL>=$MIN_QUAL && FMT/DP>=$MIN_DP" -Oz -o "$filtered_vcf"; then
        print_success "VCF filtering completed for $barcode"
    else
        print_error "bcftools filtering failed"
        exit 1
    fi
    
    # 4. Validate Output
    if [[ $(bcftools view -H "$filtered_vcf" | head -n 1 | wc -l) -eq 0 ]]; then
        print_warning "No variants passed filters for $barcode"
        # Create empty output for pipeline continuity
        touch "$OUTPUT_DIR/reports/${barcode}_haplotypes.txt"
    else
        tabix -f -p vcf "$filtered_vcf"
    fi
    
    print_info "Filtering complete: $filtered_vcf"

    # Step 8: Annotate variant consequences with bcftools csq
    print_section "Annotating consequences for $barcode"

    haplotype_report="$OUTPUT_DIR/reports/${barcode}_haplotypes.txt"
    annotated_vcf="$OUTPUT_DIR/variants/$barcode/${barcode}_csq.vcf.gz"

    if [[ ! -s "$filtered_vcf" ]]; then
        print_warning "No variants to annotate for $barcode"
        : > "$haplotype_report"
        return 0
    fi

    if ! bcftools csq -f "$REFERENCE" -g "$ANNOTATION_GFF" -p a \
            "$filtered_vcf" -Oz -o "$annotated_vcf"; then
        print_error "csq annotation failed for $barcode"
        exit 1
    fi

    bcftools query -f '%QUAL\t%FILTER\t[%DP]\t[%AF]\t%INFO/BCSQ\n' "$annotated_vcf" \
        | awk -v sample="$barcode" '
            function reformat(aa,   p,l,r,pos,ref,alt) {
                if (aa ~ />/) {
                    split(aa, p, ">"); l=p[1]; r=p[2]
                    pos=l; gsub(/[^0-9]/,"",pos)
                    ref=l; gsub(/[0-9]/,"",ref)
                    alt=r; gsub(/[0-9]/,"",alt)
                    return ref pos alt
                }
                return aa
            }
            BEGIN{FS="\t"; OFS="\t"}
            {
                qual=$1; dp=$3; af=$4; bcsq=$5
                if (bcsq=="." || bcsq=="") next
                n=split(bcsq, cons, ",")
                for (i=1;i<=n;i++) {
                    if (cons[i] ~ /^@/) continue
                    m=split(cons[i], f, "|")
                    gid=f[3]; sub(/\.[0-9]+$/,"",gid)
                    aa=(m>=6 ? reformat(f[6]) : "")
                    print sample, gid, f[2], f[1], aa, qual, dp, af
                }
            }' > "$haplotype_report"

    print_success "Annotation complete for $barcode ($(wc -l < "$haplotype_report") consequences)"
    print_info "Report: $haplotype_report"
}

function run_clair3() {
    local bam_file=$1
    local barcode=$2
    
    # Convert all paths to absolute
    local abs_bam=$(realpath "$bam_file")
    local abs_bai="${abs_bam}.bai"
    local abs_ref=$(realpath "$REFERENCE")
    local abs_ref_fai="${abs_ref}.fai"
    local abs_bed=$(realpath "$CLEANED_BED")
    local abs_output=$(realpath "$OUTPUT_DIR/clair3/$barcode")

    # Verify required files exist (plain indexed array — bash 3.2 has no associative arrays)
    local required_files=(
        "BAM:$abs_bam"
        "BAM_index:$abs_bai"
        "Reference:$abs_ref"
        "Reference_index:$abs_ref_fai"
        "BED_file:$abs_bed"
    )

    local entry file_type file_path
    for entry in "${required_files[@]}"; do
        file_type="${entry%%:*}"   # label, before the first colon
        file_path="${entry#*:}"    # path, everything after the first colon
        if [[ ! -f "$file_path" ]]; then
            print_error "$file_type not found: $file_path"
            exit 1
        fi
    done

    # Resolve the model. An absolute CLAIR3_MODEL (e.g. a user-imported model in
    # the writable user-data dir) is used as-is; otherwise it is a subdirectory
    # name under the registry dir CLAIR3_MODEL_DIR. A valid model is a directory
    # holding pileup.pt + full_alignment.pt. Fail fast with the list of available
    # models so the backend/operator can correct the choice.
    local model_path
    if [[ "$CLAIR3_MODEL" = /* ]]; then
        model_path="$CLAIR3_MODEL"
    else
        model_path="$CLAIR3_MODEL_DIR/$CLAIR3_MODEL"
    fi
    if [[ ! -f "$model_path/pileup.pt" || ! -f "$model_path/full_alignment.pt" ]]; then
        print_error "Clair3 model '$CLAIR3_MODEL' not found or incomplete in registry: $CLAIR3_MODEL_DIR"
        print_error "(expected $model_path/{pileup.pt,full_alignment.pt})"
        # Portable listing (no GNU find -printf): a valid model is any
        # subdirectory holding both weight files. Works under BSD find / bash 3.2.
        local avail="" d
        for d in "$CLAIR3_MODEL_DIR"/*/; do
            [[ -f "${d}pileup.pt" && -f "${d}full_alignment.pt" ]] || continue
            avail+="$(basename "$d") "
        done
        print_error "Available models: ${avail:-<none>}"
        exit 1
    fi

    print_section "Processing $barcode with Clair3"
    print_info "Reference: $abs_ref"
    print_info "BED targets: $abs_bed"
    print_info "BAM: $abs_bam"
    print_info "Model: $model_path"

    # Run Clair3 natively via conda
    if run_clair3.sh \
            --bam_fn="$abs_bam" \
            --ref_fn="$abs_ref" \
            --platform="ont" \
            --model_path="$model_path" \
            --threads=$THREADS \
            --output="$abs_output" \
            --bed_fn="$abs_bed" \
            --remove_intermediate_dir \
            --no_phasing_for_fa \
            --haploid_precise \
            --min_coverage=$MIN_COVERAGE \
            --chunk_size=$CHUNK_SIZE \
            --min_mq=$MIN_MQ \
            --qual=$CLAIR3_QUAL; then
        print_success "Clair3 variant calling completed for $barcode"
    else
        print_error "Clair3 variant calling failed for $barcode"
        exit 1
    fi
}

# ========================
# PIPELINE EXECUTION
# ========================

print_header "Starting PF Drug Resistance Pipeline"

# Record run start (ISO8601, UTC) for the manifest emitted at the end.
RUN_STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# From here on, any non-zero exit emits an error manifest (see on_exit).
trap on_exit EXIT

CURRENT_STAGE="initialize"
initialize
CURRENT_STAGE="prepare_bed"
prepare_bed_file

# Process each barcode, tracking sample names for the manifest.
PROCESSED_SAMPLES=()
for barcode_dir in "$FASTQ_PASS_DIR"/barcode*; do
    [ -d "$barcode_dir" ] || continue
    # Live-scan cycles may target only the barcodes whose input changed. When
    # LIVE_ONLY_BARCODES is set, skip any barcode whose basename isn't listed so
    # a cycle re-aligns only what's new (a whitespace-bounded match avoids
    # "barcode1" matching "barcode10").
    bc_name="$(basename "$barcode_dir")"
    if [[ -n "$LIVE_ONLY_BARCODES" && " $LIVE_ONLY_BARCODES " != *" $bc_name "* ]]; then
        continue
    fi
    CURRENT_STAGE="process:$bc_name"
    process_barcode "$barcode_dir"
    PROCESSED_SAMPLES+=( "$bc_name" )
done

# Live scan stops here: the per-barcode coverage TSVs in reports/ are the whole
# output. Skip combine/report/manifest (the finalize pass produces those) and
# exit cleanly so the GUI's folder-watch can parse the fresh coverage.
if [[ "$LIVE_SCAN" == "true" ]]; then
    print_success "Live scan complete (coverage in $OUTPUT_DIR/reports)"
    conda deactivate 2>/dev/null || true
    exit 0
fi

# Step 9: Generate Combined Reports (Updated)
CURRENT_STAGE="combine_reports"
print_section "Generating Combined Reports"

# Create combined intermediate file
COMBINED_INTERMEDIATE="$OUTPUT_DIR/haplotype_intermediate.txt"
> "$COMBINED_INTERMEDIATE"  # Clear existing file

# Combine all haplotype reports
for report in "$OUTPUT_DIR"/reports/*_haplotypes.txt; do
    [ -e "$report" ] || continue  # Skip if no files match
    cat "$report" >> "$COMBINED_INTERMEDIATE"
done

COMBINED_COVERAGE="$OUTPUT_DIR/coverage_intermediate.tsv"
> "$COMBINED_COVERAGE"
for cov in "$OUTPUT_DIR"/reports/*_coverage.tsv; do
    [ -e "$cov" ] || continue
    cat "$cov" >> "$COMBINED_COVERAGE"
done

# Generate final reports
if $PYTHON_CMD "$PROJECT_ROOT/src/combine_haplotype.py" \
    --input "$COMBINED_INTERMEDIATE" \
    --catalog "$RESISTANCE_CATALOG" \
    --output_dir "$OUTPUT_DIR/final_reports" \
    --coverage "$COMBINED_COVERAGE" \
    --collapse; then
    print_success "Combined reports generated successfully"
else
    print_error "Failed to generate combined reports"
    exit 1
fi

# Step 10: Generate PDF report (color-coded, from the three final CSVs)
CURRENT_STAGE="pdf_report"
print_section "Generating PDF Report"

REPORT_MODE="${REPORT_MODE:-combined}"

# Track manifest status: "success" unless the PDF step warns while CSVs exist.
RUN_STATUS="success"

# Report opt-out guard (per-job, concurrency-safe). generate_report.py only
# accepts combined|per-sample|both, so opting out MUST be a bash guard here and
# must never pass --mode none to argparse.
case "$REPORT_MODE" in
    none|off|skip)
        print_info "REPORT_MODE=$REPORT_MODE; skipping PDF generation (CSV reports already produced)"
        ;;
    *)
        if REFERENCE_SET_VERSION="$REFERENCE_SET_VERSION" \
           $PYTHON_CMD "$PROJECT_ROOT/src/generate_report.py" \
            --reports_dir "$OUTPUT_DIR/final_reports" \
            --output_dir "$OUTPUT_DIR/final_reports" \
            --qc_dir "$OUTPUT_DIR/qc_trimmed" \
            --mode "$REPORT_MODE"; then
            print_success "PDF report generated (mode: $REPORT_MODE)"
        else
            # Don't hard-fail the whole run just because the PDF step failed; the
            # CSVs are already written and are the authoritative output.
            print_warning "PDF report generation failed (mode: $REPORT_MODE); CSV reports are still available"
            RUN_STATUS="partial"
        fi
        ;;
esac

# Step 10b: Collect QC diagrams alongside the reports. When NanoPlot produced
# per-barcode plots, copy them into final_reports/qc so the diagrams travel with
# the deliverable. Fully guarded: never hard-fail the run over a missing/odd QC
# folder.
if [ "$QC_TOOL" = "nanoplot" ]; then
    print_section "Collecting QC Diagrams"
    qc_dest="$OUTPUT_DIR/final_reports/qc"
    collected=0
    for phase in trimmed raw; do
        src_root="$OUTPUT_DIR/qc_${phase}"
        [ -d "$src_root" ] || continue
        for src in "$src_root"/*/; do
            [ -d "$src" ] || continue
            barcode=$(basename "$src")
            dest="$qc_dest/$phase/$barcode"
            mkdir -p "$dest"
            if cp -R "$src"/. "$dest"/ 2>/dev/null; then
                collected=$((collected + 1))
            fi
        done
    done
    if [ "$collected" -gt 0 ]; then
        print_success "Collected QC diagrams for $collected sample(s) into final_reports/qc"
    else
        print_info "No NanoPlot diagrams found to collect"
    fi
fi

# Final output validation
print_section "Output Validation"
print_info "Generated reports:"
ls -lh "$OUTPUT_DIR/final_reports/" | while read line; do echo -e "  ${WHITE}$line${NC}"; done
print_info "Sample counts:"
if [[ -f "$OUTPUT_DIR/final_reports/resistance_calls.csv" ]]; then
    awk -F',' 'NR>1 {print $1}' "$OUTPUT_DIR/final_reports/resistance_calls.csv" | sort | uniq -c | while read line; do echo -e "  ${WHITE}$line${NC}"; done
fi

# Step 11: Emit manifest.json — the last artifact written, so the backend can
# poll for it as a completion signal. Written via the shared emit_manifest (the
# same writer the error trap uses), so success, partial, and error all share one
# JSON shape. status is "partial" only when the PDF step warned; CSVs are always
# produced by combine_haplotype.py.
CURRENT_STAGE="complete"
print_section "Writing manifest.json"
emit_manifest "$RUN_STATUS" "complete"
if [[ -f "$OUTPUT_DIR/manifest.json" ]]; then
    print_success "manifest.json written (status: $RUN_STATUS)"
else
    print_warning "manifest.json was not written"
fi

# Final cleanup — deactivate is a no-op harmless if activation fell back to PATH.
conda deactivate 2>/dev/null || true
print_header "Pipeline complete"
print_info "Final reports (CSVs): $OUTPUT_DIR/final_reports"
print_info "Run manifest: $OUTPUT_DIR/manifest.json"