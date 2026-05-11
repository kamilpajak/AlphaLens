#!/usr/bin/env bash
# Refactor C from the audit-perf optimization plan: run insider_pc_compound
# OOS (2018-2023, primary verdict) and final-lock (2024-2026, confirmation)
# concurrently in two tmux sessions on the same 8 vCPU pod.
#
# 2026-05-10: empirical pod smoke showed ProcessPoolExecutor over Form-4
# tickers is COUNTERPRODUCTIVE on RunPod's MooseFS network volume — 8
# workers reading parquet partitions simultaneously contend on
# FUSE/network rather than parallelizing on CPU. ALPHALENS_WORKERS=1
# (serial path; same byte-equivalent code via _score_one_ticker) is
# faster on pod despite Mac local SSD showing 35% gain with workers=8.
# See feedback_runpod_moosefs_process_pool_antipattern.md.
#
# Pre-reg windows are LOCKED per docs/research/insider_pc_compound_design_2026_05_10.md:
#   OOS:        --is-start 2018-01-01 --is-end 2023-12-31
#   Final-lock: --is-start 2024-01-01 --is-end 2026-03-31
#
# rebalance_stride 5 = 5 phase offsets per window (memo Section 5.1).
# Phase 0 of each window fires the IS 2014-2017 precheck guard (~30 min);
# phases 1-4 auto-skip (experiment_insider_pc_compound.py:_run_precheck logic).
#
# Monitoring (every few hours):
#   tmux ls
#   tmux capture-pane -p -t audit-oos | tail -30
#   tmux capture-pane -p -t audit-fl | tail -30
#   grep AUDIT_ /workspace/{oos,fl}_audit.log
#
# After both AUDIT_*_DONE markers appear, scp the JSON outputs locally.

set -euo pipefail

cd /workspace/AlphaLens
source /etc/rp_environment
export PATH="/root/.local/bin:$PATH"

OOS_OUT=/workspace/AlphaLens/docs/research/insider_pc_compound_audit_oos.json
FL_OUT=/workspace/AlphaLens/docs/research/insider_pc_compound_audit_finallock.json

# `remain-on-exit on` keeps the pane visible after the command exits so
# the scrollback (audit progress + AUDIT_*_DONE marker) stays inspectable
# via `tmux capture-pane`. Cleaner than `sleep 86400` (which leaks a hung
# process and dies after 24h regardless of audit duration).
# `set -o pipefail` is critical: without it `cmd | tee` returns tee's exit
# code (always 0), so AUDIT_*_DONE would falsely report success even if the
# audit crashed mid-run — silently producing a half-failed verdict artifact.
tmux new-session -d -s audit-oos \
    "set -o pipefail; ALPHALENS_WORKERS=1 .venv/bin/alphalens audit insider_pc_compound \
     --rebalance-stride 5 --is-start 2018-01-01 --is-end 2023-12-31 \
     --out ${OOS_OUT} 2>&1 | tee /workspace/oos_audit.log; \
     echo AUDIT_OOS_DONE=\$? >> /workspace/oos_audit.log" \; \
    set-option -t audit-oos remain-on-exit on

tmux new-session -d -s audit-fl \
    "set -o pipefail; ALPHALENS_WORKERS=1 .venv/bin/alphalens audit insider_pc_compound \
     --rebalance-stride 5 --is-start 2024-01-01 --is-end 2026-03-31 \
     --out ${FL_OUT} 2>&1 | tee /workspace/fl_audit.log; \
     echo AUDIT_FL_DONE=\$? >> /workspace/fl_audit.log" \; \
    set-option -t audit-fl remain-on-exit on

echo "Both audit sessions launched in tmux:"
tmux ls
echo ""
echo "Monitor: tmux capture-pane -p -t audit-oos | tail -30"
echo "Monitor: tmux capture-pane -p -t audit-fl | tail -30"
