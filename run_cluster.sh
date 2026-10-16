#!/bin/bash
# Run Citi Bike Wrapped on a Slurm cluster (e.g. Yale's).  Submit with:  sbatch run_cluster.sh
# Before the first run: copy this folder and your citibike-2025-parquet folder to the cluster
# (scp -r ...), and adjust the module / environment lines for your cluster.
#SBATCH --job-name=citibike-wrapped
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=01:00:00
#SBATCH --output=wrapped_%j.log

module load miniconda 2>/dev/null || true          # name differs by cluster: check `module avail`
pip install --user -q -r requirements.txt

python wrapped.py --year 2025 --trips "$HOME/citibike-2025-parquet/*.parquet" --threads 8 --memory 24GB
