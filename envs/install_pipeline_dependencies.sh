#!/bin/bash

eval "$(conda shell.bash hook)"

conda create -n nanopore_all -y python=3.11
conda activate nanopore_all
conda install -y -c conda-forge -c bioconda \
    minimap2 \
    samtools \
    bcftools \
    bedtools \
    biopython \
    cyvcf2 \
    nanoplot \
    nanostat \
    fastqc \
    porechop_abi \
    pillow \
    reportlab \
    clair3 \
    pyqt \
    keyring \
    psutil \
    openpyxl \
    matplotlib \
    pymupdf \
    conda-pack

# Download Clair3 PyTorch model (r941_prom_sup_g5014) for native conda Clair3
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
MODEL_DIR="$PROJECT_ROOT/data/clair3_models/r941_prom_sup_g5014"
MODEL_URL="https://www.bio8.cs.hku.hk/clair3/clair3_models_pytorch/r941_prom_sup_g5014"

mkdir -p "$MODEL_DIR"
if [[ ! -f "$MODEL_DIR/pileup.pt" || ! -f "$MODEL_DIR/full_alignment.pt" ]]; then
    echo "Downloading Clair3 PyTorch model (r941_prom_sup_g5014)..."
    # -k: the HKU model host's TLS certificate is currently expired
    curl -kfL -o "$MODEL_DIR/pileup.pt" "$MODEL_URL/pileup.pt"
    curl -kfL -o "$MODEL_DIR/full_alignment.pt" "$MODEL_URL/full_alignment.pt"
fi