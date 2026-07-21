# Copyright (c) 2026 The Regents of the University of California
# SPDX-License-Identifier: BSD-3-Clause
"""
Full-system X86 + Ruby **MESI_Three_Level** (private L1 + private L2 + shared L3)
+ N **O3** cores, with an optional finer-grained parallel-EventQueue split that
gives every architectural tier its own host thread.

This is the O3 sibling of x86_fs_mesi3_parallel_eventq.py (which uses
CPUTypes.TIMING). See docs/specs/S-018-o3-cpu-restore-balanced-checkpoint.md for
why: TimingSimpleCPU blocks on each memory access one at a time (effectively
concurrency 1), so it never generates enough concurrent in-flight cross-domain
traffic to exercise what splitting the EventQueue is actually for. O3, with its
LSQ, can have multiple outstanding loads/stores per core. gem5 checkpoints only
capture architectural state, not timing-model state, so this restores the exact
same S-013 balanced checkpoint (booted with AtomicSimpleCPU, CPU-agnostic) that
x86_fs_mesi3_parallel_eventq.py restores -- no new checkpoint boot needed, just a
different ROI CPU model.

Also bumps each core's RubySequencer.max_outstanding_requests from the stdlib
default (16) to MAX_OUTSTANDING_REQUESTS (32), matching O3's default
LQEntries/SQEntries (32/32, src/cpu/o3/BaseO3CPU.py) -- otherwise the sequencer's
cap would throttle concurrency below what O3's LSQ can actually generate before
O3 itself becomes the limit. Applied in both serial and parallel modes (like
LINK_LATENCY) so the two arms simulate the same machine.

This is the 3-level successor to x86_fs_mesi_parallel_eventq.py. The move from a
shared L2 (MESI_Two_Level) to private-L2 + shared-L3 lets the eventq split follow
the physical hierarchy: each core's whole private stack (L1+L2) rides one domain,
the shared L3 gets its own, and each memory controller gets its own. Read
``docs/specs/INDEX.md`` (S-001 §6.3, S-003 §8.x, S-004 §9.x) and the SE
path in ``configs/deprecated/example/se.py`` for the reasoning this inherits.

REQUIRES a MESI_Three_Level build:
    scons build/X86_MESI_Three_Level/gem5.opt -j$(nproc)

Run
---
  # serial baseline (single EventQueue):
  ./build/X86_MESI_Three_Level/gem5.opt -d /tmp/fs3-serial \
      docs/refs/scripts/x86_fs_mesi3_parallel_eventq_o3.py
  # parallel (one thread per tier):
  PARALLEL_EVENTQ=1 ./build/X86_MESI_Three_Level/gem5.opt -d /tmp/fs3-par \
      docs/refs/scripts/x86_fs_mesi3_parallel_eventq_o3.py

Both runs MUST use the same LINK_LATENCY (and MAX_OUTSTANDING_REQUESTS) so they
simulate the same machine; the validation is "diff the two m5out/stats.txt" (any
difference is a correctness regression, per design doc 8-9).

Domain map (4 cores, 1 shared-L3 bank, 2 DDR channels -> 8 EventQueues)
----------------------------------------------------------------------
  domain 0            : DMA controllers + every device + uncore leftovers.
                        Default eventq_index (0); the backend absorbs the FS I/O
                        subsystem for free.
  domain i+1 (i=0..N-1): core i, its private L1 controller (+sequencer+L1 caches
                        +local APIC), its **private L2 controller**, and the
                        SimplePt2Pt routers that fan out from both.
  domain N+1          : the shared L3 controller(s) + their router(s).  <-- the
                        "5th eventq at the shared L3".
  domain N+2+j        : directory j (a memory controller front-end) + its
                        DOWNSTREAM DRAM MemCtrl + its router.

Why the DRAM MemCtrl rides the directory's domain (not domain 0): the
directory->DRAM edge is a *classic* memory port, not a Ruby int_link, so it uses
raw EventQueue::schedule with a sub-quantum queueing latency. Splitting that pair
across domains would reproduce the iobus-style cross-domain wall (see
parallel-eventq-fs-device-wall memory). Keeping directory j and MemCtrl j in one
domain leaves ONLY Ruby int_links (all carrying LINK_LATENCY) and the known
CPU->iobus classic edge crossing domains -- both already handled below.
get_mem_ports() and get_memory_controllers() enumerate in the same order, so
_directory_controllers[j] pairs with get_memory_controllers()[j] by index.
"""

