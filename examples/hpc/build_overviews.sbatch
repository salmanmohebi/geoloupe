#!/usr/bin/env bash
# =============================================================================
# Build TerraScope overviews of a large store on an HPC cluster, one year per Slurm array task.
#
#   mkdir -p slurm_logs
#   sbatch --account=<project> --array=0-9%4 \
#     --export=ALL,SOURCE=/data/store.zarr,OUTDIR=/data/overviews,FIRST_YEAR=2010,VARIABLES=tas,pr \
#     examples/hpc/build_overviews.sbatch
#
# Every task writes OUTDIR/<year>.zarr and then OUTDIR/<year>.zarr.done; finished years are skipped
# when the job is submitted again (REDO=1 rebuilds them). Use all years in a dataset config with
#   overviews: /data/overviews/*.zarr
# Options (environment): MIN_FACTOR (default 4), WORKERS (default: CPUs of the node), PROJECT_DIR
# (the TerraScope checkout, default: the submit directory).
# =============================================================================
#SBATCH --job-name=ts-overviews
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --exclusive
#SBATCH --time=02:00:00
#SBATCH --output=slurm_logs/%x-%A_%a.out
set -euo pipefail

SOURCE="${SOURCE:?set SOURCE, the store to build overviews of}"
OUTDIR="${OUTDIR:?set OUTDIR, the folder for the yearly overview stores}"
FIRST_YEAR="${FIRST_YEAR:?set FIRST_YEAR}"
VARIABLES="${VARIABLES:-}"
MIN_FACTOR="${MIN_FACTOR:-4}"
WORKERS="${WORKERS:-${SLURM_CPUS_ON_NODE:-8}}"
YEAR=$((FIRST_YEAR + ${SLURM_ARRAY_TASK_ID:-0}))
OUT="$OUTDIR/$YEAR.zarr"

if [[ -e "$OUT.done" && "${REDO:-0}" != 1 ]]; then
    echo "$OUT is complete; skipping (REDO=1 rebuilds)"
    exit 0
fi
mkdir -p "$OUTDIR"
cd "${PROJECT_DIR:-$SLURM_SUBMIT_DIR}"

echo "$(date -Is) $SOURCE → $OUT ($YEAR, variables: ${VARIABLES:-all}) on $(hostname), $WORKERS workers"
uv run --no-sync terrascope overviews build "$SOURCE" "$OUT" \
    ${VARIABLES:+--variables "$VARIABLES"} --start "$YEAR-01-01" --end "$YEAR-12-31" \
    --min-factor "$MIN_FACTOR" --workers "$WORKERS" --overwrite
touch "$OUT.done"
echo "$(date -Is) done: $(du -sh "$OUT" | cut -f1)"
