# Copyright (c) 2026 The Regents of the University of California
# SPDX-License-Identifier: BSD-3-Clause
"""
One-time: boot X86 Ubuntu under a fast ATOMIC CPU (serial), stage the `threads`
benchmark, and checkpoint RIGHT BEFORE its timed region. Restore with the 3-level
parallel config (x86_fs_mesi3_parallel_eventq.py, CHECKPOINT_DIR=...) to land in
the ROI under the per-tier EventQueue split -- no re-boot.

This is the MESI_Three_Level (private L1 + private L2 + shared L3) counterpart of
x86_fs_mesi_save_ckpt.py. The cache-hierarchy graph here MUST match the restore
config's, so the checkpoint's SimObject sections line up on restore -- the cache
geometry constants below are duplicated from x86_fs_mesi3_parallel_eventq.py and
must be kept in sync.

REQUIRES a MESI_Three_Level build:
    scons build/X86_MESI_Three_Level/gem5.opt -j$(nproc)

  GEM5_RESOURCE_DIR=/workspace/gem5-resources \
  CKPT_DIR=/workspace/gem5-ckpt/x86-threads3-roi \
  ./build/X86_MESI_Three_Level/gem5.opt -d /tmp/fs3-save \
      docs/refs/scripts/x86_fs_mesi3_save_ckpt.py

The static binary is built from tests/test-progs/threads/src/threads.cpp:
  g++ -o threads.static threads.cpp -pthread -std=c++11 \
      -static -static-libgcc -static-libstdc++
"""

import base64
import os
from pathlib import Path

from gem5.components.boards.x86_board import X86Board
from gem5.components.cachehierarchies.ruby.mesi_three_level_cache_hierarchy import (
    MESIThreeLevelCacheHierarchy,
)
from gem5.components.memory import DualChannelDDR4_2400
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.isas import ISA
from gem5.resources.resource import obtain_resource
from gem5.simulate.simulator import Simulator

NUM_CORES = 4
CKPT_DIR = Path(
    os.environ.get("CKPT_DIR", "/workspace/gem5-ckpt/x86-threads3-roi")
)
BENCH_NVAL = os.environ.get("BENCH_NVAL", "200000")
BENCH_CHUNK = os.environ.get("BENCH_CHUNK", "1")
STATIC_BIN = Path(
    os.environ.get(
        "THREADS_BIN",
        "/workspace/gem5/tests/test-progs/threads/bin/x86/linux/threads.static",
    )
)

# Cache geometry -- KEEP IN SYNC with x86_fs_mesi3_parallel_eventq.py.
L1I_SIZE, L1I_ASSOC = "32KiB", 8
L1D_SIZE, L1D_ASSOC = "32KiB", 8
L2_SIZE, L2_ASSOC = "1MiB", 16      # private, per core
L3_SIZE, L3_ASSOC = "8MiB", 16      # shared
NUM_L3_BANKS = 1

# Guest run script with the benchmark embedded as base64. m5 exit (pre-ROI) is
# where the host checkpoints; everything after runs only on RESTORE.
_b64 = base64.b64encode(STATIC_BIN.read_bytes()).decode()
runscript = f"""#!/bin/bash
set -e
base64 -d > /root/threads <<'B64EOF'
{_b64}
B64EOF
chmod +x /root/threads
sync
# --- host checkpoints HERE (binary staged, pre-ROI) --------------------------
m5 exit
# --- everything below runs only on RESTORE, under the measurement CPU --------
m5 resetstats
/root/threads {BENCH_NVAL} {BENCH_CHUNK}
m5 dumpstats
m5 exit
"""

cache_hierarchy = MESIThreeLevelCacheHierarchy(
    l1i_size=L1I_SIZE, l1i_assoc=L1I_ASSOC,
    l1d_size=L1D_SIZE, l1d_assoc=L1D_ASSOC,
    l2_size=L2_SIZE, l2_assoc=L2_ASSOC,
    l3_size=L3_SIZE, l3_assoc=L3_ASSOC,
    num_l3_banks=NUM_L3_BANKS,
)
memory = DualChannelDDR4_2400(size="3GiB")
processor = SimpleProcessor(
    cpu_type=CPUTypes.ATOMIC, num_cores=NUM_CORES, isa=ISA.X86
)
board = X86Board(
    clk_freq="3GHz",
    processor=processor,
    memory=memory,
    cache_hierarchy=cache_hierarchy,
)

_wl = obtain_resource(
    "x86-ubuntu-24.04-boot-with-systemd", resource_version="5.0.0"
)
_p = _wl.get_parameters()
board.set_kernel_disk_workload(
    kernel=_p["kernel"],
    disk_image=_p["disk_image"],
    kernel_args=_p["kernel_args"],
    readfile_contents=runscript,
)

print(
    f"[save-ckpt] 3-level Atomic boot; staging threads ({len(_b64)} b64 bytes); "
    f"ckpt -> {CKPT_DIR}"
)
simulator = Simulator(board=board)
simulator.run()  # boots, decodes binary, returns at the pre-ROI m5 exit

cause = simulator.get_last_exit_event_cause()
print(f"[save-ckpt] run() returned at pre-ROI exit ({cause!r}); checkpointing")
CKPT_DIR.mkdir(parents=True, exist_ok=True)
simulator.save_checkpoint(CKPT_DIR)
print(f"[save-ckpt] checkpoint written to {CKPT_DIR}")