import os
from pathlib import Path

import m5
from m5.util import fatal
from m5.util.convert import anyToLatency

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

# ---------------------------------------------------------------------------
# Knobs (KEEP IN SYNC with x86_fs_mesi3_save_ckpt.py for checkpoint restore)
# ---------------------------------------------------------------------------
NUM_CORES = 4
PARALLEL_EVENTQ = os.environ.get("PARALLEL_EVENTQ", "0") == "1"
# Comma-separated host CPU ids to pin the per-EventQueue threads to
# (index i pins the thread driving event queue i), e.g. "0,1,2,3,4,5,6,7".
#
# For **functionality validation** (this file's use case): leave empty,
# let OS scheduler assign threads to any CPU.
#
# For **strict performance comparison** (S-007/S-012/S-017): set to isolated
# CPUs only (e.g. "100,101,102,103,104,105,106,107") to prevent scheduling
# noise from interfering with accurate wall-clock measurements.
HOST_PIN_CPUS = os.environ.get("HOST_PIN_CPUS", "")
if HOST_PIN_CPUS and not PARALLEL_EVENTQ:
    # For serial mode with HOST_PIN_CPUS set: warn because it has no effect.
    print(f"[warning] HOST_PIN_CPUS={HOST_PIN_CPUS} has no effect in serial mode")
# Per-quantum global barrier mechanism: "cv" (default), "spin", or "hybrid".
# "spin" only pays off with HOST_PIN_CPUS pinning (design-doc 10.4/12).
EVENTQ_BARRIER_MODE = os.environ.get("EVENTQ_BARRIER_MODE", "cv")
EVENTQ_BARRIER_SPIN_ITERS = int(
    os.environ.get("EVENTQ_BARRIER_SPIN_ITERS", "0")
)
# S-012 critical-path instrumentation switch (off by default: no behaviour
# change). See docs/specs/S-012-eventq-critical-path-instrumentation-design.md.
EVENTQ_CRITPATH_TRACE = os.environ.get("EVENTQ_CRITPATH_TRACE", "0") == "1"
# Per-domain critPathBuffer capacity to reserve up front, records (design
# §4.4). 0 (default) = no reservation, ordinary vector growth.
EVENTQ_CRITPATH_RESERVE = int(
    os.environ.get("EVENTQ_CRITPATH_RESERVE", "0")
)
BOARD_CLK = "3GHz"
RUBY_CLOCK = BOARD_CLK  # stdlib leaves RubySystem clk inheriting the board clk

# Cross-domain latency budget, ruby cycles, on every int_link (set on BOTH runs).
LINK_LATENCY = 20  # ruby cycles

# Per-core cap on concurrent in-flight Ruby requests (RubySequencer.py default is
# 16). Raised to match O3's default LQEntries/SQEntries (32/32) -- see module
# docstring. Set on BOTH runs, same as LINK_LATENCY.
MAX_OUTSTANDING_REQUESTS = 32

# Correctness-first quantum, ticks. Bounded by the smallest *classic* cross-
# domain edge, the iobus (IOXBar) forward_latency = 1 board cycle = 333 ticks at
# 3GHz; 300 clears it. Kills the speedup (design-doc 9.1 regime) but proves the
# 3-level split boots FS correctly. None -> derive the largest Ruby-legal value.
# Env override lets S-009 SS20's Q-ramp protocol run without editing this file.
SIM_QUANTUM_TICKS = int(os.environ.get("SIM_QUANTUM_TICKS", "300"))

# Cache geometry: private 32KiB L1 (i/d), private 1MiB L2 per core, shared 8MiB L3.
L1I_SIZE, L1I_ASSOC = "32KiB", 8
L1D_SIZE, L1D_ASSOC = "32KiB", 8
L2_SIZE, L2_ASSOC = "1MiB", 16      # private, per core
L3_SIZE, L3_ASSOC = "8MiB", 16      # shared
NUM_L3_BANKS = 1                    # one shared L3 -> one L3 domain


def build_cache_hierarchy():
    return MESIThreeLevelCacheHierarchy(
        l1i_size=L1I_SIZE, l1i_assoc=L1I_ASSOC,
        l1d_size=L1D_SIZE, l1d_assoc=L1D_ASSOC,
        l2_size=L2_SIZE, l2_assoc=L2_ASSOC,
        l3_size=L3_SIZE, l3_assoc=L3_ASSOC,
        num_l3_banks=NUM_L3_BANKS,
    )


