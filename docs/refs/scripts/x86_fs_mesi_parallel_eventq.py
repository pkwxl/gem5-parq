# Copyright (c) 2026 The Regents of the University of California
# SPDX-License-Identifier: BSD-3-Clause
"""
Full-system X86 + Ruby MESI_Two_Level + N Timing cores, with an optional
N-cores -> N+1-EventQueues parallel split (the parallel-EventQueue / shared-L2
project). This is the FS analogue of the SE parallel path in
``configs/deprecated/example/se.py`` (`--parallel-l2-eventq`); read that and
``docs/specs/parallel-eventq-lockfree-l2-design.md`` (sections 6.3, 8.x, 9.x)
for the authoritative reasoning behind every choice here.

Run
---
  # serial baseline (single EventQueue):
  ./build/X86/gem5.opt -d /tmp/fs-serial   docs/refs/scripts/x86_fs_mesi_parallel_eventq.py
  # parallel (N+1 EventQueues, N+1 host threads):
  PARALLEL_EVENTQ=1 ./build/X86/gem5.opt -d /tmp/fs-par docs/refs/scripts/x86_fs_mesi_parallel_eventq.py

Both runs MUST use the same LINK_LATENCY (see below): the parallel split is
supposed to leave simulated timing unchanged, so the validation is "diff the
two m5out/stats.txt". If they differ, that is a correctness regression to chase
in the same way sections 8-9 of the design doc chased the earlier ones.

STATUS (2026-07-14): serial mode boots. Parallel mode is correct across the
Ruby cache/coherence fabric (verified booting to sim-tick ~7.08e10, deep into
kernel init) but then hits a SECOND, FS-only cross-domain wall that has nothing
to do with Ruby:

    EventQueue::schedule: assert(when >= getCurTick()) failed
    NoncoherentXBar::recvTimingReq -> Bridge::schedTimingReq   (board.iobus, eq=0)

A CPU in a core domain (i+1) issues an MMIO / APIC access down the *classic*
memory-system PIO path (RubyPort pio -> bridge -> iobus, all in backend domain
0) with a device-access latency below sim_quantum. This path uses raw
EventQueue::schedule, NOT Ruby's Consumer::commitTick, so the per-consumer lock
+ arrival-tick machinery (design doc 8.x) does not cover it. Options, all with
accuracy/perf tradeoffs that belong in the design doc, not here:
  (a) drop sim_quantum below the minimum CPU->device PIO latency (kills the
      speedup -- back to the 9.1 regime, but proves correctness first);
  (b) give the classic-memory cross-domain hops the same defer-to-barrier
      treatment the Ruby edges got (C++ work);
  (c) restrict the parallel experiment to SE mode (the design doc's validated
      setting), where this device path does not exist.

Domain map (differs on purpose from se.py -- see the two notes below)
--------------------------------------------------------------------
  domain 0            : the whole uncore -- L2 bank(s), directory, memory
                        controllers, DMA controllers, and *every device*.
                        These all keep gem5's default eventq_index (0), so the
                        backend domain absorbs the entire FS I/O subsystem for
                        free, with no per-object assignment.
  domain i+1 (i=0..N-1): core i, its private L1 controller (+ sequencer +
                        L1 caches), its local APIC, and the SimplePt2Pt router
                        that fans out from that L1 controller.

That is exactly N+1 domains / N+1 host threads (parti-gem5's shape). Contrast
with se.py, which numbers cores 0..N-1 and the backend N, and needs an *extra*
domain for the Crossbar's shared central "xbar" router (design doc 6.3). This
config uses SimplePt2Pt, where every controller owns a private router (one
ext_link each, no shared router), so that extra domain disappears.
"""

import os
from pathlib import Path

import m5
from m5.objects import Root
from m5.util import fatal
from m5.util.convert import anyToLatency

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

# ---------------------------------------------------------------------------
# Knobs
# ---------------------------------------------------------------------------
NUM_CORES = 4
PARALLEL_EVENTQ = os.environ.get("PARALLEL_EVENTQ", "0") == "1"
BOARD_CLK = "3GHz"

# Ruby's clock. The stdlib does not pin RubySystem.clk_domain, so it inherits
# the board clock (parent). We assume that here to convert cycles<->ticks; it
# must match BOARD_CLK or the sim_quantum bound below is wrong.
RUBY_CLOCK = BOARD_CLK

# Cross-domain latency budget, in ruby cycles, applied to every int_link (the
# only cross-domain edges once cores sit in their L1 domain). This is a *timing*
# parameter -- it is set on BOTH the serial and parallel runs so they simulate
# the same machine. The stdlib default is 1 cycle, which pins the legal quantum
# to 1 cycle and lands you in the ~125x-slowdown regime of design-doc 9.1;
# raise it to buy a larger quantum (design doc 9.5/9.6). Correctness precondition
# (design doc 2.5/9.6): sim_quantum must not exceed this, since cross-domain
# arrival ticks are not snapped to the quantum grid.
LINK_LATENCY = 20  # ruby cycles

# sim_quantum override, in ticks. Default None derives the largest quantum the
# Ruby fabric allows (LINK_LATENCY ruby cycles). But FS adds classic-memory
# cross-domain edges the Ruby bound does not see -- the smallest observed is the
# iobus (IOXBar) forward_latency, 1 board cycle = 333 ticks at 3GHz -- so for a
# correctness-first FS run the quantum must drop below THAT. 300 ticks clears the
# iobus edge; lower further if a deeper edge crashes. This kills the speedup (the
# barrier fires ~every board cycle, design-doc 9.1 regime) -- the point here is
# only to prove the parallel split boots FS correctly.
SIM_QUANTUM_TICKS = 300


