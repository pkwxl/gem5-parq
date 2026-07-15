# Copyright (c) 2026 The Regents of the University of California
# SPDX-License-Identifier: BSD-3-Clause
"""
FAST(er) boot-and-checkpoint for the 3-level parallel-EventQueue experiment,
using KVM instead of classic-NoCache Atomic to reach the pre-ROI exit.

x86_fs_classic_save_ckpt.py boots under classic NoCache Atomic to sidestep
Ruby-on-Atomic slowness, but Atomic still emulates every instruction -- boot to
login took ~5.5h in this sandbox instead of the expected "minutes". KVM runs
guest instructions directly on the host CPU (hardware virtualization, no
emulation), so the same boot-to-pre-ROI-exit should take seconds to low
minutes. The checkpoint is still hierarchy-independent (.pmem + CPU arch state
+ device state; see the classic script's docstring), and restoring a
KVM-produced checkpoint into a different CPU model (TimingSimpleCPU, on a
different memory hierarchy) is the same well-supported switch every
KVM-then-switch-to-Timing gem5 example already relies on -- just done via a
checkpoint file instead of an in-run switch_processor() call.

Requires: host KVM access (/dev/kvm readable/writable -- in this sandbox that
means running under the `kvm` group, e.g. `sg kvm -c '...'`) and a gem5 build
with kvm_required support (build/X86/gem5.opt covers this; no Ruby protocol
needed since NoCache is classic memory).

  sg kvm -c '
  GEM5_RESOURCE_DIR=/workspace/gem5-resources \
  CKPT_DIR=/workspace/gem5-ckpt/x86-threads3-roi-kvm \
  ./build/X86/gem5.opt -d /tmp/fs3-save-kvm \
      docs/refs/scripts/x86_fs_kvm_save_ckpt.py
  '
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
from gem5.utils.requires import requires

requires(kvm_required=True)

NUM_CORES = 4  # MUST match x86_fs_mesi3_parallel_eventq.py
CKPT_DIR = Path(
    os.environ.get("CKPT_DIR", "/workspace/gem5-ckpt/x86-threads3-roi-kvm")
)
BENCH_NVAL = os.environ.get("BENCH_NVAL", "200000")
BENCH_CHUNK = os.environ.get("BENCH_CHUNK", "1")
STATIC_BIN = Path(
    os.environ.get(
        "THREADS_BIN",
        "/workspace/gem5/tests/test-progs/threads/bin/x86/linux/threads.static",
    )
)

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

cache_hierarchy = NoCache()
memory = DualChannelDDR4_2400(size="3GiB")  # match the restore config exactly
processor = SimpleProcessor(
    cpu_type=CPUTypes.KVM, num_cores=NUM_CORES, isa=ISA.X86
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
# gem5's KVM CPU doesn't emulate MWAIT/MONITOR; the fake CPUID family/model it
# reports nonetheless matches the guest kernel's mwait_idle allow-list, so the
# idle loop picks mwait and every core takes an invalid-opcode #UD -> "Kernel
# panic - not syncing: Attempted to kill the idle task!" at boot. Force HLT
# (which KVM does support) instead.
#
# Also: gem5's KVM CPU logs "MSR (0x4b564d0x) unsupported by gem5. Skipping"
# for the whole kvmclock paravirt-clock range at boot -- writes to those MSRs
# are silently dropped rather than backed. If the guest picks kvmclock as its
# clocksource anyway, whatever polls it post-boot never observes it advance
# and spins forever (observed: boot reaches login fine, then two vCPU threads
# sit pegged at ~100% CPU indefinitely with zero further console output).
# Force the same refined-jiffies clocksource early boot already uses
# successfully, and tell the kernel not to register kvmclock at all.
kernel_args = list(_p["kernel_args"]) + [
    "idle=halt",
    "no-kvmclock",
    "clocksource=jiffies",
]
board.set_kernel_disk_workload(
    kernel=_p["kernel"],
    disk_image=_p["disk_image"],
    kernel_args=kernel_args,
    readfile_contents=runscript,
)

print(
    f"[save-ckpt-kvm] KVM boot; staging threads "
    f"({len(_b64)} b64 bytes); ckpt -> {CKPT_DIR}"
)
simulator = Simulator(board=board)
simulator.run()  # boots, decodes binary, returns at the pre-ROI m5 exit

cause = simulator.get_last_exit_event_cause()
print(f"[save-ckpt-kvm] run() returned at pre-ROI exit ({cause!r}); checkpointing")
CKPT_DIR.mkdir(parents=True, exist_ok=True)
simulator.save_checkpoint(CKPT_DIR)
print(f"[save-ckpt-kvm] checkpoint written to {CKPT_DIR}")