# ---------------------------------------------------------------------------
# The parallel split, applied in _pre_instantiate after super() has built the
# Ruby graph + Root but before C++ objects are created.
# ---------------------------------------------------------------------------
class ParallelX86Board(X86Board):
    def _pre_instantiate(self, full_system=None):
        root = super()._pre_instantiate(full_system=full_system)

        ch = self.get_cache_hierarchy()
        net = ch.ruby_system.network
        cores = self.get_processor().get_cores()
        n = len(cores)

        # LINK_LATENCY on every int_link in BOTH modes (same simulated machine).
        for il in net.int_links:
            il.latency = LINK_LATENCY

        # MAX_OUTSTANDING_REQUESTS on every core's L1 sequencer, BOTH modes
        # (same simulated machine) -- see module docstring.
        for l1 in ch._l1_controllers:
            l1.sequencer.max_outstanding_requests = MAX_OUTSTANDING_REQUESTS

        if not PARALLEL_EVENTQ:
            return root

        l3_dom = n + 1              # shared-L3 domain (the "5th" for N=4)
        mem_dom0 = n + 2           # first memory-controller domain

        # --- core i + private L1_i + private L2_i -> domain i+1 --------------
        # descendants() yields self first, so each covers the controller object
        # plus its caches/sequencer/buffers/interrupt controller.
        for i in range(n):
            dom = i + 1
            for obj in ch._l1_controllers[i].descendants():
                obj.eventq_index = dom
            for obj in ch._l2_controllers[i].descendants():
                obj.eventq_index = dom
            for obj in cores[i].get_simobject().descendants():
                obj.eventq_index = dom

        # --- shared L3 -> domain l3_dom -------------------------------------
        for l3 in ch._l3_controllers:
            for obj in l3.descendants():
                obj.eventq_index = l3_dom

        # --- each directory + its DOWNSTREAM DRAM MemCtrl -> own domain ------
        # Same domain for the pair so the classic directory->DRAM port never
        # crosses domains (see module docstring).
        memctrls = self.get_memory().get_memory_controllers()
        n_dir = len(ch._directory_controllers)
        for j, dirc in enumerate(ch._directory_controllers):
            dom = mem_dom0 + j
            for obj in dirc.descendants():
                obj.eventq_index = dom
            if j < len(memctrls):
                for obj in memctrls[j].descendants():  # MemCtrl + DRAM interface
                    obj.eventq_index = dom

        # --- routers follow their controller --------------------------------
        # SimplePt2Pt gives each controller a private router (Switch) via one
        # ext_link (ext_node=controller, int_node=router). The router is a
        # cross-*reference*, not a child, so el.descendants() does NOT reach it
        # (that was the tick-333 crash in the 2-level config) -- set el.int_node
        # directly. Setting the Switch also places its PerfectSwitch + out-port
        # Throttles (both take the Switch as their Consumer `em`).
        router_dom = {}
        for i in range(n):
            router_dom[id(ch._l1_controllers[i])] = i + 1
            router_dom[id(ch._l2_controllers[i])] = i + 1
        for l3 in ch._l3_controllers:
            router_dom[id(l3)] = l3_dom
        for j, dirc in enumerate(ch._directory_controllers):
            router_dom[id(dirc)] = mem_dom0 + j
        for el in net.ext_links:
            dom = router_dom.get(id(el.ext_node))
            if dom is not None:
                for obj in el.int_node.descendants():
                    obj.eventq_index = dom

        # --- sim_quantum: required when >1 EventQueue (simulate.cc:227) ------
        m5.ticks.fixGlobalFrequency()
        if SIM_QUANTUM_TICKS is not None:
            quantum = int(SIM_QUANTUM_TICKS)
        else:
            ruby_cycle = m5.ticks.fromSeconds(anyToLatency(RUBY_CLOCK))
            quantum = LINK_LATENCY * ruby_cycle
        if quantum <= 0:
            fatal("sim_quantum <= 0; check SIM_QUANTUM_TICKS/LINK_LATENCY")
        root.sim_quantum = quantum

        if HOST_PIN_CPUS:
            root.eventq_host_cpus = [
                int(c) for c in HOST_PIN_CPUS.split(",")
            ]

        root.eventq_barrier_mode = EVENTQ_BARRIER_MODE
        root.eventq_barrier_spin_iters = EVENTQ_BARRIER_SPIN_ITERS
        root.critpath_trace = EVENTQ_CRITPATH_TRACE
        root.critpath_trace_reserve = EVENTQ_CRITPATH_RESERVE

        n_dom = mem_dom0 + n_dir  # 0..(mem_dom0+n_dir-1)
        print(
            f"[parallel-eventq] 3-level split: {n_dom} EventQueues "
            f"(cores 1..{n}, L3={l3_dom}, mem={mem_dom0}..{mem_dom0 + n_dir - 1}), "
            f"sim_quantum={quantum} ticks"
            + (f", host-pin={HOST_PIN_CPUS}" if HOST_PIN_CPUS else "")
            + (
                f", barrier={EVENTQ_BARRIER_MODE}"
                + (
                    f"@{EVENTQ_BARRIER_SPIN_ITERS}"
                    if EVENTQ_BARRIER_MODE == "hybrid"
                    else ""
                )
                if EVENTQ_BARRIER_MODE != "cv"
                else ""
            )
            + (", critpath_trace=1" if EVENTQ_CRITPATH_TRACE else "")
            + (
                f", critpath_trace_reserve={EVENTQ_CRITPATH_RESERVE}"
                if EVENTQ_CRITPATH_RESERVE
                else ""
            )
        )
        return root


