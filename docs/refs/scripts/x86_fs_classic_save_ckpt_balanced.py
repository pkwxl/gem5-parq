# Copyright (c) 2026 The Regents of the University of California
# SPDX-License-Identifier: BSD-3-Clause
"""
FAST boot-and-checkpoint for the 3-level parallel-EventQueue experiment,
using the BALANCED `threads_balanced` benchmark (docs/specs/S-013) instead
of the original `threads` binary.

Identical to x86_fs_classic_save_ckpt.py in every way except the staged
binary and the checkpoint directory -- see that script's docstring for why
classic NoCache Atomic is used instead of Atomic-on-Ruby. This variant
exists because the original `threads` benchmark has serial init/validate
loops that land entirely on one core, which made that core's domain the
overwhelming last-arriver in S-012 Step 3's critical-path trace (§13.5) --
a workload-shape artifact, not a coherence-topology finding.
`threads_balanced.cpp` parallelizes all three phases (init, compute,
validate) with the same block-cyclic partition and pins each thread to a
specific vCPU, so all four cores should be equally busy through the whole
ROI. `BENCH_NVAL`/`BENCH_CHUNK` defaults are kept identical to the original
checkpoint's so the only variable changed is the workload's shape, not its
size or false-sharing intensity.

Any gem5 build with the X86 ISA + classic memory works; this uses
build/X86/gem5.opt:

  GEM5_RESOURCE_DIR=/workspace/gem5-resources \
  CKPT_DIR=/workspace/gem5-ckpt/x86-threads-balanced3-roi-classic \
  ./build/X86/gem5.opt -d /tmp/fs3-save-classic-balanced \
      docs/refs/scripts/x86_fs_classic_save_ckpt_balanced.py
"""

import base64
import os
from pathlib import Path

from gem5.components.boards.x86_board import X86Board
from gem5.components.cachehierarchies.classic.no_cache import NoCache
from gem5.components.memory import DualChannelDDR4_2400
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.isas import ISA
from gem5.resources.resource import obtain_resource
from gem5.simulate.simulator import Simulator

NUM_CORES = 4  # MUST match x86_fs_mesi3_parallel_eventq.py
CKPT_DIR = Path(
    os.environ.get(
        "CKPT_DIR", "/workspace/gem5-ckpt/x86-threads-balanced3-roi-classic"
    )
)
BENCH_NVAL = os.environ.get("BENCH_NVAL", "200000")
BENCH_CHUNK = os.environ.get("BENCH_CHUNK", "1")
STATIC_BIN = Path(
    os.environ.get(
        "THREADS_BIN",
        "/workspace/gem5-bin/threads_balanced.static",
    )
)

# Guest run script with the benchmark embedded as base64. m5 exit (pre-ROI) is
# where the host checkpoints; everything after runs only on RESTORE.
_b64 = base64.b64encode(STATIC_BIN.read_bytes()).decode()
runscript = f"""#!/bin/bash
set -e
base64 -d > /root/threads_balanced <<'B64EOF'
{_b64}
B64EOF
chmod +x /root/threads_balanced
sync
# --- host checkpoints HERE (binary staged, pre-ROI) --------------------------
m5 exit
# --- everything below runs only on RESTORE, under the measurement CPU --------
m5 resetstats
/root/threads_balanced {BENCH_NVAL} {BENCH_CHUNK}
m5 dumpstats
m5 exit
"""

cache_hierarchy = NoCache()
memory = DualChannelDDR4_2400(size="3GiB")  # match the restore config exactly
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
    f"[save-ckpt-classic-balanced] classic NoCache Atomic boot; staging "
    f"threads_balanced ({len(_b64)} b64 bytes); ckpt -> {CKPT_DIR}"
)
simulator = Simulator(board=board)
simulator.run()  # boots, decodes binary, returns at the pre-ROI m5 exit

cause = simulator.get_last_exit_event_cause()
print(
    f"[save-ckpt-classic-balanced] run() returned at pre-ROI exit "
    f"({cause!r}); checkpointing"
)
CKPT_DIR.mkdir(parents=True, exist_ok=True)
simulator.save_checkpoint(CKPT_DIR)
print(f"[save-ckpt-classic-balanced] checkpoint written to {CKPT_DIR}")
