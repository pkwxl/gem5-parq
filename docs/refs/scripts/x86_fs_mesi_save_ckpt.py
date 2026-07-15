# Copyright (c) 2026 The Regents of the University of California
# SPDX-License-Identifier: BSD-3-Clause
"""
One-time: boot X86 Ubuntu with a fast ATOMIC CPU (serial), stage the `threads`
benchmark, and checkpoint RIGHT BEFORE its timed region. Restore with the
parallel Ruby-Timing config (x86_fs_mesi_parallel_eventq.py, CHECKPOINT_DIR=...)
to land straight in the ROI under N+1 EventQueues -- no re-boot.

Mechanism
---------
The x86-ubuntu-24.04 image runs `after_boot.sh`, which fetches the gem5 readfile
(`readfile_contents` below) and executes it. Our run script:

  1. base64-decodes the *static* threads binary to /root/threads (the X86Board
     uses CowDiskImage, whose writes ARE serialized into the checkpoint -- see
     CowDiskImage::serialize, disk_image.cc:425 -- so the binary survives).
  2. `m5 exit`  -> hands control back to the host, which takes the checkpoint
     here: binary staged, guest bash paused on the very next line.
  3. (reached only on RESTORE) resetstats; run the benchmark; dumpstats; exit.

So the boot + base64 decode run under fast Atomic (once), and the benchmark ROI
runs under whatever the restore config uses (parallel Ruby-Timing).

  GEM5_RESOURCE_DIR=/workspace/gem5-resources \
  CKPT_DIR=/workspace/gem5-ckpt/x86-threads-roi \
  ./build/X86/gem5.opt -d /tmp/fs-save docs/refs/scripts/x86_fs_mesi_save_ckpt.py

The static binary is built from tests/test-progs/threads/src/threads.cpp:
  g++ -o threads.static threads.cpp -pthread -std=c++11 \
      -static -static-libgcc -static-libstdc++
"""

import base64
import os
from pathlib import Path

from gem5.components.boards.x86_board import X86Board
from gem5.components.cachehierarchies.ruby.mesi_two_level_cache_hierarchy import (
    MESITwoLevelCacheHierarchy,
)
from gem5.components.memory import DualChannelDDR4_2400
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.isas import ISA
from gem5.resources.resource import obtain_resource
from gem5.simulate.simulator import Simulator

NUM_CORES = 4
CKPT_DIR = Path(os.environ.get("CKPT_DIR", "/workspace/gem5-ckpt/x86-threads-roi"))
# Benchmark args: threads <num_values> <chunk_size>. chunk_size=1 is the
# original worst-case-sharing behavior (matches the SE baseline "200000 1").
BENCH_NVAL = os.environ.get("BENCH_NVAL", "200000")
BENCH_CHUNK = os.environ.get("BENCH_CHUNK", "1")
STATIC_BIN = Path(
    os.environ.get(
        "THREADS_BIN",
        "/workspace/gem5/tests/test-progs/threads/bin/x86/linux/threads.static",
    )
)

# Build the guest run script with the benchmark binary embedded as base64.
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

# Same board / memory / cache graph as the restore config (keep in sync) so the
# checkpoint restores cleanly; only CPU type + mem mode change across restore.
cache_hierarchy = MESITwoLevelCacheHierarchy(
    l1i_size="32KiB", l1i_assoc=8,
    l1d_size="32KiB", l1d_assoc=8,
    l2_size="1MiB", l2_assoc=16,
    num_l2_banks=1,
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

# Reuse the boot-with-systemd workload's exact kernel + disk (so the restore,
# which uses the same resource, is byte-compatible) but inject our run script.
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
    f"[save-ckpt] Atomic boot; staging threads ({len(_b64)} b64 bytes); "
    f"ckpt -> {CKPT_DIR}"
)
simulator = Simulator(board=board)
simulator.run()  # boots, decodes binary, returns at the pre-ROI m5 exit

cause = simulator.get_last_exit_event_cause()
print(f"[save-ckpt] run() returned at pre-ROI exit ({cause!r}); checkpointing")
CKPT_DIR.mkdir(parents=True, exist_ok=True)
simulator.save_checkpoint(CKPT_DIR)
print(f"[save-ckpt] checkpoint written to {CKPT_DIR}")