# ---------------------------------------------------------------------------
# Board assembly
# ---------------------------------------------------------------------------
cache_hierarchy = build_cache_hierarchy()
memory = DualChannelDDR4_2400(size="3GiB")  # X86Board is capped at 3GiB
processor = SimpleProcessor(
    cpu_type=CPUTypes.O3, num_cores=NUM_CORES, isa=ISA.X86
)
board = ParallelX86Board(
    clk_freq=BOARD_CLK,
    processor=processor,
    memory=memory,
    cache_hierarchy=cache_hierarchy,
)

# CHECKPOINT_DIR restores the pre-ROI checkpoint from x86_fs_mesi3_save_ckpt.py,
# skipping the boot and resuming straight into the benchmark under this parallel
# config. The checkpoint's core count + cache-hierarchy graph must match this one.
_ckpt = os.environ.get("CHECKPOINT_DIR")
_workload = obtain_resource(
    "x86-ubuntu-24.04-boot-with-systemd", resource_version="5.0.0"
)
if _ckpt:
    _p = _workload.get_parameters()
    board.set_kernel_disk_workload(
        kernel=_p["kernel"],
        disk_image=_p["disk_image"],
        kernel_args=_p["kernel_args"],
        checkpoint=Path(_ckpt),
    )
else:
    board.set_workload(_workload)

print(
    f"[parallel-eventq-o3] mode={'PARALLEL' if PARALLEL_EVENTQ else 'serial'} "
    f"cores={NUM_CORES} cpu=O3 L2=private/{L2_SIZE} L3=shared/{L3_SIZE} "
    f"link_latency={LINK_LATENCY}cyc "
    f"max_outstanding_requests={MAX_OUTSTANDING_REQUESTS} "
    f"{'restore=' + _ckpt if _ckpt else 'fresh-boot'}"
)

simulator = Simulator(board=board)

# STAT_DUMP_PERIOD (ticks) schedules a periodic mid-run stats dump. Each dump
# is a GlobalEvent whose process() calls pythonDump() -> CPython; used to
# exercise/repro the "stats dump on a subordinate EventQueue thread" wall
# (design doc 12.4) quickly instead of waiting for the ROI-end dump. Default
# 0 = off (no behaviour change).
_stat_period = int(os.environ.get("STAT_DUMP_PERIOD", "0"))
if _stat_period:
    import m5.stats as _m5stats

    simulator._instantiate()  # build C++ objects + populate mainEventQueue
    _m5stats.periodicStatDump(_stat_period)
    print(f"[parallel-eventq-o3] periodic stat dump every {_stat_period} ticks")

# MAX_TICKS (ticks) bounds the run to a fixed window from the restore point
# (a MAX_TICK exit dumps stats from the main thread and stops), for wall-clock
# A/B of the barrier modes without running the whole multi-hour ROI. Default
# 0 = run to a real exit event.
_max_ticks = int(os.environ.get("MAX_TICKS", "0"))
if _max_ticks:
    print(f"[parallel-eventq-o3] bounded run: {_max_ticks} ticks from restore")
    simulator.run(max_ticks=_max_ticks)
else:
    simulator.run()