# ---------------------------------------------------------------------------
# The parallel split, applied after the object graph exists but before C++
# objects are created (i.e. inside _pre_instantiate, after super() has run
# _connect_things / built Root). Subclassing the board is the clean hook: the
# Simulator calls board._pre_instantiate() and then immediately instantiates.
# ---------------------------------------------------------------------------
class ParallelX86Board(X86Board):
    def _pre_instantiate(self, full_system=None):
        root = super()._pre_instantiate(full_system=full_system)

        ch = self.get_cache_hierarchy()
        net = ch.ruby_system.network
        cores = self.get_processor().get_cores()
        n = len(cores)

        # LINK_LATENCY goes on every int_link in BOTH modes so the serial and
        # parallel runs are the same simulated machine (see module docstring).
        for il in net.int_links:
            il.latency = LINK_LATENCY

        if not PARALLEL_EVENTQ:
            return root

        # --- core i + its private L1 + local APIC -> domain i+1 -------------
        # descendants() yields self first, so this covers each controller/core
        # object itself as well as its children (caches, sequencer, buffers,
        # interrupt controller). The uncore is left untouched at domain 0.
        for i, core in enumerate(cores):
            dom = i + 1
            for obj in ch._l1_controllers[i].descendants():
                obj.eventq_index = dom
            for obj in core.get_simobject().descendants():
                obj.eventq_index = dom

        # --- routers follow their controller ---------------------------------
        # SimplePt2Pt gives each controller a private router (Switch) via one
        # ext_link (ext_node=controller, int_node=router). Put each L1 router in
        # its core's domain; every other router stays at 0 with the backend.
        #
        # The router is a cross-*reference* on the ext_link, not a child, so it
        # must be set directly (el.descendants() does NOT reach it -- that was
        # the tick-333 crash). Setting the Switch also places its PerfectSwitch
        # and all its out-port Throttles: both take the Switch as their Consumer
        # `em` (PerfectSwitch.cc:63, Switch.cc:107 passes `this`), so they run on
        # the Switch's event queue. Each Throttle belongs to its *source* switch,
        # so every cross-domain hop is throttle(src) -> dst-router carrying the
        # full int_link latency (>= quantum). int_links/ext_links themselves are
        # passive here and stay at 0.
        l1_dom = {id(ch._l1_controllers[i]): i + 1 for i in range(n)}
        for el in net.ext_links:
            dom = l1_dom.get(id(el.ext_node))
            if dom is not None:
                for obj in el.int_node.descendants():  # the Switch + its buffers
                    obj.eventq_index = dom

        # --- sim_quantum: required when >1 EventQueue (simulate.cc:227) -------
        m5.ticks.fixGlobalFrequency()
        if SIM_QUANTUM_TICKS is not None:
            quantum = int(SIM_QUANTUM_TICKS)
        else:
            ruby_cycle = m5.ticks.fromSeconds(anyToLatency(RUBY_CLOCK))
            quantum = LINK_LATENCY * ruby_cycle  # largest Ruby-legal value
        if quantum <= 0:
            fatal("sim_quantum <= 0; check SIM_QUANTUM_TICKS/LINK_LATENCY")
        root.sim_quantum = quantum
        print(f"[parallel-eventq] sim_quantum={quantum} ticks")

        return root


# ---------------------------------------------------------------------------
# Board assembly
# ---------------------------------------------------------------------------
cache_hierarchy = MESITwoLevelCacheHierarchy(
    l1i_size="32KiB", l1i_assoc=8,
    l1d_size="32KiB", l1d_assoc=8,
    l2_size="1MiB", l2_assoc=16,
    num_l2_banks=1,  # single shared L2 bank == one backend domain
)

memory = DualChannelDDR4_2400(size="3GiB")  # X86Board is capped at 3GiB

processor = SimpleProcessor(
    cpu_type=CPUTypes.TIMING, num_cores=NUM_CORES, isa=ISA.X86
)

board = ParallelX86Board(
    clk_freq=BOARD_CLK,
    processor=processor,
    memory=memory,
    cache_hierarchy=cache_hierarchy,
)

# CHECKPOINT_DIR restores a login checkpoint taken by x86_fs_mesi_save_ckpt.py,
# skipping the boot entirely and starting the measurement from login. The
# checkpoint's core count / cache-hierarchy graph must match this config (4
# cores, Ruby MESI, 1 L2 bank); the Atomic->Timing CPU + mem-mode change across
# the restore is expected. Restore happens in Simulator._instantiate AFTER
# _pre_instantiate (so the eventq split + sim_quantum are already applied).
_ckpt = os.environ.get("CHECKPOINT_DIR")
_workload = obtain_resource(
    "x86-ubuntu-24.04-boot-with-systemd", resource_version="5.0.0"
)
if _ckpt:
    # set_workload() takes no checkpoint kwarg, so restore by calling
    # set_kernel_disk_workload directly with the resource's own kernel/disk
    # (identical to the save config) plus checkpoint=. The guest resumes its
    # paused run script and runs the benchmark; no readfile needed on restore.
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
    f"[parallel-eventq] mode={'PARALLEL' if PARALLEL_EVENTQ else 'serial'} "
    f"cores={NUM_CORES} link_latency={LINK_LATENCY}cyc "
    f"-> {NUM_CORES + 1 if PARALLEL_EVENTQ else 1} EventQueue(s) "
    f"{'restore=' + _ckpt if _ckpt else 'fresh-boot'}"
)

simulator = Simulator(board=board)
simulator.run()
